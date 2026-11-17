"""Replay the hybrid's recorded decision requests through Liquid's hosted d1.2 and score the answers against pplx's.

    python -m voice.research.liquid_decider.replay [--tag hybrid] [--out DIR]

The hybrid made 94 decisions calls on the 11 original scenarios, the 18 held-out scenarios and the 10 clips, and every
one of those request bodies is in a pplx cache record with its answer. This sends each body again, once, in call order,
and reports latency, what the calls cost, and how far d1.2's answers are from pplx's on the same questions. A noul
answer agrees when both are on the same side of 0.5; a choice answer agrees when both chose the same option.

The requests hold Mason's transcripts, so the rows stay under $VOICE_HOME and never in the repo. The run re-checks that
the hosted `confidence` is the margin the gate expects, from `probabilities` where an answer carries them.
"""

import argparse
import json
import re
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from voice.liquid_decider.client import LiquidClient, OUT
from voice.research.local_decider import bench

GROUPS = ("kind", "ready", "side_effect", "depends", "corrects", "finished", "at", "same_as")
SUFFIX = re.compile(r"_(kind|ready|side_effect|depends|corrects|finished|at)$")
SCALE_TOLERANCE = 1e-6


def group_of(name):
    match = SUFFIX.search(name)
    if match:
        return match.group(1)
    return "same_as" if name.endswith("_same_as") else name.rsplit("_", 1)[-1]


def answers_agree(name, ours, theirs):
    """Whether two answers to one question carry the same decision."""
    if not isinstance(ours, dict) or not isinstance(theirs, dict):
        return None
    if group_of(name) == "same_as" or "choice" in (ours.get("type"), theirs.get("type")):
        return ours.get("choice") == theirs.get("choice")
    ours_noul, theirs_noul = ours.get("noul"), theirs.get("noul")
    if isinstance(ours_noul, (int, float)) and isinstance(theirs_noul, (int, float)):
        return (ours_noul >= 0.5) == (theirs_noul >= 0.5)
    return None


def margin(probabilities):
    if not isinstance(probabilities, dict) or len(probabilities) < 2:
        return None
    values = list(probabilities.values())
    if not all(isinstance(value, (int, float)) for value in values):
        return None
    chance = 1 / len(values)
    return (max(values) - chance) / (1 - chance) if chance < 1 else None


def scale_checks(response):
    """Per answer with probabilities: the margin, the reported confidence, and whether they are the same number."""
    checks = []
    for name, answer in (response.get("answers") or {}).items():
        if not isinstance(answer, dict):
            continue
        expected = margin(answer.get("probabilities"))
        confidence = answer.get("confidence")
        if expected is None or not isinstance(confidence, (int, float)):
            continue
        checks.append({"name": name, "confidence": confidence, "margin": expected,
                       "same": abs(confidence - expected) <= SCALE_TOLERANCE})
    return checks


def percentile(values, share):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(share * (len(ordered) - 1))))] if ordered else None


def rows_for(client, limit=None):
    sequences = bench.sequences()
    files = [name for sequence in sequences.values() for calls in sequence.values() for name in calls]
    if limit:
        files = files[:limit]
    rows = []
    for index, name in enumerate(files, 1):
        recorded = bench.find(name)
        pplx = (recorded.get("response") or {}).get("answers") or {}
        record = client.call("decisions", recorded["request"], label=f"replay:{name}", fresh=True)
        response = record.get("response") if isinstance(record.get("response"), dict) else {}
        ours = response.get("answers") or {}
        usage = response.get("usage") or {}
        agree = {question: answers_agree(question, ours.get(question), pplx.get(question)) for question in pplx}
        rows.append({"n": index, "file": name, "pplx_label": recorded.get("label"),
                     "pplx_ms": (recorded.get("timing") or {}).get("ms"),
                     "ms": record.get("timing", {}).get("ms"), "status": record.get("status"),
                     "questions": len(recorded["request"]["questions"]),
                     "input_tokens": usage.get("input_tokens"), "cost": usage.get("cost"),
                     "agree": agree, "pplx": {k: bench.compact({"a": v})["a"] for k, v in pplx.items()},
                     "ours": {k: bench.compact({"a": v})["a"] for k, v in ours.items()},
                     "scale": scale_checks(response)})
        print(f"{index:>3}/{len(files)} {name:<34} {record.get('status')} "
              f"{record.get('timing', {}).get('ms', 0):>6.0f} ms  {len(agree):>2} questions", flush=True)
    return rows


