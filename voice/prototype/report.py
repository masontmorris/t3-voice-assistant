"""Summarize prototype runs: WER, task records against expected.json, the side-effect hold, and timings.

    python -m voice.prototype.report RUN_DIR [RUN_DIR ...]

RUN_DIR holds the CLIP.json files of one `voice.prototype.run` pass. References come from script.txt in
$VOICE_HOME/audio/real/ and from the synthetic clips' .txt files. The printed tables carry numbers and expected-task
keys only, never transcript text, so they can go into the public doc. The full summary goes to RUN_DIR/summary.json.
"""

import argparse
import json
import os
import re
import statistics
from pathlib import Path

from voice.segmentation.score import score

ROOT = Path(__file__).resolve().parent
VOICE_HOME = Path(os.environ.get("VOICE_HOME", Path.home() / "voice"))
SCRIPT = VOICE_HOME / "audio" / "real" / "script.txt"
SYNTHETIC = VOICE_HOME / "evidence" / "stage1" / "audio"
SMALL = ("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
         "seventeen eighteen nineteen").split()
TENS = "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()
TIMINGS = ("final_transcript_after_audio_end_s", "snapshot_ready_after_audio_end_s",
           "first_reply_audio_after_audio_end_s")


def number_words(n):
    if n < 20:
        return SMALL[n]
    if n < 100:
        return TENS[n // 10] + ("" if n % 10 == 0 else " " + SMALL[n % 10])
    return SMALL[n // 100] + " hundred" + ("" if n % 100 == 0 else " " + number_words(n % 100))


def normalize(text):
    text = re.sub(r"\d+", lambda m: number_words(int(m.group())) if int(m.group()) < 1000 else m.group(), text.lower())
    return re.findall(r"[a-z']+", text.replace("-", " "))


def wer(reference, hypothesis):
    ref, hyp = normalize(reference), normalize(hypothesis)
    row = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        prev, row[0] = row[0], i
        for j, h in enumerate(hyp, 1):
            prev, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, prev + (r != h))
    return row[-1] / len(ref)


def references():
    refs = {}
    for line in SCRIPT.read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#"):
            name, text = line.split("\t")[:2]
            refs[Path(name).stem] = text
    for path in SYNTHETIC.glob("*.txt"):
        refs[path.stem] = path.read_text(encoding="utf-8").strip()
    return refs


def short_failure(failure):
    """A failure string from the scorer, without the transcript text it may quote."""
    key = failure.split(":")[0]
    if "no task matches" in failure:
        return f"{key} missing"
    if "unexpected" in failure:
        return "extra " + failure.split("unexpected ")[1].split(" ")[0] + " task"
    if "duplicate dispatch" in failure:
        return "duplicate dispatch"
    if failure.startswith("clarification_needed"):
        return "asked to clarify"
    if "dispatched on partial" in failure:
        return f"{key} dispatched early"
    for field in ("status", "depends_on", "revises", "sources", "instruction"):
        if failure.startswith(f"{key}: {field}") or f"{field} " in failure.split(":", 1)[1][:20]:
            detail = failure.split(":", 1)[1].split(",")[0].strip()
            return f"{key} {detail}" if field == "status" else f"{key} {field}"
    return f"{key} other"


def side_effect_check(record):
    by_id = {t["task_id"]: t for t in record["tasks"]}
    held = executed = early = 0
    for task_id, execution in record["executions"].items():
        task = by_id[task_id]
        if task["side_effect"]:
            held += execution["state"] == "held_for_confirmation"
            executed += execution["state"] == "completed"
    for entry in record["dispatch_log"]:
        early += by_id[entry["task_id"]]["side_effect"] and not entry["is_final"]
    return {"side_effect_tasks": sum(t["side_effect"] for t in record["tasks"]), "held": held,
            "executed": executed, "dispatched_early": early}


def final_decision(record):
    """How the snapshot wait splits: the final transcript, then client spacing, then the decider call itself."""
    points = [e for e in record["timeline"] if e["kind"] == "decision_point"]
    finals = [e for e in points if e["is_final"]]
    if not finals:
        return None
    last = points[-1]
    call_ms = sum(c["ms"] for c in last["calls"]) if last["live_calls"] else 0.0
    return {"live": bool(last["live_calls"]), "points_after_final": len(points) - points.index(finals[0]),
            "decide_s": last["t"] - last["started"], "call_ms": call_ms}


def summarize_clip(record, expected, refs):
    stem = Path(record["clip"]).stem
    if "error" in record:
        return {"clip": stem, "error": record["error"]}
    orchestrated = [t for t in record["tasks"] if "orchestrator" in t["sources"]]
    for_score = [{**t, "sources": ["orchestrator"]} for t in orchestrated]
    graded = score(expected[stem], for_score, [d for d in record["dispatch_log"]
                                               if any(t["task_id"] == d["task_id"] for t in orchestrated)],
                   record["snapshot"])
    calls = (record.get("tool_turn") or {}).get("function_calls", [])
    return {
        "clip": stem,
        "wer": wer(refs[stem], record["final_transcript"]),
        "tasks_pass": graded["pass"],
        "failures": [short_failure(f) for f in graded["failures"]],
        "snapshot_statuses": [t["status"] for t in record["snapshot"]["tasks"]],
        "side_effects": side_effect_check(record),
        "delegate_calls": [c.get("name") for c in calls],
        "delegate_only_tasks": sum("orchestrator" not in t["sources"] for t in record["tasks"]),
        "injection_accepted": [i["accepted"] for i in record["injection"]],
        "updates": len(record["task_updates"]),
        "replies": [r["kind"] for r in record["replies"] if r.get("status") == "completed"],
        **{k: record["metrics"][k] for k in TIMINGS},
        "decider_live_calls": record["metrics"]["decider_live_calls"],
        "final_decision": final_decision(record),
    }


def spread(values):
    values = [v for v in values if v is not None]
    if not values:
        return "n/a"
    return f"{statistics.median(values):.2f} ({min(values):.2f} to {max(values):.2f}), n={len(values)}"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("runs", nargs="+", type=Path)
    args = parser.parse_args()
    expected = json.loads((ROOT / "expected.json").read_text(encoding="utf-8"))
    refs = references()
    for run in args.runs:
        rows = [summarize_clip(json.loads(p.read_text(encoding="utf-8")), expected, refs)
                for p in sorted(run.glob("*.json")) if p.name != "summary.json"]
        (run / "summary.json").write_text(json.dumps(rows, indent=1) + "\n", encoding="utf-8")
        print(f"\n## {run.name}\n")
        print("| Clip | WER | Tasks | Failures | Snapshot statuses | Side effects held / executed / early | "
              "Final transcript (s) | Snapshot ready (s) | First reply audio (s) | Live calls |")
        print("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
        for r in rows:
            if "error" in r:
                print(f"| {r['clip']} | error: {r['error']} |")
                continue
            se = r["side_effects"]
            print(f"| {r['clip']} | {r['wer']:.2f} | {'pass' if r['tasks_pass'] else 'FAIL'} | "
                  f"{'; '.join(r['failures']) or '-'} | {', '.join(r['snapshot_statuses']) or '-'} | "
                  f"{se['held']} / {se['executed']} / {se['dispatched_early']} of {se['side_effect_tasks']} | "
                  + " | ".join(f"{r[k]:.2f}" if r[k] is not None else "n/a" for k in TIMINGS)
                  + f" | {r['decider_live_calls']} |")
        ok = [r for r in rows if "error" not in r]
        print(f"\nPass {sum(r['tasks_pass'] for r in ok)} of {len(ok)}. "
              f"WER median {statistics.median(r['wer'] for r in ok):.3f}.")
        for key in TIMINGS:
            print(f"- {key}: {spread(r[key] for r in ok)}")
        voice_side = [r["first_reply_audio_after_audio_end_s"] - r["snapshot_ready_after_audio_end_s"] for r in ok
                      if r["first_reply_audio_after_audio_end_s"] is not None]
        print(f"- first reply audio after snapshot ready: {spread(voice_side)}")
        live = [r for r in ok if r["final_decision"] and r["final_decision"]["live"]]
        print(f"- snapshot ready, final decision live: {spread(r['snapshot_ready_after_audio_end_s'] for r in live)}")
        cached = [r for r in ok if r["final_decision"] and not r["final_decision"]["live"]]
        print(f"- snapshot ready, final decision cached: "
              f"{spread(r['snapshot_ready_after_audio_end_s'] for r in cached)}")
        waits = [r["final_decision"]["decide_s"] - r["final_decision"]["call_ms"] / 1000 for r in live]
        print(f"- last decision point, wall time minus call time (client spacing and queueing): {spread(waits)}")
        print(f"- last decision point, call time: {spread(r['final_decision']['call_ms'] / 1000 for r in live)}")


if __name__ == "__main__":
    main()
