"""Compare a run with the decider call at the end of the utterance against a run with the handoff to LFM3.

    python -m voice.prototype.eou_report BEFORE_DIR AFTER_DIR [--grades GRADES.json]

Each directory holds one `voice.prototype.run` pass over the same clips. The printed tables carry numbers and
expected-task keys only, never transcript text, so they can go into the public doc. GRADES.json, when given, holds a
reader's grade of every spoken reply under {"before"|"after": {clip: {"invents": bool, "every_status": bool,
"held_claimed_done": bool}}}. It stays local, beside the runs, because the grader's notes quote the replies.
"""

import argparse
import json
import math
import statistics
from pathlib import Path

from voice.prototype.report import ROOT
from voice.segmentation.cursor import words
from voice.segmentation.score import _matches, score

TIMED = ("final_transcript_after_audio_end_s", "snapshot_ready_after_audio_end_s", "first_reply_audio_after_audio_end_s")


def load(run):
    return {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in sorted(run.glob("*.json"))
            if p.name != "summary.json"}


def p95(values):
    values = sorted(values)
    return values[math.ceil(0.95 * len(values)) - 1]


def spread(values):
    values = [v for v in values if v is not None]
    if not values:
        return "n/a"
    return (f"{statistics.median(values):.2f} / {p95(values):.2f} / {max(values):.2f} (n={len(values)})")


def points(record):
    """Successful decision points as (completion time, words read, is_final)."""
    sizes = {e["revision"]: len(words(e["text"])) for e in record["timeline"] if e["kind"] == "transcript_update"}
    return [(e["t"], sizes[e["revision"]], e["is_final"]) for e in record["timeline"]
            if e["kind"] == "decision_point" and e["calls"] and all(c["status"] == 200 for c in e["calls"])]


def lags(record, partial_only):
    """Seconds from each word's first partial to the first decision point that read it. Words no point read are
    left out."""
    updates = [(e["t"], len(words(e["text"]))) for e in record["timeline"] if e["kind"] == "transcript_update"]
    covering = [(t, n) for t, n, final in points(record) if not (partial_only and final)]
    out = []
    for i in range(updates[-1][1]):
        seen = next(t for t, n in updates if n > i)
        read = next((t for t, n in covering if n > i and t >= seen), None)
        if read is not None:
            out.append(read - seen)
    return out


def tail_words(record):
    """Words of the final transcript that no decision point on a partial read."""
    final = len(words(record["final_transcript"]))
    return final - max((n for _, n, is_final in points(record) if not is_final), default=0)


def graded_records(record):
    """Every task. A side effect held at the end of the utterance scores as a final dispatch, because it ends as
    held_for_confirmation, as a dispatched side effect does. A held read-only task scores as undispatched."""
    records, log = [], list(record["dispatch_log"])
    for t in record["tasks"]:
        if t["status"] == "held" and not t["side_effect"]:
            t = {**t, "status": "ready"}
        elif t["status"] == "held":
            t = {**t, "status": "dispatched"}
            log.append({"task_id": t["task_id"], "revision": None, "is_final": True, "instruction": t["instruction"]})
        records.append({**t, "sources": ["orchestrator"]})
    return records, log


def counterfactual(record, wait_dispatches=False):
    """The measured handoff run under a different close rule, code only: the voice model's calls stay as they were.

    The measured run dispatched a read-only task whose words changed after its last decision. The shipped rule holds
    it. With `wait_dispatches`, a task whose last decision said wait and whose words never changed dispatches instead
    of asking for clarification."""
    close = next(e["revision"] for e in record["timeline"] if e["kind"] == "close")
    tasks, log, executions = [], list(record["dispatch_log"]), dict(record["executions"])
    for t in record["tasks"]:
        settled = [h for h in t["history"] if h["event"] == "status" and h["revision"] == close]
        reason = settled[0]["reason"] if settled else None
        if reason == "read-only request changed after its last decision":
            t = {**t, "status": "held"}
            log = [d for d in log if d["task_id"] != t["task_id"]]
            executions.pop(t["task_id"], None)
        elif wait_dispatches and reason == "readiness wait" and t["status"] == "needs_clarification":
            t = {**t, "status": "held" if t["side_effect"] else "dispatched"}
            if not t["side_effect"]:
                log.append({"task_id": t["task_id"], "revision": close, "is_final": True,
                            "instruction": t["instruction"]})
        tasks.append(t)
    unclear = [t["instruction"] for t in tasks if t["status"] == "needs_clarification"]
    return {**record, "tasks": tasks, "dispatch_log": log, "executions": executions,
            "snapshot": {**record["snapshot"], "clarification_needed": unclear or None}}


