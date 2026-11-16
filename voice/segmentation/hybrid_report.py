"""Derive every number in docs/voice/stage2-hybrid.md. No network.

    python -m voice.segmentation.hybrid_report

The scenario sections read committed files only. The old score is the one each run recorded, from the scorer that
matched on segment text. The new score rescores the same task records with the current scorer, which matches on the
instruction. The clips section reads the prototype's run records under $VOICE_HOME when they exist, and prints
numbers and expected-task keys only.
"""

import json
import os
import statistics
from collections import Counter
from pathlib import Path

from voice.segmentation.client import CACHE, LEDGER
from voice.segmentation.report import dist
from voice.segmentation.run import RESULTS
from voice.segmentation.score import score
from voice.segmentation.variants_report import boundary_errors, results, run_files

SYSTEMS = {"baseline": "stream-a", "e": "stream-e", "hybrid": "stream-h"}
SETS = {"original": "", "heldout": "-heldout"}
ROUND_START = 157 + 363
VOICE_HOME = Path(os.environ.get("VOICE_HOME", Path.home() / "voice"))
REAL = [f"{i:02d}_{n}" for i, n in enumerate(["single", "two_requests", "dependent", "correction", "topic_change",
                                              "not_a_request", "implicit", "cancel", "run_on", "noisy"], 1)]
CLIP_RUNS = {"baseline": VOICE_HOME / "evidence" / "prototype" / "run1-c",
             "hybrid": VOICE_HOME / "evidence" / "hybrid" / "run-h-c"}


def seconds(values):
    values = [v for v in values if v is not None]
    if not values:
        return None
    return {"n": len(values), "median": round(statistics.median(values), 2), "min": round(min(values), 2),
            "max": round(max(values), 2)}


def rescore(result):
    return score(result["expected"], result["tasks"], result["dispatch_log"], result["snapshot"])


def run_summary(run, set_name):
    rs = results(run)
    new = {r["scenario"]: rescore(r) for r in rs}
    files = sorted({f for r in rs for f in run_files(r)})
    timings = [json.loads((CACHE / f).read_text(encoding="utf-8"))["timing"]["ms"] for f in files]
    breaks = boundary_errors(run, set_name)
    return {
        "run": run, "scenarios": len(rs),
        "pass_old": sum(r["score"]["pass"] for r in rs), "pass_new": sum(s["pass"] for s in new.values()),
        "premature": sum(s["premature_dispatches"] for s in new.values()),
        "duplicate": sum(s["duplicate_dispatches"] for s in new.values()),
        "missed_corrections": sum(s["missed_corrections"] for s in new.values()),
        "early_dispatches": sum(not d["is_final"] for r in rs for d in r["dispatch_log"]),
        "calls": sum(len(run_files(r)) for r in rs),
        "calls_per_utterance": round(sum(len(run_files(r)) for r in rs) / len(rs), 2),
        "malformed": sum(len(p["problems"]) for r in rs for p in r["decision_points"]),
        "latency_ms": dist(timings),
        "breaks": {k: breaks[k] for k in ("labeled_breaks", "exact", "off_by_1_to_3", "missed", "extra_cuts")},
        "per_scenario": {r["scenario"]: {"old": r["score"]["pass"], "new": new[r["scenario"]]["pass"],
                                         "failures": new[r["scenario"]]["failures"],
                                         "premature": new[r["scenario"]]["premature_dispatches"],
                                         "calls": len(run_files(r)),
                                         "early": sum(not d["is_final"] for d in r["dispatch_log"]),
                                         "break_error": breaks["per_scenario"][r["scenario"]]["error"]}
                         for r in rs},
    }


def hybrid_points(run):
    """How the hybrid's decision points split by kind, and how often a final transcript needed a drain."""
    kinds, drains = Counter(), 0
    for r in results(run):
        kinds.update(p["kind"] for p in r["decision_points"])
        drains += any(p["kind"] == "drain" for p in r["decision_points"])
    return {"points_by_kind": dict(kinds), "utterances_with_a_drain": drains}


def ledger():
    entries = [json.loads(line) for line in LEDGER.read_text(encoding="utf-8").splitlines()][ROUND_START:]
    return {"attempts_this_round": len(entries), "status": dict(Counter(str(e["status"]) for e in entries)),
            "first": entries[0]["at"] if entries else None, "last": entries[-1]["at"] if entries else None}


def clip_points(record):
    return [e for e in record["timeline"] if e["kind"] == "decision_point"]


