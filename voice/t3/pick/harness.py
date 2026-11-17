"""Run the labeled set against one arm and score it, strictly and leniently.

Inputs live outside the repository, under `$VOICE_HOME/evals/twinb-pick`: the frozen snapshots,
the labels, and every output this writes. Every run first verifies the frozen manifest, so no
label or snapshot can change after the first run. The repository holds only this code and the
aggregate doc.
"""

import argparse
import json
import os
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

from .decide import pick
from .manifest import verify
from .model import MODEL, LocalClient, ModelClient
from .pick import Pick, question_for

ROOT = Path(os.environ.get("VOICE_HOME", Path.home() / "voice")) / "evals" / "twinb-pick"
LABEL_FILES = ("known-failures.jsonl", "labels-labeler.jsonl")
ARMS = ("prefilter", "model", "model_tie", "local")
LATENCY_METRIC = "single_request_p95_ms"
LATENCY_BAR_MS = 700


def load_inputs():
	verify(ROOT)
	snaps = {p.stem.split("-")[-1]: json.loads(p.read_text(encoding="utf-8"))
	         for p in sorted((ROOT / "snapshots").glob("snap-*.json"))}
	cases = []
	for name in LABEL_FILES:
		path = ROOT / "labels" / name
		cases += [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
	return snaps, cases


def visible(snap):
	return [t for t in snap["threads"] if t["projectId"] in snap["allowedProjects"]]


def outcome(picked, gold, listed, parse="ok"):
	if parse == "unparseable":
		return "unparseable"
	if parse == "failed":
		return "unavailable"
	if picked.kind == "one":
		if picked.thread_id not in listed:
			return "invalid_id"
		if gold["kind"] == "one":
			return "correct" if picked.thread_id == gold["thread_id"] else "wrong_thread"
		if gold["kind"] == "ask":
			return "picked_when_should_ask"
		return "wrong_thread"
	if picked.kind == "ask":
		if gold["kind"] == "ask":
			# An empty gold set means the only right answer is a question; every gold id must
			# appear when the labeler does define one.
			need = set(gold.get("thread_ids") or [])
			return "correct" if not need or need <= set(picked.candidates) else "question_missing_gold"
		if gold["kind"] == "one":
			return "asked_when_one"
		return "unnecessary_question"
	if gold["kind"] == "none":
		return "correct"
	if gold["kind"] == "one":
		return "said_none_when_exists"
	return "said_none_when_should_ask"


def lenient(verdict, gold):
	"""A question never counts as a wrong thread, and an unparseable reply reads as a question."""
	if verdict in ("unparseable", "unavailable"):
		return {"ask": "correct", "one": "asked_when_one", "none": "unnecessary_question"}[gold["kind"]]
	if verdict == "question_missing_gold":
		return "correct"
	return verdict


def one_run(case, snap, arm, client, attempt):
	threads = visible(snap)
	listed = [t["threadId"] for t in threads]
	titles = {t["threadId"]: t.get("title", "") for t in threads}
	start = time.perf_counter()
	meta = {"model": False, "parse": "ok", "ms": 0}
	if case.get("task_record_thread_id"):
		picked = Pick.coerce(case["task_record_thread_id"], listed, question_for([]), "task record")
	else:
		picked, meta = pick(case["utterance"], threads, snap["projects"], snap["takenAt"],
		                    client if arm in ("model", "model_tie", "local") else None,
		                    break_tie=(arm == "model_tie"))
	wall = round((time.perf_counter() - start) * 1000, 2)
	verdict = outcome(picked, case["gold"], listed, meta.get("parse", "ok"))
	request_ms = meta.get("ms") if meta.get("model") and meta.get("parse") != "failed" else 0
	return {
		"case": case["id"], "snapshot": case["snapshot"], "slice": case["slice"], "arm": arm,
		"attempt": attempt, "utterance": case["utterance"], "gold": case["gold"],
		"pick": {"kind": picked.kind, "thread_id": picked.thread_id, "candidates": list(picked.candidates),
		         "question": picked.question, "reason": picked.reason},
		"outcome": verdict, "lenient": lenient(verdict, case["gold"]),
		"parse": meta.get("parse", "ok"), "model_call": bool(meta.get("model")),
		"shortlist": meta.get("shortlist"), "tokens": meta.get("tokens"),
		"wall_ms": wall, "request_ms": request_ms, "raw": meta.get("raw"), "titles": titles,
	}


def p95(values):
	values = sorted(values)
	if not values:
		return 0
	return values[min(len(values) - 1, int(round(0.95 * (len(values) - 1))))]


def summarise(runs, arms):
	out = {}
	for arm in arms:
		rows = [r for r in runs if r["arm"] == arm]
		cases = {}
		for r in rows:
			cases.setdefault(r["case"], []).append(r)
		per_slice = {}
		for attempts in cases.values():
			slice_name = attempts[0]["slice"]
			bucket = per_slice.setdefault(slice_name, {
				"cases": 0, "pass_strict": 0, "pass_lenient": 0, "outcomes": {}, "lenient_outcomes": {},
				"request_ms": [], "wall_ms": [], "reasoning_tokens": 0, "content_tokens": 0,
				"model_calls": 0, "unparseable": 0,
			})
			bucket["cases"] += 1
			bucket["pass_strict"] += int(all(a["outcome"] == "correct" for a in attempts))
			bucket["pass_lenient"] += int(all(a["lenient"] == "correct" for a in attempts))
			for a in attempts:
				bucket["outcomes"][a["outcome"]] = bucket["outcomes"].get(a["outcome"], 0) + 1
				bucket["lenient_outcomes"][a["lenient"]] = bucket["lenient_outcomes"].get(a["lenient"], 0) + 1
				bucket["wall_ms"].append(a["wall_ms"])
				if a["request_ms"]:
					bucket["request_ms"].append(a["request_ms"])
				if a["model_call"]:
					bucket["model_calls"] += 1
				if a["parse"] == "unparseable":
					bucket["unparseable"] += 1
				got = a.get("tokens") or {}
				bucket["reasoning_tokens"] += got.get("reasoning_tokens") or 0
				bucket["content_tokens"] += got.get("content_tokens") or 0
		for bucket in per_slice.values():
			bucket["p95_single_request_ms"] = p95(bucket["request_ms"])
			bucket["p50_single_request_ms"] = round(statistics.median(bucket["request_ms"]), 1) if bucket["request_ms"] else 0
			bucket["p95_wall_ms"] = p95(bucket["wall_ms"])
			bucket["median_wall_ms"] = round(statistics.median(bucket["wall_ms"]), 2)
			bucket["request_runs"] = len(bucket.pop("request_ms"))
			bucket.pop("wall_ms")
		out[arm] = per_slice
	return out


def _merge(slices, *names):
	total = {}
	for name in names:
		for k, v in slices.get(name, {}).get("outcomes", {}).items():
			total[k] = total.get(k, 0) + v
	return total


def _merge_lenient(slices, *names):
	total = {}
	for name in names:
		for k, v in slices.get(name, {}).get("lenient_outcomes", {}).items():
			total[k] = total.get(k, 0) + v
	return total


def pass_bar(summary, arm):
	s = summary[arm]
	result = {"latency_metric": LATENCY_METRIC, "latency_bar_ms": LATENCY_BAR_MS}
	for view, merged in (("strict", _merge(s, *s)), ("lenient", _merge_lenient(s, *s))):
		bad_slices = _merge(s, "ambiguous", "nonexistent", "outside") if view == "strict" else \
			_merge_lenient(s, "ambiguous", "nonexistent", "outside")
		ambiguous_bad = sum(bad_slices.get(k, 0) for k in ("wrong_thread", "picked_when_should_ask"))
		overall_bad = sum(merged.get(k, 0) for k in ("wrong_thread", "picked_when_should_ask"))
		unambiguous = {"strict": _merge(s, "exact", "paraphrase"),
		               "lenient": _merge_lenient(s, "exact", "paraphrase")}[view]
		unamb_total = sum(unambiguous.values())
		unnecessary = merged.get("asked_when_one", 0) + merged.get("unnecessary_question", 0)
		total_runs = sum(merged.values())
		result[view] = {
			"zero_wrong_in_ambiguous_nonexistent_outside": ambiguous_bad == 0,
			"at_most_two_wrong_overall": overall_bad <= 2,
			"ambiguous_wrong": ambiguous_bad,
			"overall_wrong": overall_bad,
			"unambiguous_correct_rate": round(unambiguous.get("correct", 0) / max(1, unamb_total), 4),
			"unambiguous_at_least_90pct": unambiguous.get("correct", 0) / max(1, unamb_total) >= 0.90,
			"unnecessary_question_rate": round(unnecessary / max(1, total_runs), 4),
			"unnecessary_at_most_10pct": unnecessary / max(1, total_runs) <= 0.10,
		}
	latencies = [v["p95_single_request_ms"] for v in s.values()]
	result["p95_single_request_ms"] = max(latencies)
	result["p95_wall_ms"] = max(v["p95_wall_ms"] for v in s.values())
	result["p95_at_most_700ms"] = result["p95_single_request_ms"] <= LATENCY_BAR_MS
	result["request_runs"] = sum(v["request_runs"] for v in s.values())
	result["passes"] = all(
		result[view]["zero_wrong_in_ambiguous_nonexistent_outside"]
		and result[view]["at_most_two_wrong_overall"]
		and result[view]["unambiguous_at_least_90pct"]
		and result[view]["unnecessary_at_most_10pct"]
		for view in ("strict", "lenient")
	) and result["p95_at_most_700ms"]
	return result


def main():
	parser = argparse.ArgumentParser()
	parser.add_argument("--arm", choices=ARMS, required=True)
	parser.add_argument("--repeats", type=int, default=3)
	parser.add_argument("--config-line", default="", help="one line naming what differs from the last run")
	parser.add_argument("--expect", default="", help="one sentence on what this run should teach")
	parser.add_argument("--limit", type=int, default=0)
	parser.add_argument("--summarise-only", action="store_true", help="score the run files already on disk; makes no pick")
	args = parser.parse_args()

	snaps, cases = load_inputs()
	if args.limit:
		cases = cases[: args.limit]
	out_dir = ROOT / "runs" / args.arm
	out_dir.mkdir(parents=True, exist_ok=True)
	if args.summarise_only:
		runs = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(out_dir.glob("*-r*.json"))]
		summary = summarise(runs, [args.arm])
		result = {"summary": summary, "pass_bar": pass_bar(summary, args.arm)}
		(out_dir / "summary.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
		print(json.dumps(result, indent=1))
		return
	client = ModelClient() if args.arm in ("model", "model_tie") else LocalClient() if args.arm == "local" else None
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
		snap = snaps[case["snapshot"]]
		for attempt in range(1, args.repeats + 1):
			if client:
				client.attempt = attempt
			row = one_run(case, snap, args.arm, client, attempt)
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