def side_effects_run(record, expected):
    """Expected side-effect requests (dispatch only on the final transcript) that a task started running."""
    states = {k: v["state"] for k, v in record["executions"].items()}
    return sum(any(_matches(e, t) and states.get(t["task_id"]) in ("running", "completed") for t in record["tasks"])
               for e in expected["tasks"] if e.get("dispatch_only_on_final"))


def handoff_split(record):
    """Seconds from the close's snapshot to the end of the tool turn, and from the last injection to first audio."""
    at = {e["kind"]: e["t"] for e in record["timeline"]}
    injected = [e["t"] for e in record["timeline"] if e["kind"] == "injection"][-1]
    return (at["tool_turn"] - at["snapshot"],
            record["audio_end"] + record["metrics"]["first_reply_audio_after_audio_end_s"] - injected)


def outcomes(record, expected):
    records, log = graded_records(record)
    graded = score(expected, records, log, record["snapshot"])
    keys = [e["key"] for e in expected["tasks"]]
    failed = {f.split(":")[0] for f in graded["failures"]}
    missing = sum(f"{k}: no task matches" in " ".join(graded["failures"]) for k in keys)
    return {"pass": graded["pass"], "expected": len(keys), "correct": sum(k not in failed for k in keys),
            "premature": graded["premature_dispatches"], "duplicate": graded["duplicate_dispatches"],
            "missed": missing + graded["missed_corrections"], "extra": len(graded["extra_tasks"])}


def tail_requests(record, expected):
    """Expected tasks the decider never made a task for, and what LFM3's delegates did with them."""
    decider = [t for t in record["tasks"] if "orchestrator" in t["sources"]]
    voice = [t for t in record["tasks"] if t["sources"] == ["lfm"]]
    tail = [e for e in expected["tasks"] if not any(_matches(e, t) for t in decider)]
    delegated = [e for e in tail if any(_matches(e, t) for t in voice)]
    kind_right = [e for e in delegated
                  if next(t for t in voice if _matches(e, t))["side_effect"] == bool(e.get("dispatch_only_on_final"))]
    covered = [e for e in expected["tasks"] if e not in tail]
    duplicated = sum(any(_matches(e, t) for t in voice) for e in covered)
    return {"tail": len(tail), "delegated": len(delegated), "kind_right": len(kind_right),
            "missed": len(tail) - len(delegated), "duplicated": duplicated,
            "unexpected": sum(not any(_matches(e, t) for e in expected["tasks"]) for t in voice)}