def summarise(rows):
    agree, counts = defaultdict(int), defaultdict(int)
    for row in rows:
        for question, same in row["agree"].items():
            if same is None:
                continue
            counts[group_of(question)] += 1
            agree[group_of(question)] += bool(same)
    groups = {group: {"answers": counts[group], "agreement": agree[group] / counts[group]}
              for group in GROUPS if counts[group]}
    total = sum(counts.values())
    exact = sum(all(same for same in row["agree"].values() if same is not None) for row in rows)
    latencies = [float(row["ms"]) for row in rows if row["ms"] is not None]
    buckets = defaultdict(list)
    for row in rows:
        if row["ms"] is None:
            continue
        size = row["questions"]
        buckets["1-4" if size <= 4 else "5-8" if size <= 8 else "9+"].append(float(row["ms"]))
    checks = [check for row in rows for check in row["scale"]]
    return {"calls": len(rows), "failed": sum(row["status"] != 200 for row in rows),
            "questions": total, "agreement": sum(agree.values()) / total if total else 0.0,
            "groups": groups, "exact_requests": exact,
            "latency_ms": {"median": statistics.median(latencies), "p95": percentile(latencies, 0.95),
                           "max": max(latencies), "buckets": {name: {"calls": len(values),
                                                                     "median": statistics.median(values)}
                                                              for name, values in sorted(buckets.items())}},
            "input_tokens": sum(int(row["input_tokens"] or 0) for row in rows),
            "cost_usd": sum(float(row["cost"] or 0.0) for row in rows),
            "scale": {"answers": len(checks), "same": sum(check["same"] for check in checks),
                      "worst_gap": max((abs(check["confidence"] - check["margin"]) for check in checks),
                                       default=0.0)}}


def report(summary, rows):
    print(f"\n{summary['calls']} recorded hybrid requests replayed, {summary['failed']} failed")
    print(f"{'question':12}{'answers':>9}{'agreement':>11}")
    for group, row in summary["groups"].items():
        print(f"{group:12}{row['answers']:>9}{row['agreement']:>10.1%}")
    print(f"{'all':12}{summary['questions']:>9}{summary['agreement']:>10.1%}")
    print(f"\nrequests whose every answer matched: {summary['exact_requests']}/{summary['calls']}")
    latency = summary["latency_ms"]
    print(f"latency: median {latency['median']:.0f} ms, p95 {latency['p95']:.0f} ms, max {latency['max']:.0f} ms")
    for name, row in latency["buckets"].items():
        print(f"  {name:>4} questions: {row['calls']:>2} calls, median {row['median']:.0f} ms")
    print(f"input tokens {summary['input_tokens']:,}, cost ${summary['cost_usd']:.5f}")
    scale = summary["scale"]
    print(f"confidence scale check: {scale['same']}/{scale['answers']} answers report pplx's margin exactly, "
          f"worst gap {scale['worst_gap']:.2e}")

def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tag", default="hybrid")
    parser.add_argument("--out", type=Path, default=None, help="where rows and the summary go (default: evidence)")
    parser.add_argument("--limit", type=int, help="only the first N requests, for a smoke test")
    args = parser.parse_args()
    out = (args.out or OUT / "replay-hybrid") / args.tag
    out.mkdir(parents=True, exist_ok=True)
    client = LiquidClient()
    rows = rows_for(client, args.limit)
    summary = summarise(rows)
    summary["at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    summary["model"] = "d1.2"
    (out / "rows.jsonl").write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    (out / "summary.json").write_text(json.dumps(summary, indent=1) + "\n", encoding="utf-8")
    report(summary, rows)
    print(f"rows and summary: {out}")


if __name__ == "__main__":
    main()
