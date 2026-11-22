"""Record the local d1-2B's answers on briefs for made-up launch requests, for voice/t3/test_brief_gate.py.

    python -m voice.t3.eval.record_brief_gate     # starts LFM3-1.7B and the d1-2B; run it under the GPU lease

Every brief of voice/t3/eval/brief_cases.py goes through each gate of `GATES`: the gate before 2026-11-22 (`before`),
the first fix of that day (`context`, which says the start is left out on purpose), and the wordings tried after a
review found it passing briefs that drop where the work is done or a limit (`keep`, `keep_typed`). One of them is the
shipped `local_summary.brief_gate`, which the replay checks by its body. LFM3-1.7B also writes `DRAWS` first drafts a
request with the shipped prompt, and each goes through every gate. A row keeps each body's hash, so the replay says
when a question changed.
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
TEXT_PORT, GATE_PORT = 8099, 8100

BEFORE_CONTEXT = ("A voice assistant started a coding thread for a person. `request` is what the person said to ask "
                  "for it. `task` is the first message the thread's coding agent receives, written from `request` "
                  "alone. The agent has tools that can start new threads.")
CONTEXT = BEFORE_CONTEXT + (" On purpose, `task` leaves out the request to start a thread, which is already done, the "
                            "filler, and anything said to the assistant.")
KEEP = CONTEXT + (" `task` must keep what to do, where to do it (the project, app, screen, file or section named), and "
                  "every limit or prohibition the request gives, such as short, only, at most or do not.")
ADDS = "Does `task` ask for anything, or state any detail, that `request` does not?"
DROPS = "Does `task` leave out any part of the work, or any detail of it, that `request` asks for?"
STARTS = "Does `task` ask its reader to start, open or create a thread, a conversation or another agent?"
DROPS_KEEP = ("Setting aside the request to start a thread, which is done, does `task` leave out or change what to do, "
              "where to do it, or any limit or prohibition that `request` gives?")
WHERE = ("Does `task` name a different place to do the work, or a less specific one, than `request` does: the project, "
         "app, screen, file or section?")
LIMITS = ("Does `request` give a limit, size or prohibition, such as short, only, at most or do not, that `task` leaves "
          "out or changes?")
# name: (context, questions, bars)
GATES = {
    "before": (BEFORE_CONTEXT, {"adds": ADDS, "drops": DROPS, "starts": STARTS},
               {"adds": 0.10, "drops": 0.5, "starts": 0.5}),
    "context": (CONTEXT, {"adds": ADDS, "drops": DROPS, "starts": STARTS}, {"adds": 0.35, "drops": 0.6, "starts": 0.5}),
    "keep": (KEEP, {"adds": ADDS, "drops": DROPS_KEEP, "starts": STARTS}, None),
    "keep_typed": (KEEP, {"adds": ADDS, "drops": DROPS, "starts": STARTS, "where": WHERE, "limits": LIMITS}, None),
}


def key(body):
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


def gate_body(context: str, questions: dict, request: str, brief: str) -> dict:
    return {"model": "d1.2", "state": {"context": context, "request": request, "task": brief},
            "questions": {name: {"type": "noul", "instructions": question, "criteria": {"true": "Yes.", "false": "No."}}
                          for name, question in questions.items()}}


def bodies(request: str, brief: str) -> dict:
    return {name: gate_body(context, questions, request, brief) for name, (context, questions, _) in GATES.items()}


def shipped_gate(request: str, brief: str) -> str | None:
    """The name of the recorded gate whose body is the shipped `local_summary.brief_gate`, or None."""
    sent = LS.brief_gate(request, brief)
    return next((name for name, body in bodies(request, brief).items() if body == sent), None)


def gated(gate, request, brief):
    out = {}
    for name, body in bodies(request, brief).items():
        result = gate.call("decisions", body, label="brief_gate")
        if result["status"] != 200:
            raise RuntimeError(f"decider status {result['status']}")
        out[name] = {"hash": key(body), "answers": result["response"]["answers"]}
    return out


def rows_for(text, gate):
    rows = []
    for split, cases in BC.SPLITS:
        by_name = {c["name"]: c for c in cases}
        for row in BC.briefs(cases):
            rows.append({"split": split, **row, "gates": gated(gate, by_name[row["case"]]["request"], row["text"])})
        for c in cases:
            for draw in range(DRAWS):
                written = _clean(LS.read_description(text.chat_gist(LS.brief_messages(c["request"]))) or "")
                rows.append({"split": split, "case": c["name"], "shape": c["shape"], "kind": "written", "draw": draw,
                             "text": written, "gates": gated(gate, c["request"], written) if written else None})
            print(f"{split} {c['name']}", flush=True)
    return rows


def main():
    from voice.live.t3_tasks import LOCAL_TEXT_FLAGS, LOCAL_TEXT_MODEL
    logs = Path(tempfile.mkdtemp(prefix="brief-gate-"))
    with (LlamaServer(["-m", str(LOCAL_TEXT_MODEL), *LOCAL_TEXT_FLAGS], TEXT_PORT, logs / "text.log"),
          d1_server(port=GATE_PORT, log_path=logs / "gate.log")):
        rows = rows_for(LocalText(f"http://127.0.0.1:{TEXT_PORT}"), D1Client(f"http://127.0.0.1:{GATE_PORT}",
                                                                              timeout_s=30))
    FIXTURE.write_bytes((json.dumps(rows, separators=(",", ":")) + "\n").encode())
    print(f"{len(rows)} rows")


if __name__ == "__main__":
    main()
