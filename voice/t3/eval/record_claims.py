"""Record the local d1-2B's answers for the free-reply check, for voice/t3/test_claims.py.

    python -m voice.t3.eval.record_claims     # starts the local d1-2B, under the GPU lease
    python -m voice.t3.eval.record_claims --cpu   # the same with no GPU layers, while the GPU is held
    python -m voice.t3.eval.record_claims --no-thread   # only the cases with no thread in view

The cases are made up (voice/t3/eval/claim_cases.py). Each row keeps the answers and the call's time, so the replay can
score the thresholds without a model server and the time of one check is on record.
"""

import hashlib
import json
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from voice.local_decider.client import FLAGS, MODEL, VOICE_HOME, D1Client, LlamaServer, decider
from voice.t3.eval.claim_cases import HELD_OUT, NO_THREAD, SEEN
from voice.t3.verify import claim_request

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
FIXTURE = FIXTURES / "claims.json"
NO_THREAD_FIXTURE = FIXTURES / "claims_no_thread.json"


def key(body):
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


@contextmanager
def cpu_decider(port=8196):
    """The d1-2B with no GPU layers, for a recording made while the GPU is held. Its answers are close to the GPU's, not
    equal: the recording says which one made it."""
    server = LlamaServer(["-m", str(MODEL), "-ngl", "0", "-np", "1", *FLAGS], port,
                         VOICE_HOME / "evidence" / "local-decider" / "d1-cpu-server.log")
    with server:
        yield D1Client(f"http://127.0.0.1:{port}")


def main():
    rows = []
    with (cpu_decider() if "--cpu" in sys.argv else decider("d1")) as client:
        client.call("decisions", claim_request("Hello.", SEEN[0][2]))  # the first call loads the prompt
        sets = ((("no_thread", NO_THREAD),) if "--no-thread" in sys.argv
                else (("seen", SEEN), ("held_out", HELD_OUT)))
        for split, cases in sets:
            for name, reply, facts, want in cases:
                body = claim_request(reply, facts)
                started = time.perf_counter()
                result = client.call("decisions", body)
                ms = round((time.perf_counter() - started) * 1000, 1)
                if result["status"] != 200:
                    raise RuntimeError(f"decider status {result['status']}")
                rows.append({"split": split, "name": name, "want": want, "ms": ms, "request_hash": key(body),
                             "answers": result["response"]["answers"]})
    (NO_THREAD_FIXTURE if "--no-thread" in sys.argv else FIXTURE).write_text(json.dumps(rows, separators=(",", ":")) + "\n", encoding="utf-8")
    times = sorted(row["ms"] for row in rows)
    print(f"{len(rows)} rows, median {times[len(times) // 2]} ms, p95 {times[int(len(times) * 0.95)]} ms")


if __name__ == "__main__":
    main()
