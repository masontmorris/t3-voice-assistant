"""Print the update backlog's decisions, and the reply that followed each one.

    python -m voice.prototype.backlog_decisions RUN_DIR [RUN_DIR ...]

RUN_DIR holds one `voice.prototype.run` pass. The output quotes the voice's replies and the tasks' summaries, so it
stays local, beside the run, and never goes into the repo or a public doc.
"""

import argparse
import json
from pathlib import Path


def load(run):
    return {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in sorted(run.glob("*.json"))
            if p.name != "summary.json"}


def lines(record):
    first = next(r for r in record["replies"] if r["kind"] == "snapshot")
    updates = [r for r in record["replies"] if r["kind"] == "update"]
    out = [f"First reply: {first['text']}", ""]
    said = 0
    for d in record["backlog"]:
        out.append(f"- At {d['at']:.2f} s, {'re-prompt' if d['reprompt'] else 'no re-prompt'}")
        for e in d["backlog"]:
            out.append(f"  - {e['task_id']} {e['old']} to {e['new']} while {e['during']}: {e['summary']}")
        if not d["backlog"]:
            out.append("  - nothing arrived")
        out.append(f"  - tell: {', '.join(d['tell']) or 'nothing'}")
        if d["reprompt"]:
            out.append(f"  - update reply: {updates[said]['text']}")
            said += 1
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("runs", nargs="+", help="a run directory, or several")
    args = parser.parse_args()
    for path in args.runs:
        for clip, record in load(Path(path)).items():
            print(f"## {clip}\n")
            print("\n".join(lines(record)))
            print()


if __name__ == "__main__":
    main()
