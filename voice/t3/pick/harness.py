"""Run the labeled set against one arm and score it.

Inputs live outside the repository, under `$VOICE_HOME/evals/twinb-pick`: the frozen snapshots, the
labels, and every output this writes. The repository holds only this code and the aggregate doc.
"""

import argparse
import json
import os
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

from .decide import pick
from .model import MODEL, LocalClient, ModelClient
from .pick import Pick

ROOT = Path(os.environ.get("VOICE_HOME", Path.home() / "voice")) / "evals" / "twinb-pick"


def load_inputs():
	snaps = {p.stem.split("-")[-1]: json.loads(p.read_text(encoding="utf-8")) for p in sorted((ROOT / "snapshots").glob("snap-*.json"))}
	cases = []
	for name in ("known-failures.jsonl", "labels-labeler.jsonl"):
		cases += [json.loads(line) for line in (ROOT / "labels" / name).read_text(encoding="utf-8").splitlines() if line.strip()]
	return snaps, cases


def visible(snap):
	return [t for t in snap["threads"] if t["projectId"] in snap["allowedProjects"]]


def outcome(picked, gold, listed):
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
			return "correct"
		if gold["kind"] == "one":
			return "asked_when_one"
		return "unnecessary_question"
	if gold["kind"] == "none":
		return "correct"
	if gold["kind"] == "one":
		return "said_none_when_exists"
	return "said_none_when_should_ask"


def candidate_hit(picked, gold):
	if gold["kind"] != "ask" or not gold.get("thread_ids"):
		return None
	if picked.kind != "ask":
		return None
	return bool(set(picked.candidates) & set(gold["thread_ids"]))


def one_run(case, snap, arm, client, attempt):
	threads = visible(snap)
	listed = [t["threadId"] for t in threads]
	start = time.perf_counter()
	meta = {"model": False}
	if case.get("task_record_thread_id"):
		picked = Pick.one(case["task_record_thread_id"], "task record")
	else:
		picked, meta = pick(case["utterance"], threads, snap["projects"], snap["takenAt"],
		                    client if arm in ("model", "local") else None)
	elapsed = round((time.perf_counter() - start) * 1000, 2)
	return {
		"case": case["id"], "snapshot": case["snapshot"], "slice": case["slice"], "arm": arm,
		"attempt": attempt, "utterance": case["utterance"], "gold": case["gold"],
		"pick": {"kind": picked.kind, "thread_id": picked.thread_id, "candidates": list(picked.candidates),
		         "question": picked.question, "reason": picked.reason},
		"outcome": outcome(picked, case["gold"], listed),
		"candidate_hit": candidate_hit(picked, case["gold"]),
		"latency_ms": elapsed, "model_call": bool(meta.get("model")), "raw": meta.get("raw"),
	}


def summarise(runs, arms):
	out = {}
	for arm in arms:
		rows = [r for r in runs if r["arm"] == arm]
		cases = {}
		for r in rows:
			cases.setdefault(r["case"], []).append(r)
		per_slice = {}
		for case_id, attempts in cases.items():
			sl = attempts[0]["slice"]
			bucket = per_slice.setdefault(sl, {"cases": 0, "pass": 0, "outcomes": {}, "latencies": [], "candidate_hits": [0, 0]})
			bucket["cases"] += 1
			bucket["pass"] += int(all(a["outcome"] == "correct" for a in attempts))
			for a in attempts:
				bucket["outcomes"][a["outcome"]] = bucket["outcomes"].get(a["outcome"], 0) + 1
				bucket["latencies"].append(a["latency_ms"])
				if a["candidate_hit"] is not None:
					bucket["candidate_hits"][1] += 1
					bucket["candidate_hits"][0] += int(a["candidate_hit"])
		for sl, bucket in per_slice.items():
			lat = sorted(bucket.pop("latencies"))
			bucket["p95_ms"] = lat[min(len(lat) - 1, int(round(0.95 * (len(lat) - 1))))]
			bucket["median_ms"] = statistics.median(lat)
		out[arm] = per_slice
	return out


def _merge(slices, *names):
	total = {}
	for name in names:
		for k, v in slices.get(name, {}).get("outcomes", {}).items():
			total[k] = total.get(k, 0) + v
	return total


def pass_bar(summary, arm):
	s = summary[arm]
	all_slices = list(s)
	ambiguous_bad = sum(_merge(s, "ambiguous", "nonexistent", "outside").get(k, 0)
	                    for k in ("wrong_thread", "picked_when_should_ask"))
	overall = _merge(s, *all_slices)
	all_bad = overall.get("wrong_thread", 0) + overall.get("picked_when_should_ask", 0)
	unamb = _merge(s, "exact", "paraphrase")
	unamb_correct = unamb.get("correct", 0) / max(1, sum(unamb.values()))
	unnecessary = overall.get("asked_when_one", 0) + overall.get("unnecessary_question", 0)
	total_runs = sum(sum(v["outcomes"].values()) for v in s.values())
	return {
		"zero_wrong_in_ambiguous_nonexistent_outside": ambiguous_bad == 0,
		"at_most_two_wrong_overall": all_bad <= 2,
		"unambiguous_correct_rate": round(unamb_correct, 4),
		"unambiguous_at_least_90pct": unamb_correct >= 0.90,
		"unnecessary_question_rate": round(unnecessary / max(1, total_runs), 4),
		"unnecessary_at_most_10pct": unnecessary / max(1, total_runs) <= 0.10,
		"p95_ms_by_slice": {sl: v["p95_ms"] for sl, v in s.items()},
		"p95_at_most_700ms": max(v["p95_ms"] for v in s.values()) <= 700,
	}


def main():
	parser = argparse.ArgumentParser()
	parser.add_argument("--arm", choices=["prefilter", "model", "local"], required=True)
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
	client = ModelClient() if args.arm == "model" else LocalClient() if args.arm == "local" else None
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
			(out_dir / f"{case['id']}-r{attempt}.json").write_text(json.dumps(row, indent=1, ensure_ascii=False), encoding="utf-8")
		print(f"{case['id']} {row['outcome']} {row['latency_ms']}ms", flush=True)

	summary = summarise(runs, [args.arm])
	(out_dir / "summary.json").write_text(json.dumps({"summary": summary, "pass_bar": pass_bar(summary, args.arm)}, indent=1), encoding="utf-8")
	print(json.dumps({"summary": summary, "pass_bar": pass_bar(summary, args.arm)}, indent=1))
	if client:
		print("api calls:", client.calls, "paused:", client.paused)


if __name__ == "__main__":
	main()
