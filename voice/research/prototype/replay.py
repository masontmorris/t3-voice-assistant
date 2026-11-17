"""Replay a prototype run's transcript updates through a segmenter, with no audio and no GPU.

    python -m voice.research.prototype.replay RUN_DIR --segmenter hybrid|baseline [--as-run] [--offline] --out DIR

Each CLIP.json in RUN_DIR holds, in its timeline, the transcript updates its live ASR produced. The replay feeds them
in order to a fresh segmenter and scores its task records against expected.json, as voice.research.prototype.report does.
--as-run skips the revisions the run's timeline marks as coalesced, so the replay sees what the live segmenter saw.
Without it, every revision is processed. Decider calls go through the prototype's cache under $VOICE_HOME, and
--offline refuses any request the cache lacks.

The printed table carries numbers and expected-task keys only. The records go to --out, which must be outside the
repo because they hold transcripts.
"""

import argparse
import json
from pathlib import Path

from voice.research.prototype.report import ROOT, VOICE_HOME, short_failure
from voice.prototype.segmenter import DECIDER_BUDGET, SEGMENTERS, live_calls, use_decider_cache
from voice.segmentation import client as api
from voice.segmentation.contract import TranscriptUpdate
from voice.segmentation.run import OfflineClient
from voice.segmentation.score import score


def updates(record, as_run):
    skipped = {r for e in record["timeline"] if e["kind"] == "coalesced" for r in e["revisions"]} if as_run else set()
    return [TranscriptUpdate(e["revision"], e["text"], e["is_final"]) for e in record["timeline"]
            if e["kind"] == "transcript_update" and e["revision"] not in skipped]


def grade(expected, records, dispatch_log):
    """The prototype report's scoring: orchestrator tasks only, clarification from the records."""
    orchestrated = [t for t in records if "orchestrator" in t["sources"]]
    ids = {t["task_id"] for t in orchestrated}
    unclear = [t["instruction"] for t in records if t["status"] == "needs_clarification"]
    return score(expected, [{**t, "sources": ["orchestrator"]} for t in orchestrated],
                 [d for d in dispatch_log if d["task_id"] in ids], {"clarification_needed": unclear or None})


def replay(record, segmenter_name, decider, as_run):
    stem = Path(record["clip"]).stem
    segmenter = SEGMENTERS[segmenter_name](decider, stem)
    before, points = live_calls(), 0
    for update in updates(record, as_run):
        points += segmenter.update(update) is not None
    return stem, {"records": segmenter.records(), "dispatch_log": segmenter.dispatch_log, "decision_points": points,
                  "calls": sum(len(p["calls"]) for p in getattr(segmenter, "points", [])) or None,
                  "live_calls": live_calls() - before}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("run", type=Path)
    parser.add_argument("--segmenter", choices=sorted(SEGMENTERS), required=True)
    parser.add_argument("--as-run", action="store_true")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--only", nargs="*", help="clip stems to replay")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    use_decider_cache(VOICE_HOME / "evidence" / "prototype" / "decider-cache", DECIDER_BUDGET)
    decider = OfflineClient() if args.offline else api.Client()
    expected = json.loads((ROOT / "expected.json").read_text(encoding="utf-8"))
    args.out.mkdir(parents=True, exist_ok=True)
    rows = []
    for path in sorted(args.run.glob("*.json")):
        if path.name == "summary.json" or (args.only and path.stem not in args.only):
            continue
        record = json.loads(path.read_text(encoding="utf-8"))
        if "error" in record:
            continue
        stem, result = replay(record, args.segmenter, decider, args.as_run)
        graded = grade(expected[stem], result["records"], result["dispatch_log"])
        early = [d for d in result["dispatch_log"] if not d["is_final"]]
        side = {t["task_id"] for t in result["records"] if t["side_effect"]}
        same = result["records"] == record["tasks"] and result["dispatch_log"] == record["dispatch_log"]
        row = {"clip": stem, "pass": graded["pass"], "failures": [short_failure(f) for f in graded["failures"]],
               "premature": graded["premature_dispatches"], "duplicate": graded["duplicate_dispatches"],
               "missed_corrections": graded["missed_corrections"], "decision_points": result["decision_points"],
               "calls": result["calls"], "live_calls": result["live_calls"], "early_dispatches": len(early),
               "side_effect_early": sum(d["task_id"] in side for d in early), "same_as_run": same}
        rows.append(row)
        (args.out / f"{stem}.json").write_text(json.dumps({**row, **result}, indent=1) + "\n", encoding="utf-8")
    (args.out / "summary.json").write_text(json.dumps(rows, indent=1) + "\n", encoding="utf-8")
    print("| Clip | Tasks | Failures | Premature | Decision points | Calls | Early dispatches | Same as run |")
    print("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for r in rows:
        print(f"| {r['clip']} | {'pass' if r['pass'] else 'FAIL'} | {'; '.join(r['failures']) or '-'} | "
              f"{r['premature']} | {r['decision_points']} | {r['calls'] if r['calls'] is not None else '-'} | "
              f"{r['early_dispatches']} | {'yes' if r['same_as_run'] else 'no'} |")
    print(f"\nPass {sum(r['pass'] for r in rows)} of {len(rows)}. Live decider calls {sum(r['live_calls'] for r in rows)}.")


if __name__ == "__main__":
    main()