def tool_counts(record):
    results = record.get("tool_results") or []
    return {"calls": len(results), "linked": sum("duplicate_of" in r["result"] for r in results),
            "refused": sum(not r["result"].get("ok") for r in results),
            "cancel_or_amend": sum(r["name"] in ("cancel_task", "amend_task") for r in results)}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("before", type=Path)
    parser.add_argument("after", type=Path)
    parser.add_argument("--grades", type=Path)
    args = parser.parse_args()
    expected = json.loads((ROOT / "expected.json").read_text(encoding="utf-8"))
    runs = {"before": load(args.before), "after": load(args.after)}
    clips = [c for c in runs["before"] if c in runs["after"]]
    grades = json.loads(args.grades.read_text(encoding="utf-8")) if args.grades else {}

    print("## Timings (median / p95 / max, seconds after the audio ends)\n")
    print("| Measurement | Before | After |\n| --- | --- | --- |")
    for key in TIMED:
        print(f"| {key} | " + " | ".join(spread(runs[r][c]["metrics"][key] for c in clips) for r in runs) + " |")
    for label, partial_only in (("decider lag, every decision point", False),
                                ("decider lag, decision points on partials only", True)):
        print(f"| {label} | " + " | ".join(
            spread([x for c in clips for x in lags(runs[r][c], partial_only)]) for r in runs) + " |")

    print("\n## Per clip\n")
    print("| Clip | Tail words | Calls, before / after | Tasks pass, before / after | Correct of expected, "
          "before / after | Tail requests: delegated, kind right, missed | LFM tool calls: total, linked, "
          "cancel or amend, refused | First reply audio (s), before / after |")
    print("| --- | --- | --- | --- | --- | --- | --- | --- |")
    totals = {r: {"correct": 0, "expected": 0, "premature": 0, "duplicate": 0, "missed": 0, "extra": 0, "pass": 0}
              for r in runs}
    tails = {"tail": 0, "delegated": 0, "kind_right": 0, "missed": 0, "duplicated": 0, "unexpected": 0}
    for c in clips:
        b, a = runs["before"][c], runs["after"][c]
        ob, oa = outcomes(b, expected[c]), outcomes(a, expected[c])
        for r, o in (("before", ob), ("after", oa)):
            for k in totals[r]:
                totals[r][k] += o[k]
        tr, tc = tail_requests(a, expected[c]), tool_counts(a)
        for k in tails:
            tails[k] += tr[k]
        print(f"| {c} | {tail_words(a)} | {b['metrics']['decider_calls']} / {a['metrics']['decider_calls']} | "
              f"{'pass' if ob['pass'] else 'FAIL'} / {'pass' if oa['pass'] else 'FAIL'} | "
              f"{ob['correct']} / {oa['correct']} of {ob['expected']} | "
              f"{tr['delegated']}, {tr['kind_right']}, {tr['missed']} of {tr['tail']} | "
              f"{tc['calls']}, {tc['linked']}, {tc['cancel_or_amend']}, {tc['refused']} | "
              f"{b['metrics']['first_reply_audio_after_audio_end_s']:.2f} / "
              f"{a['metrics']['first_reply_audio_after_audio_end_s']:.2f} |")

    print("\n## Totals\n")
    print("| Measurement | Before | After |\n| --- | --- | --- |")
    for k in ("pass", "correct", "expected", "premature", "duplicate", "missed", "extra"):
        print(f"| {k} | {totals['before'][k]} | {totals['after'][k]} |")
    ran = {r: sum(side_effects_run(runs[r][c], expected[c]) for c in clips) for r in runs}
    print(f"| expected side-effect requests that ran | {ran['before']} | {ran['after']} |")
    for label, wait in (("after, shipped close rule (code only)", False),
                        ("after, shipped rule and a wait on unchanged words dispatches (code only)", True)):
        alt = {c: counterfactual(runs["after"][c], wait) for c in clips}
        o = [outcomes(alt[c], expected[c]) for c in clips]
        print(f"| {label} | | {sum(x['pass'] for x in o)} pass, {sum(x['correct'] for x in o)} correct, "
              f"{sum(side_effects_run(alt[c], expected[c]) for c in clips)} side-effect requests ran |")
    calls = {r: [runs[r][c]["metrics"]["decider_calls"] for c in clips] for r in runs}
    print(f"| decider calls per utterance, mean | {statistics.mean(calls['before']):.2f} | "
          f"{statistics.mean(calls['after']):.2f} |")
    print(f"| decider calls, total | {sum(calls['before'])} | {sum(calls['after'])} |")
    print(f"| tail words per clip, median (max) | | {statistics.median(tail_words(runs['after'][c]) for c in clips)} "
          f"({max(tail_words(runs['after'][c]) for c in clips)}) |")
    split = [handoff_split(runs["after"][c]) for c in clips]
    print(f"\nAfter, the tool turn took {spread([t for t, _ in split])} s after the close's snapshot, and the voice "
          f"started speaking {spread([v for _, v in split])} s after its snapshot went in.")
    print(f"\nTail requests after the close: {tails['tail']}. LFM3 delegated {tails['delegated']} "
          f"({tails['kind_right']} with the expected side-effect flag), missed {tails['missed']}, re-delegated "
          f"{tails['duplicated']} requests the decider already had, and made {tails['unexpected']} tasks that match "
          "no expected task.")

    if grades:
        print("\n## Replies (reader's grade)\n")
        print("| Measurement | Before | After |\n| --- | --- | --- |")
        for k, label in (("invents", "replies that invent something"), ("every_status", "replies that state every "
                         "task's status"), ("held_claimed_done", "replies that call a held task done")):
            print(f"| {label} | " + " | ".join(
                f"{sum(g[k] for g in grades[r].values())} of {len(grades[r])}" for r in runs) + " |")


if __name__ == "__main__":
    main()
