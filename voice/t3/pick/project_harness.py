"""Run the frozen Twin A project set against one arm and score it, strictly and leniently.

Inputs live outside the repository, under `$VOICE_HOME/evals/twina-pick`: the frozen project
snapshot, the labels, and every output this writes. Every run first verifies the frozen
manifest, so no label or snapshot can change after the first run. Scoring reuses Twin B's
`harness` outcome functions through `ProjectPick.as_thread_pick`, so one implementation
defines correct, and the strict, lenient and confirming-question views read the same.
"""

import argparse
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from .decide import decide_project
from .harness import (
	LATENCY_BAR_MS,
	LATENCY_METRIC,
	PASSING,
	WRONG,
	VIEWS,
	_merge,
	confirm_outcome,
	lenient,
	outcome,
	p95,
	summarise,
)
from .manifest import verify
from .model import ModelClient

ROOT = Path(os.environ.get("VOICE_HOME", Path.home() / "voice")) / "evals" / "twina-pick"
LABEL_FILES = ("known-hard.jsonl", "labels-labeler.jsonl")
ARMS = ("match", "model")
UNAMBIGUOUS = ("exact", "variant")
ZERO_WRONG_SLICES = ("shared", "nontarget", "absent", "missing")


def load_inputs(root=ROOT):
	root = Path(root)
	verify(root)
	snap = json.loads((root / "snapshots" / "snap-projects.json").read_text(encoding="utf-8"))
	cases = []
	for name in LABEL_FILES:
		path = root / "labels" / name
		cases += [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
	return snap, cases


def projects_of(snap):
	return [p for p in snap["projects"] if p["projectId"] in snap["allowedProjects"]]


def gold_for_thread(case):
	"""Twin B shaped gold, so Twin B scoring reads it unchanged."""
	gold = case["gold"]
	out = {"kind": gold["kind"]}
	if "project_id" in gold:
		out["thread_id"] = gold["project_id"]
	if "project_ids" in gold:
		out["thread_ids"] = gold["project_ids"]
	return out


def one_run(case, projects, arm, client, attempt):
	listed = [p["projectId"] for p in projects]
	names = {p["projectId"]: p.get("name", "") for p in projects}
	start = time.perf_counter()
	picked, meta = decide_project(case["utterance"], projects, client if arm == "model" else None)
	wall = round((time.perf_counter() - start) * 1000, 2)
	thread_pick = picked.as_thread_pick(listed)
	gold = gold_for_thread(case)
	verdict = outcome(thread_pick, gold, listed, meta.get("parse", "ok"))
	confirmed = confirm_outcome(thread_pick, gold, listed, meta.get("parse", "ok"))
	request_ms = meta.get("ms") if meta.get("model") and meta.get("parse") != "failed" else 0
	return {
		"case": case["id"], "snapshot": case["snapshot"], "slice": case["slice"], "arm": arm,
		"attempt": attempt, "utterance": case["utterance"], "gold": case["gold"],
		"pick": {"kind": picked.kind, "project_id": picked.project_id,
		         "candidates": list(picked.candidates), "question": picked.question,
		         "reason": picked.reason},
		"outcome": verdict, "lenient": lenient(verdict, gold),
		"confirm": confirmed, "confirm_lenient": lenient(confirmed, gold),
		"parse": meta.get("parse", "ok"), "model_call": bool(meta.get("model")),
		"shortlist": meta.get("shortlist"), "tokens": meta.get("tokens"),
		"wall_ms": wall, "request_ms": request_ms, "raw": meta.get("raw"), "names": names,
	}


def pass_bar(summary, arm):
	s = summary[arm]
	result = {"latency_metric": LATENCY_METRIC, "latency_bar_ms": LATENCY_BAR_MS}
	for view in VIEWS:
		merged = _merge(s, view)
		zero_bad = sum(_merge(s, view, *ZERO_WRONG_SLICES).get(k, 0) for k in WRONG)
		overall_bad = sum(merged.get(k, 0) for k in WRONG)
		unambiguous = _merge(s, view, *UNAMBIGUOUS)
		unamb_total = sum(unambiguous.values())
		unamb_correct = sum(unambiguous.get(k, 0) for k in PASSING)
		unnecessary = merged.get("asked_when_one", 0) + merged.get("unnecessary_question", 0)
		total_runs = sum(merged.values())
		result[view] = {
			"zero_wrong_in_shared_nontarget_absent_missing": zero_bad == 0,
			"at_most_two_wrong_overall": overall_bad <= 2,
			"zero_wrong_runs": zero_bad,
			"overall_wrong": overall_bad,
			"unambiguous_correct_rate": round(unamb_correct / max(1, unamb_total), 4),
			"unambiguous_at_least_90pct": unamb_correct / max(1, unamb_total) >= 0.90,
			"unnecessary_question_rate": round(unnecessary / max(1, total_runs), 4),
			"unnecessary_at_most_10pct": unnecessary / max(1, total_runs) <= 0.10,
			"confirmations": merged.get("confirmed", 0) + merged.get("confirm_wrong", 0),
			"confirm_wrong": merged.get("confirm_wrong", 0),
		}
	latencies = [v["p95_single_request_ms"] for v in s.values()]
	pooled = [ms for v in s.values() for ms in v.get("request_ms", [])]
	result["p95_single_request_ms"] = p95(pooled)
	result["p95_single_request_worst_slice_ms"] = max(latencies)
	result["p95_wall_ms"] = p95([ms for v in s.values() for ms in v["wall_ms"]])
	result["p95_at_most_700ms"] = result["p95_single_request_ms"] <= LATENCY_BAR_MS
	result["request_runs"] = sum(v["request_runs"] for v in s.values())
	gates = ("zero_wrong_in_shared_nontarget_absent_missing", "at_most_two_wrong_overall",
	         "unambiguous_at_least_90pct", "unnecessary_at_most_10pct")
	for pair in (("strict", "lenient"), ("confirm", "confirm_lenient")):
		name = "passes" if pair[0] == "strict" else "passes_confirm"
		result[name] = all(result[v][g] for v in pair for g in gates) and result["p95_at_most_700ms"]
	return result


def main():
	parser = argparse.ArgumentParser()
	parser.add_argument("--arm", choices=ARMS, required=True)
	parser.add_argument("--repeats", type=int, default=3)
	parser.add_argument("--config-line", default="", help="one line naming what differs from the last run")
	parser.add_argument("--expect", default="", help="one sentence on what this run should teach")
	parser.add_argument("--limit", type=int, default=0)
	parser.add_argument("--root", type=Path, default=ROOT, help="the eval directory: snapshots, labels, runs, cache")
	parser.add_argument("--summarise-only", action="store_true",
	                          help="score the run files already on disk; makes no pick")
	args = parser.parse_args()

	snap, cases = load_inputs(args.root)
	projects = projects_of(snap)
	if args.limit:
		cases = cases[: args.limit]
	out_dir = args.root / "runs" / args.arm
	out_dir.mkdir(parents=True, exist_ok=True)
	if args.summarise_only:
		runs = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(out_dir.glob("*-r*.json"))]
		summary = summarise(runs, [args.arm])
		result = {"summary": summary, "pass_bar": pass_bar(summary, args.arm)}
		(out_dir / "summary.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
		print(json.dumps(result, indent=1))
		return
	client = ModelClient(cache_dir=args.root / "cache") if args.arm == "model" else None
	header = [
		f"arm: {args.arm}",
		f"model: {client.model if client else 'none'}",
		f"repeats: {args.repeats}",
		f"diff: {args.config_line}",
		f"expect: {args.expect}",
		f"started: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
	]
	(out_dir / "config.txt").write_text("\n".join(header) + "\n", encoding="utf-8")
	print("\n".join(header), flush=True)

	runs = []
	for case in cases:
		for attempt in range(1, args.repeats + 1):
			if client:
				client.attempt = attempt
			row = one_run(case, projects, args.arm, client, attempt)
			runs.append(row)
			(out_dir / f"{case['id']}-r{attempt}.json").write_text(
				json.dumps(row, indent=1, ensure_ascii=False), encoding="utf-8")
		print(f"{case['id']} {row['outcome']} {row['request_ms']}ms/{row['wall_ms']}ms", flush=True)

	summary = summarise(runs, [args.arm])
	result = {"summary": summary, "pass_bar": pass_bar(summary, args.arm)}
	(out_dir / "summary.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
	print(json.dumps(result, indent=1))
	if client:
		print("api calls:", client.calls, "paused:", client.paused)


if __name__ == "__main__":
	main()
