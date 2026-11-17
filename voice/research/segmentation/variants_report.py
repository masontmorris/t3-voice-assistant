"""Derive every number in docs/voice/stage2-variants.md from committed files. No network.

    python -m voice.research.segmentation.variants_report
"""

import json
import statistics
from collections import Counter

from voice.segmentation.client import CACHE, LEDGER
from voice.segmentation.cursor import markers, words
from voice.research.segmentation.report import dist
from voice.segmentation.run import RESULTS, SETS
from voice.segmentation.score import score

ROOT = RESULTS.parent
RUNS = {"stream-a": "original", "stream-e-v1": "original", "stream-e": "original",
        "stream-a-heldout": "heldout", "stream-e-heldout": "heldout"}
ROUND_START = 157
NEAR = 3


def scenarios(name):
    return {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in sorted(SETS[name].glob("*.json"))}


def results(run):
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted((RESULTS / run).glob("*.json"))
            if p.name != "summary.json"]


def labels(name):
    return json.loads((ROOT / "labels" / f"{name}.json").read_text(encoding="utf-8"))["breaks"]


def final_text(scenario):
    return [e for e in scenario["events"] if e["type"] == "transcript"][-1]["text"]


def cuts(result, text):
    """Word indices where a later segment starts, in the final transcript."""
    ws = words(text)
    if result["option"] == "e":
        return [s["w0"] for s in result["segments"][1:]]
    final = [p for p in result["decision_points"] if p["is_final"] and "segments" in p][-1]["segments"]
    return [next((i for i, w in enumerate(ws) if w.start >= s["start"]), len(ws)) for s in final[1:]]


def boundary_errors(run, set_name):
    scen, truth = scenarios(set_name), labels(set_name)
    exact = near = missed = extra = 0
    errors, rows = [], {}
    for result in results(run):
        sid = result["scenario"]
        found = cuts(result, final_text(scen[sid]))
        row = []
        for lo, hi in truth[sid]:
            d = min((0 if lo <= c <= hi else min(abs(c - lo), abs(c - hi)) for c in found), default=None)
            if d is None or d > NEAR:
                missed += 1
                row.append(None)
            else:
                exact += d == 0
                near += d > 0
                errors.append(d)
                row.append(d)
        extra += sum(all(min(abs(c - lo), abs(c - hi)) > NEAR and not lo <= c <= hi for lo, hi in truth[sid])
                     for c in found)
        rows[sid] = {"cuts": found, "labels": truth[sid], "error": row}
    total = exact + near + missed
    return {"labeled_breaks": total, "exact": exact, "off_by_1_to_3": near, "missed": missed,
            "mean_error_words_when_found": round(statistics.mean(errors), 2) if errors else None,
            "extra_cuts": extra, "per_scenario": rows}


def run_files(result):
    return [c["file"] for p in result["decision_points"] for c in p["calls"]]


def run_summary(run, set_name):
    rs = results(run)
    files = sorted({f for r in rs for f in run_files(r)})
    timings = [json.loads((CACHE / f).read_text(encoding="utf-8"))["timing"]["ms"] for f in files]
    rescored = [score(r["expected"], [{**t, "segment_text": t["instruction"]} for t in r["tasks"]],
                      r["dispatch_log"], r["snapshot"])["pass"] for r in rs]
    return {"run": run, "set": set_name, "scenarios": len(rs), "pass": sum(r["score"]["pass"] for r in rs),
            "pass_if_matched_on_instruction": sum(rescored),
            "passed": [r["scenario"] for r in rs if r["score"]["pass"]],
            "premature": sum(r["score"]["premature_dispatches"] for r in rs),
            "duplicate": sum(r["score"]["duplicate_dispatches"] for r in rs),
            "missed_corrections": sum(r["score"]["missed_corrections"] for r in rs),
            "early_dispatches": sum(not d["is_final"] for r in rs for d in r["dispatch_log"]),
            "revisions": sum(r["revisions"] for r in rs),
            "calls": sum(len(run_files(r)) for r in rs), "distinct_requests": len(files),
            "calls_per_utterance": round(sum(len(run_files(r)) for r in rs) / len(rs), 2),
            "malformed": sum(len(p["problems"]) for r in rs for p in r["decision_points"]),
            "latency_ms": dist(timings), "failures": {r["scenario"]: r["score"]["failures"] for r in rs
                                                     if not r["score"]["pass"]},
            }


