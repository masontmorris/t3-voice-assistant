"""Compare end-of-utterance modes on the same clips: gated, decider and handoff, or any subset.

    python -m voice.research.prototype.eou_report NAME=RUN_DIR [NAME=RUN_DIR ...] [--grades GRADES.json]

Each RUN_DIR holds one `voice.prototype.run` pass over the same clips. The printed tables carry numbers and
expected-task keys only, never transcript text, so they can go into the public doc. GRADES.json, when given, holds a
reader's grade of every spoken reply under {NAME: {clip: {"invents": bool, "every_status": bool,
"held_claimed_done": bool}}}. It stays local, beside the runs, because the grader's notes quote the replies.
"""

import argparse
import json
import math
import statistics
from pathlib import Path

from voice.research.prototype.report import ROOT
from voice.segmentation.cursor import words
from voice.research.segmentation.gate_tune import label
from voice.segmentation.score import _matches, score

TIMED = ("final_transcript_after_audio_end_s", "snapshot_ready_after_audio_end_s",
         "first_reply_audio_after_audio_end_s")
OUTCOMES = ("pass", "correct", "expected", "premature", "duplicate", "missed", "extra")


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
    return f"{statistics.median(values):.2f} / {p95(values):.2f} / {max(values):.2f} (n={len(values)})"


def points(record):
    """Successful decision points as (completion time, words read, is_final)."""
    sizes = {e["revision"]: len(words(e["text"])) for e in record["timeline"] if e["kind"] == "transcript_update"}
    return [(e["t"], sizes[e["revision"]], e["is_final"]) for e in record["timeline"]
            if e["kind"] == "decision_point" and e["calls"] and all(c["status"] == 200 for c in e["calls"])]


def lags(record):
    """Seconds from each word's first partial to the first decision point on a partial that read it."""
    updates = [(e["t"], len(words(e["text"]))) for e in record["timeline"] if e["kind"] == "transcript_update"]
    covering = [(t, n) for t, n, final in points(record) if not final]
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


def after_the_utterance(record):
    """The dispatch log, with every dispatch a voice-model call caused marked as after the final transcript.

    Before `Orchestrator.end`, the gated close left the last partial's revision in place, so those dispatches logged
    as partial ones. Every tool call comes after the snapshot in the timeline."""
    by_voice = {t["task_id"] for t in record["tasks"]
                if any(h["event"] == "status" and h["new"] == "dispatched" and h["reason"].startswith("voice model")
                       for h in t["history"])}
    return [{**d, "is_final": True} if d["task_id"] in by_voice else d for d in record["dispatch_log"]]


def graded_records(record):
    """Every task. A held side effect scores as a final dispatch, because it ends as held_for_confirmation, as a
    dispatched side effect the service holds does. A held read-only task scores as undispatched."""
    records, log = [], after_the_utterance(record)
    for t in record["tasks"]:
        if t["status"] == "held" and not t["side_effect"]:
            t = {**t, "status": "ready"}
        elif t["status"] == "held":
            t = {**t, "status": "dispatched"}
            log.append({"task_id": t["task_id"], "revision": None, "is_final": True, "instruction": t["instruction"]})
        records.append({**t, "sources": ["orchestrator"]})
    return records, log


def outcomes(record, expected):
    records, log = graded_records(record)
    graded = score(expected, records, log, record["snapshot"])
    keys = [e["key"] for e in expected["tasks"]]
    failed = {f.split(":")[0] for f in graded["failures"]}
    missing = sum(f"{k}: no task matches" in " ".join(graded["failures"]) for k in keys)
    return {"pass": graded["pass"], "expected": len(keys), "correct": sum(k not in failed for k in keys),
            "premature": graded["premature_dispatches"], "duplicate": graded["duplicate_dispatches"],
            "missed": missing + graded["missed_corrections"], "extra": len(graded["extra_tasks"]),
            "failed_keys": sorted(failed & set(keys))}


def side_effects_run(record, expected):
    """Expected side-effect requests (dispatch only on the final transcript) that a task started running, and how many
    of those ran on a dispatch the gate's labels call wrong."""
    states = {k: v["state"] for k, v in record["executions"].items()}
    log = after_the_utterance(record)
    ran = wrong = 0
    for e in expected["tasks"]:
        if not e.get("dispatch_only_on_final"):
            continue
        runs = [t for t in record["tasks"] if _matches(e, t) and states.get(t["task_id"]) in ("running", "completed")]
        ran += bool(runs)
        for t in runs[:1]:
            i = next(i for i, d in enumerate(log) if d["task_id"] == t["task_id"])
            wrong += label(log[i], expected, {d["task_id"] for d in log[:i]})[0] == "wrong"
    return ran, wrong


