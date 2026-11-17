"""Summarize prototype runs that made real lookups, one row per lookup.

    python -m voice.research.tools.report RUN_DIR [RUN_DIR ...] [--json]

Per lookup it gives the tool and arguments, the outcome, the lookup's own latency, when it was dispatched relative
to the end of the audio, which reply first carried the result, and when that reply's first audio came, also
relative to the end of the audio. `facts` lists the numbers and names in the summary, and `stated` the ones the
reply text contains. Matching is literal, so a reply that rephrases a fact needs a person to read it.
"""

import argparse
import json
import re
from pathlib import Path

FACT = re.compile(r"\d{1,2}:\d{2} [AP]M|\d+(?:\.\d+)?|Saturday|Sunday|Monday|Tuesday|Wednesday|Thursday|Friday")


def facts(summary):
    return list(dict.fromkeys(FACT.findall(summary)))


def stated(fact, text):
    if re.fullmatch(r"\d{1,2}:\d{2} [AP]M", fact):
        clock, half = fact.split()
        return re.search(re.escape(clock) + r"\s*" + half[0] + r"\.?\s*" + half[1], text, re.I) is not None
    return re.search(r"(?<![\d.])" + re.escape(fact) + r"(?![\d])", text) is not None


def rows(record):
    audio_end = record["audio_end"]
    boundary = next((e["t"] for e in record["timeline"] if e["kind"] == "task_updates"), None)
    replies = {r["kind"]: r for r in record["replies"]}
    for task_id, execution in record["executions"].items():
        tool = execution.get("tool")
        if not tool:
            continue
        told_in = ("snapshot" if any(t["task_id"] == task_id and "result_summary" in t
                                     for t in record["snapshot"]["tasks"]) else "update")
        reply = replies.get(told_in)
        if reply is None or reply["first_audio_after_s"] is None:
            heard = None
        elif told_in == "snapshot":
            heard = reply["first_audio_after_s"]
        else:
            heard = boundary - audio_end + reply["first_audio_after_s"]
        summary = tool.get("summary") or ""
        found = [f for f in facts(summary) if reply and stated(f, reply["text"])]
        yield {"clip": record["clip"], "task_id": task_id, "tool": tool["tool"], "arguments": tool["arguments"],
               "outcome": tool.get("outcome"), "latency_s": tool.get("latency_s"),
               "dispatched_after_audio_end_s": execution["dispatched_at"] - audio_end, "told_in": told_in,
               "result_audio_after_audio_end_s": heard, "summary": summary, "facts": facts(summary), "stated": found,
               "reply": reply["text"] if reply else None}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("runs", nargs="+", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    out = []
    for run in args.runs:
        for path in sorted(run.glob("*.json")):
            record = json.loads(path.read_text(encoding="utf-8"))
            if "executions" in record:
                out.extend({"run": run.name, **row} for row in rows(record))
    if args.json:
        print(json.dumps(out, indent=1))
        return
    for row in out:
        heard = row["result_audio_after_audio_end_s"]
        print(f"{row['run']} {row['clip']} {row['task_id']} {row['tool']} {json.dumps(row['arguments'])} "
              f"{row['outcome']} lookup {row['latency_s']:.3f} s, dispatched {row['dispatched_after_audio_end_s']:+.2f} s, "
              f"told in {row['told_in']} at {'-' if heard is None else f'{heard:.2f}'} s, "
              f"stated {len(row['stated'])}/{len(row['facts'])}")


if __name__ == "__main__":
    main()