def cursor_behaviour(run):
    """Variant E only: verdicts, how commits happened, and how often a finished answer flips on the next revision."""
    verdicts, how, flips, finished_then = Counter(), Counter(), 0, 0
    at_margins = []
    for result in results(run):
        points = [p for p in result["decision_points"] if "verdict" in p]
        for p in points:
            verdicts[p["verdict"]] += 1
            if p["answer"]:
                at_margins.append(p["answer"]["at"]["margin"])
            if p["committed"]:
                how["final" if p["is_final"] else "pause" if p["pause"] else "held two revisions"] += 1
        stream = [p for p in points if "drain" not in p["label"]]
        for a, b in zip(stream, stream[1:]):
            if a["answer"] and b["answer"] and a["cursor"] == b["cursor"] and a["answer"]["finished"] >= 0.5:
                finished_then += 1
                flips += b["answer"]["finished"] < 0.5
    return {"verdicts": dict(verdicts), "commits_by_rule": dict(how),
            "finished_then_next_revision": finished_then, "finished_flipped_to_unfinished": flips,
            "at_margin_below_0.2": sum(m < 0.2 for m in at_margins), "at_answers": len(at_margins)}


def extra_questions():
    pairs = json.loads((RESULTS / "extra-questions.json").read_text(encoding="utf-8"))
    changed, compared, noul_shift = [], 0, []
    for p in pairs:
        for name, a in p["answers"].items():
            compared += 1
            if "base_margin" in a:
                if a["base"] != a["extra"]:
                    changed.append(f"{p['label']} {name}: {a['base']} -> {a['extra']}")
            else:
                noul_shift.append(abs(a["base"] - a["extra"]))
                if (a["base"] >= 0.5) != (a["extra"] >= 0.5):
                    changed.append(f"{p['label']} {name}: {a['base']:.2f} -> {a['extra']:.2f}")
    diffs = [p["extra_ms"] - p["base_ms"] for p in pairs]
    return {"pairs": len(pairs), "answers_compared": compared, "answers_changed": len(changed), "changed": changed,
            "noul_abs_shift": dist(noul_shift), "base_ms": dist([p["base_ms"] for p in pairs]),
            "extra_ms": dist([p["extra_ms"] for p in pairs]), "paired_diff_ms": dist(diffs),
            "questions": {"base": dist([p["base_questions"] for p in pairs]),
                          "extra": dist([p["extra_questions"] for p in pairs])},
            "added_answers": [p["added"] for p in pairs]}


def marker_recall():
    """Share of labeled breaks with an E marker inside the label range, on final text and with punctuation stripped."""
    out = {}
    for set_name in SETS:
        scen, truth = scenarios(set_name), labels(set_name)
        for style in ("punctuated", "stripped"):
            hit = total = 0
            for sid, ranges in truth.items():
                text = final_text(scen[sid])
                if style == "stripped":
                    text = " ".join(w.text for w in words(text))
                gaps = set(markers(text, words(text), 0))
                for lo, hi in ranges:
                    total += 1
                    hit += any(lo <= g <= hi for g in gaps)
            out[f"{set_name}_{style}"] = {"breaks": total, "with_marker": hit}
    return out


def ledger():
    entries = [json.loads(line) for line in LEDGER.read_text(encoding="utf-8").splitlines()][ROUND_START:]
    costs = Counter()
    for path in CACHE.glob("*.json"):
        record = json.loads(path.read_text(encoding="utf-8"))
        if record["status"] == 200:
            costs[(record["response"].get("usage") or {}).get("cost")] += 1
    return {"attempts_this_round": len(entries), "status": dict(Counter(str(e["status"]) for e in entries)),
            "errors": dict(Counter(e["error"] for e in entries if e["error"])),
            "first": entries[0]["at"], "last": entries[-1]["at"], "usage_cost_values_all_cache": dict(costs)}


def main():
    report = {"runs": [run_summary(run, s) for run, s in RUNS.items()],
              "boundaries": {run: boundary_errors(run, s) for run, s in RUNS.items()},
              "cursor": {run: cursor_behaviour(run) for run in RUNS if "-e" in run},
              "extra_questions": extra_questions(), "marker_recall": marker_recall(), "ledger": ledger()}
    (RESULTS / "variants-report.json").write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "boundaries"}, indent=1, default=str))
    print(json.dumps({run: {k: v for k, v in b.items() if k != "per_scenario"} for run, b in report["boundaries"].items()},
                     indent=1))


if __name__ == "__main__":
    main()
