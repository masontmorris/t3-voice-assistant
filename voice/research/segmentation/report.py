"""Derive every number in docs/voice/stage2-segmentation.md from committed files. No network.

Only round 2's ledger entries and cache files count. docs/voice/stage2-variants.md has its own report.

    python -m voice.research.segmentation.report
"""

import json
import statistics
from collections import Counter

from voice.segmentation.client import CACHE, LEDGER
from voice.segmentation.run import RESULTS

RUNS = ["final-a", "final-b", "final-c", "stream-a", "final-a-separate", "final-a-repeat1", "stream-a-repeat1"]
PAIRS = [("final-a", "final-a-repeat1", "same request sent twice"),
         ("stream-a", "stream-a-repeat1", "same request sent twice"),
         ("final-a", "final-a-separate", "all segments in one call vs one segment per call")]


ROUND2_ATTEMPTS = 157


def round2_entries():
    return [json.loads(line) for line in LEDGER.read_text(encoding="utf-8").splitlines()][:ROUND2_ATTEMPTS]


def round2_cache():
    """Cache files for the requests round 2 sent. Later rounds share the cache directory."""
    hashes = {e["hash"] for e in round2_entries()}
    return [p for p in CACHE.glob("*.json") if p.stem.split("-", 1)[1] in hashes]


def pct(values, q):
    values = sorted(values)
    return values[min(len(values) - 1, round(q * (len(values) - 1)))]


def dist(values):
    return {"n": len(values), "median": round(statistics.median(values), 1), "p95": round(pct(values, 0.95), 1),
            "min": round(min(values), 1), "max": round(max(values), 1)}


def load(run, scenario=None):
    if scenario:
        return json.loads((RESULTS / run / f"{scenario}.json").read_text(encoding="utf-8"))
    return json.loads((RESULTS / run / "summary.json").read_text(encoding="utf-8"))


def run_table():
    rows = []
    for run in RUNS:
        summary = load(run)
        rows.append({"run": run, "pass": sum(r["pass"] for r in summary), "scenarios": len(summary),
                     "revisions": sum(r["revisions"] for r in summary),
                     "decision_points": sum(r["decision_points"] for r in summary),
                     "calls": sum(r["calls"] for r in summary), "premature": sum(r["premature"] for r in summary),
                     "duplicate": sum(r["duplicate"] for r in summary),
                     "missed_corrections": sum(r["missed_corrections"] for r in summary),
                     "malformed": sum(r["problems"] for r in summary)})
    return rows


def answers_by_point(run):
    """Map (scenario, revision, segment texts, question) to the chosen answer."""
    out = {}
    for summary in load(run):
        result = load(run, summary["scenario"])
        for point in result["decision_points"]:
            texts = tuple(s["text"] for s in point["segments"])
            for sid, d in point["decisions"].items():
                for field in ("kind", "readiness", "depends_on", "corrects", "side_effect"):
                    out[(summary["scenario"], point["revision"], texts, sid, field)] = json.dumps(d[field])
    return out


def agreement(a, b):
    x, y = answers_by_point(a), answers_by_point(b)
    shared = sorted(set(x) & set(y))
    by_field = Counter(k[-1] for k in shared)
    diff = [k for k in shared if x[k] != y[k]]
    return {"questions_compared": len(shared), "differ": len(diff),
            "differ_by_field": dict(Counter(k[-1] for k in diff)), "compared_by_field": dict(by_field),
            "examples": [f"{k[0]} r{k[1]} {k[3]}.{k[4]}: {x[k]} vs {y[k]}" for k in diff[:6]]}


def margins():
    """Margin (top minus second probability) of every recorded choice answer, by question field."""
    fields = {}
    for path in round2_cache():
        if not path.name.startswith("decisions-"):
            continue
        record = json.loads(path.read_text(encoding="utf-8"))
        for name, answer in ((record.get("response") or {}).get("answers") or {}).items():
            if answer.get("type") == "choice" and "_" in name:
                fields.setdefault(name.split("_", 1)[1], []).append(answer["confidence"])
    return {f: {"n": len(v), "below_0.2": sum(m < 0.2 for m in v), "median": round(statistics.median(v), 2)}
            for f, v in sorted(fields.items())}


def latency():
    records = [json.loads(p.read_text(encoding="utf-8")) for p in round2_cache()]
    live = [r for r in records if r["status"] == 200]
    by_route = {route: dist([r["timing"]["ms"] for r in live if r["route"] == route])
                for route in sorted({r["route"] for r in live})}
    warm = [r["timing"]["ms"] for r in live if r["timing"]["connect_ms"] is None and r["route"] == "decisions"]
    tokens = [(r["response"]["usage"]["input_tokens"], r["timing"]["ms"]) for r in live if r["route"] == "decisions"]
    small = [ms for t, ms in tokens if t < 1000]
    large = [ms for t, ms in tokens if t >= 1500]
    questions = [(len(r["request"]["questions"]), r["timing"]["ms"]) for r in live if r["route"] == "decisions"]
    floor = [ms for line in (RESULTS / "floor.jsonl").read_text(encoding="utf-8").splitlines()
             for ms in json.loads(line)["ms"]]
    connects = [r["timing"]["connect_ms"] for r in live if r["timing"]["connect_ms"] is not None]
    costs = Counter((r["response"].get("usage") or {}).get("cost") for r in live)
    return {"by_route": by_route, "decisions_warm": dist(warm), "floor_key_get": dist(floor),
            "connect_ms": dist(connects + [json.loads(l)["connect_ms"] for l in
                                           (RESULTS / "floor.jsonl").read_text(encoding="utf-8").splitlines()]),
            "decisions_under_1000_tokens": dist(small) if small else None,
            "decisions_1500_tokens_or_more": dist(large) if large else None,
            "input_tokens": dist([t for t, _ in tokens]),
            "questions_per_call": dist([q for q, _ in questions]), "usage_cost_values": dict(costs),
            "first": min(r["timing"]["started_at"] for r in live),
            "last": max(r["timing"]["started_at"] for r in live)}


def call_ms(run):
    out = {}
    for summary in load(run):
        for point in load(run, summary["scenario"])["decision_points"]:
            for call in point["calls"]:
                out[call["label"]] = call["ms"]
    return out


def run_latency():
    per_run = {run: dist(list(call_ms(run).values())) for run in RUNS}
    a, b = call_ms("stream-a"), call_ms("stream-a-repeat1")
    paired = [b[k] - a[k] for k in a if k in b]
    return {"per_run": per_run, "stream_repeat_paired_diff": dist(paired)}


def largest_choice_set():
    sizes = [len(q["criteria"]) for path in round2_cache() if path.name.startswith("decisions-")
             for q in json.loads(path.read_text(encoding="utf-8"))["request"]["questions"].values()
             if q["type"] == "choice"]
    return max(sizes)


def ledger():
    entries = round2_entries()
    return {"attempts": len(entries), "by_route": dict(Counter(e["route"] for e in entries)),
            "status": dict(Counter(str(e["status"]) for e in entries)),
            "errors": dict(Counter(e["error"] for e in entries if e["error"])),
            "distinct_requests": len({e["hash"] for e in entries}),
            "cache_files": len(round2_cache())}


def main():
    report = {"runs": run_table(), "agreement": [{"a": a, "b": b, "what": w, **agreement(a, b)} for a, b, w in PAIRS],
              "margins": margins(), "latency": latency(), "run_latency": run_latency(),
              "largest_choice_set": largest_choice_set(), "ledger": ledger()}
    (RESULTS / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
