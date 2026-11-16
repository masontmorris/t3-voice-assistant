"""Compare how task results reach the voice before and after the update backlog.

    python -m voice.prototype.backlog_report NAME=RUN_DIR [NAME=RUN_DIR ...]

For each finished task it finds when the result arrived and when the voice started to say it. A mock task's result
arrives at its due time, and a lookup's at its dispatch time plus its latency. The voice says a result in the first
reply when the snapshot carried it, or in the update turn that carried it. The printed tables carry numbers and task
ids only, never transcript text.
"""

import argparse
from pathlib import Path

from voice.prototype.eou_report import load, spread

TERMINAL = ("completed", "failed")


def arrival(execution):
    tool = execution.get("tool") or {}
    if tool.get("latency_s") is not None:
        return execution["dispatched_at"] + tool["latency_s"]
    return execution["due_at"]


def windows(record):
    """(start, end, name) for the tools session's turn, the first reply, and each update reply. A result that lands
    in the tools turn reaches the voice snapshot, so it is not one that arrived while the voice was replying."""
    at = {e["kind"]: e["t"] for e in record["timeline"] if e["kind"] == "snapshot"}
    injected = [e["t"] for e in record["timeline"] if e["kind"] == "injection"][0]
    first = next(r for r in record["replies"] if r["kind"] == "snapshot")
    out = [(at["snapshot"], injected, "tools turn"), (injected, first["done_at"], "first reply")]
    boundaries = [e["t"] for e in record["timeline"] if e["kind"] == "task_updates"]
    for boundary, reply in zip(boundaries, [r for r in record["replies"] if r["kind"] == "update"]):
        out.append((boundary, reply["done_at"], "update reply"))
    return out


def deliveries(record):
    """task_id -> list of times the voice started saying its result."""
    said = {}
    first = next(r for r in record["replies"] if r["kind"] == "snapshot")
    snapshot = next((e["snapshot"] for e in record["timeline"] if e["kind"] == "snapshot_after_tools"),
                    record["snapshot"])
    for task in snapshot["tasks"]:
        if task["status"] in TERMINAL and first["first_audio_after_s"] is not None:
            said.setdefault(task["task_id"], []).append(record["audio_end"] + first["first_audio_after_s"])
    turns = [e for e in record["timeline"] if e["kind"] == "task_updates"]
    for event, reply in zip(turns, [r for r in record["replies"] if r["kind"] == "update"]):
        if reply["first_audio_after_s"] is None:
            continue
        for update in event["updates"]:
            said.setdefault(update["task_id"], []).append(event["t"] + reply["first_audio_after_s"])
    return said


def clip_rows(record):
    said, spans = deliveries(record), windows(record)
    rows = []
    for task_id, execution in record["executions"].items():
        if execution["state"] not in TERMINAL:
            continue
        t = arrival(execution)
        where = next((name for start, end, name in spans if start <= t <= end), None)
        if where is None:
            where = "before the snapshot" if t < spans[0][0] else "while idle"
        times = said.get(task_id, [])
        rows.append({"task_id": task_id, "arrived": where, "said": len(times),
                     "wait_s": times[0] - t if times else None})
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("runs", nargs="+", help="NAME=RUN_DIR")
    args = parser.parse_args()
    runs = {name: load(Path(path)) for name, path in (r.split("=", 1) for r in args.runs)}
    names = list(runs)
    clips = [c for c in runs[names[0]] if all(c in runs[n] for n in names)]
    rows = {n: {c: clip_rows(runs[n][c]) for c in clips} for n in names}
    every = {n: [r for c in clips for r in rows[n][c]] for n in names}

    def cell(n, pick):
        return pick(every[n])

    print("| Measurement | " + " | ".join(names) + " |\n| --- |" + " --- |" * len(names))
    for text, pick in (
            ("finished tasks", len),
            ("results that arrived before the snapshot", lambda rs: sum(r["arrived"] == "before the snapshot"
                                                                       for r in rs)),
            ("results that arrived in the tools turn", lambda rs: sum(r["arrived"] == "tools turn" for r in rs)),
            ("results that arrived during a reply", lambda rs: sum(r["arrived"].endswith("reply") for r in rs)),
            ("results that arrived while idle", lambda rs: sum(r["arrived"] == "while idle" for r in rs)),
            ("results never said", lambda rs: sum(r["said"] == 0 for r in rs)),
            ("results said twice or more", lambda rs: sum(r["said"] > 1 for r in rs)),
            ("arrival to first word, median / p95 / max (s)",
             lambda rs: spread(r["wait_s"] for r in rs if r["arrived"] not in ("before the snapshot", "tools turn"))),
    ):
        print(f"| {text} | " + " | ".join(str(cell(n, pick)) for n in names) + " |")
    for n in names:
        turns = [len([r for r in runs[n][c]["replies"] if r["kind"] == "update"]) for c in clips]
        print(f"| update turns, {n} | {sum(turns)} over {sum(t > 0 for t in turns)} clips |")
    print("\n| Clip | " + " | ".join(names) + " |\n| --- |" + " --- |" * len(names))
    for c in clips:
        print(f"| {c} | " + " | ".join(
            ", ".join(f"{r['task_id']} {r['arrived']} "
                      f"{'%.2f s' % r['wait_s'] if r['wait_s'] is not None else 'never said'}"
                      for r in rows[n][c]) or "-" for n in names) + " |")


if __name__ == "__main__":
    main()