def clips():
    """Mason's ten clips: baseline pass 1 of the prototype round against the hybrid's run, same transcripts."""
    if not all(d.is_dir() for d in CLIP_RUNS.values()):
        return None
    from voice.prototype.replay import grade
    from voice.prototype.report import final_decision
    expected = json.loads((Path(__file__).resolve().parents[1] / "prototype" / "expected.json").read_text("utf-8"))
    out = {}
    for name, run in CLIP_RUNS.items():
        rows = {}
        for stem in REAL:
            record = json.loads((run / f"{stem}.json").read_text(encoding="utf-8"))
            graded = grade(expected[stem], record["tasks"], record["dispatch_log"])
            final = final_decision(record)
            side = {t["task_id"] for t in record["tasks"] if t["side_effect"]}
            rows[stem] = {"pass": graded["pass"], "failures": len(graded["failures"]),
                          "premature": graded["premature_dispatches"],
                          "early_dispatches": sum(not d["is_final"] for d in record["dispatch_log"]),
                          "side_effect_early": sum(not d["is_final"] and d["task_id"] in side
                                                   for d in record["dispatch_log"]),
                          "calls": sum(len(p["calls"]) for p in clip_points(record)),
                          "live_calls": record["metrics"]["decider_live_calls"],
                          "coalesced": record["metrics"].get("coalesced_revisions", 0),
                          "snapshot_ready_s": round(record["metrics"]["snapshot_ready_after_audio_end_s"], 3),
                          "first_reply_audio_s": round(record["metrics"]["first_reply_audio_after_audio_end_s"], 3),
                          "final_live": bool(final and final["live"]),
                          "final_wait_s": round(final["decide_s"] - final["call_ms"] / 1000, 3) if final else None}
        live = [r["snapshot_ready_s"] for r in rows.values() if r["final_live"]]
        out[name] = {"pass": sum(r["pass"] for r in rows.values()),
                     "premature": sum(r["premature"] for r in rows.values()),
                     "early_dispatches": sum(r["early_dispatches"] for r in rows.values()),
                     "side_effect_early": sum(r["side_effect_early"] for r in rows.values()),
                     "calls": sum(r["calls"] for r in rows.values()),
                     "coalesced": sum(r["coalesced"] for r in rows.values()),
                     "snapshot_ready_s": seconds([r["snapshot_ready_s"] for r in rows.values()]),
                     "snapshot_ready_final_live_s": seconds(live),
                     "final_wait_s": seconds([r["final_wait_s"] for r in rows.values() if r["final_live"]]),
                     "first_reply_audio_s": seconds([r["first_reply_audio_s"] for r in rows.values()]),
                     "per_clip": rows}
    replays = {}
    for name in ("replay-h-asrun", "replay-h-every"):
        path = VOICE_HOME / "evidence" / "hybrid" / name / "summary.json"
        if path.exists():
            rows = [r for r in json.loads(path.read_text(encoding="utf-8")) if r["clip"] in REAL]
            replays[name] = {"pass": sum(r["pass"] for r in rows), "same_as_run": sum(r["same_as_run"] for r in rows),
                             "decision_points": sum(r["decision_points"] for r in rows)}
    out["replays"] = replays
    return out


def main():
    report = {"sets": {s: {name: run_summary(run + suffix, s) for name, run in SYSTEMS.items()}
                       for s, suffix in SETS.items()},
              "hybrid_points": {s: hybrid_points("stream-h" + suffix) for s, suffix in SETS.items()},
              "ledger": ledger(), "clips": clips()}
    (RESULTS / "hybrid-report.json").write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    for s, systems in report["sets"].items():
        print(f"\n## {s}")
        for name, r in systems.items():
            print(f"{name:9} pass old {r['pass_old']} new {r['pass_new']} of {r['scenarios']} | premature "
                  f"{r['premature']} dup {r['duplicate']} missed {r['missed_corrections']} early {r['early_dispatches']}"
                  f" | calls {r['calls']} ({r['calls_per_utterance']}/utt) | breaks {r['breaks']} | "
                  f"median ms {r['latency_ms'].get('median') if r['latency_ms'] else None}")
    print(json.dumps(report["hybrid_points"]), json.dumps(report["ledger"]))
    if report["clips"]:
        for name, c in report["clips"].items():
            if name != "replays":
                print(name, {k: v for k, v in c.items() if k != "per_clip"})
        print(report["clips"]["replays"])


if __name__ == "__main__":
    main()
