"""Record pick and reply answers for voice/t3/test_local_answers.py.

    python -m voice.t3.eval.record_local_picks            # local d1-2B, under the GPU lease
    VOICE_ENV_FILE=<env file> python -m voice.t3.eval.record_local_picks liquid
    python -m voice.t3.eval.record_local_picks d1 replies  # only the reply sets

The local run records every set. The hosted run records the two held-out sets only; record_picks.py and
record_replies.py keep its other fixtures.
"""

import hashlib
import json
import sys
from pathlib import Path

from voice.local_decider.client import decider
from voice.t3.eval import fresh_holdout as fresh
from voice.t3.eval.pick_holdout import CASES as HELD, THREADS
from voice.t3.eval.record_picks import cases, listed
from voice.t3.eval.record_replies import CASES as REPLIES
from voice.t3.verify import pick_request, reading_request

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def record(client, body):
    result = client.call("decisions", body)
    if result["status"] != 200:
        raise RuntimeError(f"decider status {result['status']}")
    key = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
    return key, result["response"]["answers"]


def save(name, rows):
    (FIXTURES / name).write_text(json.dumps(rows, separators=(",", ":")) + "\n", encoding="utf-8")


def replies(client, cases):
    rows = []
    for act, reply, options, must_act, target in cases:
        key, answers = record(client, reading_request(act, reply, options))
        rows.append({"act": act.to_json(), "reply": reply, "options": list(options), "must_act": must_act,
                     "expected_target": target, "answers": answers, "request_hash": key})
    return rows


def main():
    name = sys.argv[1] if len(sys.argv) > 1 else "d1"
    only_replies = sys.argv[2:] == ["replies"]
    prefix = "local" if name == "d1" else "hosted"
    with decider(name) as client:
        if only_replies:
            if name == "d1":
                save("local_reply_reads.json", replies(client, REPLIES))
            save(f"{prefix}_fresh_replies.json", replies(client, fresh.REPLIES))
            return
        if name == "d1":
            rows = []
            for case in cases():
                key, answers = record(client, pick_request(case["request"], listed(case["listing"]), case["recent"]))
                rows.append({**case, "answer": answers["pick"], "request_hash": key})
            save("local_thread_picks.json", rows)
            save("local_reply_reads.json", replies(client, REPLIES))
        rows = []
        for request, want, about in HELD:
            key, answers = record(client, pick_request(request, THREADS))
            rows.append({"request": request, "want": want, "about": about, "answer": answers["pick"],
                         "request_hash": key})
        save(f"{prefix}_pick_holdout.json", rows)
        rows = []
        for request, recent, want, about in fresh.PICKS:
            key, answers = record(client, pick_request(request, fresh.THREADS, recent))
            rows.append({"request": request, "recent": recent, "want": want, "about": about,
                         "answer": answers["pick"], "request_hash": key})
        save(f"{prefix}_fresh_picks.json", rows)
        save(f"{prefix}_fresh_replies.json", replies(client, fresh.REPLIES))


if __name__ == "__main__":
    main()
