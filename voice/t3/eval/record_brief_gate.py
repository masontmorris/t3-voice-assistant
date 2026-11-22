"""Record the local d1-2B's answers on briefs for made-up launch requests, for voice/t3/test_brief_gate.py.

    python -m voice.t3.eval.record_brief_gate     # starts LFM3-1.7B and the d1-2B; run it under the GPU lease

Every brief of voice/t3/eval/brief_cases.py goes through the shipped brief gate (`local_summary.brief_gate`) and the
gate as it was before 2026-11-22 (`BEFORE_CONTEXT`, `BEFORE_BARS`), so the replay shows both. LFM3-1.7B also writes
`DRAWS` briefs a request through the product's own path (`speech.briefed`, one retry told why), and each draft is
gated by both. A row keeps the answers and each request body's hash, so the replay says when a question changed.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path

from voice.local_decider.client import D1Client, LlamaServer, d1_server
from voice.t3 import local_summary as LS
from voice.t3.eval import brief_cases as BC
from voice.t3.speech import LocalText, _clean

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "brief_gate.json"
DRAWS = 3
BEFORE_CONTEXT = ("A voice assistant started a coding thread for a person. `request` is what the person said to ask "
                  "for it. `task` is the first message the thread's coding agent receives, written from `request` "
                  "alone. The agent has tools that can start new threads.")
BEFORE_BARS = {"adds": 0.10, "drops": 0.5, "starts": 0.5}
TEXT_PORT, GATE_PORT = 8099, 8100


def key(body):
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


def bodies(request: str, brief: str) -> dict:
    shipped = LS.brief_gate(request, brief)
    before = {**shipped, "state": {**shipped["state"], "context": BEFORE_CONTEXT}}
    return {"shipped": shipped, "before": before}


def gated(gate, request, brief):
    out = {}
    for name, body in bodies(request, brief).items():
        result = gate.call("decisions", body, label="brief_gate")
        if result["status"] != 200:
            raise RuntimeError(f"decider status {result['status']}")
        out[name] = {"hash": key(body), "answers": result["response"]["answers"]}
    return out


def written(text: LocalText, gate, request: str) -> list[dict]:
    """The writer's drafts for `request` as `speech.briefed` makes them: a first draft, and a retry told which shipped
    question refused it."""
    def write(messages):
        return _clean(LS.read_description(text.chat_gist(messages)) or "")

    first = write(LS.brief_messages(request))
    drafts = [{"draft": 1, "text": first, "gates": gated(gate, request, first) if first else None}]
    failed = (LS.brief_refusals({"status": 200, "response": {"answers": drafts[0]["gates"]["shipped"]["answers"]}})
              if first else tuple(LS.BRIEF_GATE))
    if failed:
        again = write(LS.brief_retry_messages(request, first, failed) if first else LS.brief_messages(request))
        drafts.append({"draft": 2, "text": again, "gates": gated(gate, request, again) if again else None})
    return drafts


def rows_for(text, gate):
    rows = []
    for split, cases in BC.SPLITS:
        by_name = {c["name"]: c for c in cases}
        for row in BC.briefs(cases):
            rows.append({"split": split, **row, "gates": gated(gate, by_name[row["case"]]["request"], row["text"])})
        for c in cases:
            for draw in range(DRAWS):
                rows.append({"split": split, "case": c["name"], "shape": c["shape"], "kind": "written",
                             "draw": draw, "drafts": written(text, gate, c["request"])})
            print(f"{split} {c['name']}", flush=True)
    return rows


def main():
    from voice.live.t3_tasks import LOCAL_TEXT_FLAGS, LOCAL_TEXT_MODEL
    logs = Path(tempfile.mkdtemp(prefix="brief-gate-"))
    with (LlamaServer(["-m", str(LOCAL_TEXT_MODEL), *LOCAL_TEXT_FLAGS], TEXT_PORT, logs / "text.log"),
          d1_server(port=GATE_PORT, log_path=logs / "gate.log")):
        rows = rows_for(LocalText(f"http://127.0.0.1:{TEXT_PORT}"), D1Client(f"http://127.0.0.1:{GATE_PORT}",
                                                                              timeout_s=30))
    FIXTURE.write_text(json.dumps(rows, separators=(",", ":")) + "\n", encoding="utf-8")
    print(f"{len(rows)} rows")


if __name__ == "__main__":
    main()
