"""Round 2 API probes: key limits, extraction spans on the free id, and decisions with object state and several questions."""

import json
import sys
from datetime import datetime, timezone

from voice.segmentation.client import MODEL, ROOT, Client

TEXT = "find the q3 budget spreadsheet and tell me the total travel spend, actually make that q2"

PROBES = [
    ("extractions", "probe-extract-list", {
        "model": MODEL,
        "state": TEXT,
        "fields": {"segments": {"type": "list", "items": "text", "instructions":
                                "Each separate request or correction the speaker makes, copied exactly."}},
    }),
    ("decisions", "probe-object-state", {
        "model": MODEL,
        "state": {"transcript": TEXT, "segments": {"s1": "find the q3 budget spreadsheet",
                                                    "s2": "and tell me the total travel spend",
                                                    "s3": "actually make that q2"}},
        "questions": {
            "s3_kind": {"type": "choice", "instructions": "Classify segment s3.", "criteria": {
                "request": "Asks the assistant to do something.",
                "description": "Describes a task without asking for it.",
                "chitchat": "Filler or small talk.",
                "correction": "Changes an earlier segment.",
                "topic_change": "Only a change of subject."}},
            "s2_depends": {"type": "choice", "instructions": "Does segment s2 need another segment's result first?",
                           "criteria": {"none": "No.", "s1": "Needs s1's result."}},
            "s1_side_effect": {"type": "noul", "instructions": "Does carrying out s1 act outside the assistant?",
                               "criteria": {"true": "Sends, buys, books, posts, or changes someone's data.",
                                            "false": "Only looks up or reports information."}},
        },
    }),
]


def floor(n):
    """Time GET /api/v1/key on one warm connection: network round trip plus gateway auth, no inference."""
    client = Client()
    samples = [client.key_info() for _ in range(n + 1)]
    out = ROOT / "results" / "floor.jsonl"
    out.parent.mkdir(exist_ok=True)
    entry = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "connect_ms": samples[0]["connect_ms"],
             "status": sorted({s["status"] for s in samples}), "ms": [round(s["ms"], 1) for s in samples[1:]]}
    with out.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
    print(json.dumps(entry))


def main():
    if sys.argv[1:2] == ["floor"]:
        return floor(int(sys.argv[2]))
    client = Client()
    print(json.dumps({"key": client.key_info()}, indent=2))
    for route, label, body in PROBES:
        record = client.call(route, body, label=label)
        print(json.dumps({"label": label, "status": record["status"], "response": record["response"],
                          "ms": record["timing"]["ms"]}, indent=2))


if __name__ == "__main__":
    main()