def gate_counts(record, expected):
    """Tasks the gate ran during speech, how many of them were wrong, tasks it held, and tasks LFM3 decided."""
    by_id = {t["task_id"]: t for t in record["tasks"]}
    ran, wrong = [], []
    for i, entry in enumerate(record["dispatch_log"]):
        moved = [h for h in by_id[entry["task_id"]]["history"] if h["event"] == "status" and h["new"] == "dispatched"]
        if moved and moved[-1]["reason"].startswith("gate:"):
            ran.append(entry["task_id"])
            if label(entry, expected, {e["task_id"] for e in record["dispatch_log"][:i]})[0] == "wrong":
                wrong.append(entry["task_id"])
    held = [t["task_id"] for t in record["tasks"]
            if any(h["event"] == "gate" and not h["passed"] for h in t["history"]) and t["task_id"] not in ran]
    voice = [t["task_id"] for t in record["tasks"] if "lfm" in t["sources"]
             or any(h.get("reason", "").startswith("voice model") for h in t["history"])]
    return {"ran": len(ran), "wrong": len(wrong), "held": len(held), "voice": len(voice)}


def tool_turn_s(record):
    """Seconds from the snapshot to the end of the tools session's turn, when the mode has one."""
    at = {e["kind"]: e["t"] for e in record["timeline"]}
    return at["tool_turn"] - at["snapshot"] if "tool_turn" in at else None


def row(label_, values):
    return f"| {label_} | " + " | ".join(str(v) for v in values) + " |"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("runs", nargs="+", help="NAME=RUN_DIR")
    parser.add_argument("--grades", type=Path)
    args = parser.parse_args()
    expected = json.loads((ROOT / "expected.json").read_text(encoding="utf-8"))
    runs = {name: load(Path(path)) for name, path in (r.split("=", 1) for r in args.runs)}
    names = list(runs)
    clips = [c for c in runs[names[0]] if all(c in runs[n] for n in names)]
    grades = json.loads(args.grades.read_text(encoding="utf-8")) if args.grades else {}
    graded = {n: {c: outcomes(runs[n][c], expected[c]) for c in clips} for n in names}

    print("## Timings (median / p95 / max, seconds after the audio ends)\n")
    print(row("Measurement", names) + "\n| --- |" + " --- |" * len(names))
    for key in TIMED:
        print(row(key, [spread(runs[n][c]["metrics"][key] for c in clips) for n in names]))
    print(row("tools session turn after the snapshot", [spread(tool_turn_s(runs[n][c]) for c in clips) for n in names]))
    print(row("decider lag behind speech, partials",
              [spread(x for c in clips for x in lags(runs[n][c])) for n in names]))

    print("\n## Totals\n")
    print(row("Measurement", names) + "\n| --- |" + " --- |" * len(names))
    for key in OUTCOMES:
        print(row(key, [sum(graded[n][c][key] for c in clips) for n in names]))
    ran = {n: [side_effects_run(runs[n][c], expected[c]) for c in clips] for n in names}
    print(row("expected side-effect requests that ran", [sum(r for r, _ in ran[n]) for n in names]))
    print(row("of those, on a wrong dispatch", [sum(w for _, w in ran[n]) for n in names]))
    print(row("decider calls per utterance, mean",
              [f"{statistics.mean(runs[n][c]['metrics']['decider_calls'] for c in clips):.2f}" for n in names]))
    print(row("LFM3 tool calls", [sum(runs[n][c]["metrics"].get("tool_calls", 0) for c in clips) for n in names]))
    gates = {n: [gate_counts(runs[n][c], expected[c]) for c in clips] for n in names}
    for key, text in (("ran", "tasks the gate ran during speech"), ("wrong", "of those, wrong"),
                      ("held", "tasks the gate held"), ("voice", "tasks LFM3 decided")):
        print(row(text, [sum(g[key] for g in gates[n]) for n in names]))
    if grades:
        for key, text in (("invents", "replies that invent something"),
                          ("held_claimed_done", "replies that call a held task done")):
            print(row(text, [f"{sum(g[key] for g in grades[n].values())} of {len(grades[n])}" if n in grades
                             else "n/a" for n in names]))

    print("\n## Per clip: expected tasks correct, then first reply audio (s)\n")
    print(row("Clip", names) + "\n| --- |" + " --- |" * len(names))
    for c in clips:
        print(row(c, [f"{graded[n][c]['correct']} of {graded[n][c]['expected']}"
                      f"{' (' + ', '.join(graded[n][c]['failed_keys']) + ')' if graded[n][c]['failed_keys'] else ''}"
                      f", {runs[n][c]['metrics']['first_reply_audio_after_audio_end_s']:.2f}" for n in names]))


if __name__ == "__main__":
    main()
