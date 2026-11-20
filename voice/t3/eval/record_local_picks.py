"""Record local pick and reply answers. Run under the GPU lease."""

import hashlib
import json
from pathlib import Path

from voice.local_decider.client import decider
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


def main():
    with decider("d1") as client:
        rows = []
        for case in cases():
            key, answers = record(client, pick_request(case["request"], listed(case["listing"]), case["recent"]))
            rows.append({**case, "answer": answers["pick"], "request_hash": key})
        save("local_thread_picks.json", rows)
        rows = []
        for act, reply, options, must_act, target in REPLIES:
            key, answers = record(client, reading_request(act, reply, options))
            rows.append({"act": act.to_json(), "reply": reply, "options": options, "must_act": must_act,
                         "expected_target": target, "answers": answers, "request_hash": key})
        save("local_reply_reads.json", rows)
        rows = []
        for request, want, about in HELD:
            key, answers = record(client, pick_request(request, THREADS))
            rows.append({"request": request, "want": want, "about": about, "answer": answers["pick"],
                         "request_hash": key})
        save("local_pick_holdout.json", rows)


if __name__ == "__main__":
    main()
