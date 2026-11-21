"""Record the local d1-2B's answers on descriptions of made-up approval requests, for voice/t3/test_approval_gate.py.

    python -m voice.t3.eval.record_approval_gate            # starts the local d1-2B, under the GPU lease
    python -m voice.t3.eval.record_approval_gate --url http://127.0.0.1:8198   # one that is already running

Every description of voice/t3/eval/approval_cases.py goes through two gates: the approval gate (`request_gate`) and the
summary gate the approvals used before it (`gate_request`), so the replay can show both. A row keeps the answers and the
request's hash, so the replay says when a question changed.
"""

import hashlib
import json
import sys
from pathlib import Path

from voice.local_decider.client import D1Client, decider
from voice.t3 import local_summary as LS
from voice.t3.eval import approval_cases as AC

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "approval_gate.json"
SPLITS = (("seen", AC.SEEN), ("held_out", AC.HELD_OUT))


def key(body):
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


def rows_for(client):
    rows = []
    for split, cases in SPLITS:
        by_name = {c["name"]: c for c in cases}
        for row in AC.descriptions(cases):
            state = AC.state(by_name[row["case"]])
            gates = {}
            for gate, body in (("request", LS.request_gate(state, row["text"])),
                               ("summary", LS.gate_request(state, row["text"]))):
                result = client.call("decisions", body, label="approval_gate")
                if result["status"] != 200:
                    raise RuntimeError(f"decider status {result['status']}")
                gates[gate] = {"hash": key(body), "answers": result["response"]["answers"]}
            rows.append({"split": split, "case": row["case"], "kind": row["kind"], "text": row["text"],
                         "faithful": row["faithful"], "gates": gates})
    return rows


def main():
    if "--url" in sys.argv:
        rows = rows_for(D1Client(sys.argv[sys.argv.index("--url") + 1], timeout_s=30))
    else:
        with decider("d1") as client:
            rows = rows_for(client)
    FIXTURE.write_text(json.dumps(rows, separators=(",", ":")) + "\n", encoding="utf-8")
    print(f"{len(rows)} rows")


if __name__ == "__main__":
    main()
