"""Record the hosted decider's answers for the line-check fixture: the reviewer's three escapes, the status claims
code's word lists used to refuse, and one right line for each, so voice/t3/test_verify.py replays real answers
with no live call.

    VOICE_ENV_FILE=<env file> python -m voice.t3.eval.record_verify

Run it again after a change to the questions in voice/t3/verify.py. One request per case to Liquid's d1.2.
"""

from __future__ import annotations

import json
from pathlib import Path

from voice.liquid_decider.client import LiquidClient
from voice.t3.acts import Kind, act
from voice.t3.verify import request

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "verify_lines.json"

CASES = [
    ("a stop confirm", act(Kind.CONFIRM_STOP, thread="Alpha"), "Alpha is complete?", "Want me to stop Alpha?"),
    ("a working status", act(Kind.THREAD_STATUS, thread="Alpha", status="working"), "Alpha has failed.",
     "Alpha is still working on it."),
    ("Alpha's run status", act(Kind.RUN_UPDATE, thread="Alpha", status="running"),
     "Beta is running. Alpha is unrelated.", "Alpha is running now."),
]
T = "Add a clamp helper"
# The status claims code's word lists refused before the decider replaced them, each beside its act's right line.
CLAIMS = [
    (act(Kind.CONFIRM_LAUNCH, project="Voice sandbox"), "It started in Voice sandbox, want more?",
     "Should I start that in Voice sandbox?"),
    (act(Kind.CONFIRM_TELL, thread=T, message="also add a test"),
     f"I sent {T} the note to also add a test, okay?", f"Should I tell {T} to also add a test?"),
    (act(Kind.CONFIRM_STOP, thread=T), f"I've stopped {T}, right?", f"Want me to stop {T}?"),
    (act(Kind.CANCELLED, op="stop", thread=T), f"Okay, {T} is stopped.", f"Okay, I'll leave {T} running."),
    (act(Kind.THREAD_STATUS, thread=T, status="working", summary="It added the helper."), f"{T} has finished.",
     f"{T} is still working, and it has added the helper."),
    (act(Kind.THREAD_STATUS, thread=T, status="working", summary="It added the helper."), f"{T} was stopped.",
     None),
    (act(Kind.TELL_PENDING, thread=T), f"I passed that on to {T}.", f"I've asked T3 to pass that to {T}."),
    (act(Kind.HELD_FOLLOWUP, thread=T), f"{T} has finished, so I sent it.",
     f"{T} is busy, so I'll hold that until its run ends."),
    (act(Kind.DROPPED_FOLLOWUP, thread=T), f"I've passed that on to {T}.", f"Okay, I won't give that to {T}."),
    (act(Kind.STOPPING, thread=T), f"{T} has stopped.", f"I'm stopping {T} now."),
    (act(Kind.NOTHING_RUNNING, thread=T), f"I stopped {T}.", f"{T} isn't doing anything right now."),
    (act(Kind.NOTHING_RUNNING, thread=T), f"{T} is still running.", None),
    (act(Kind.STOP_UNCONFIRMED, thread=T), f"{T} was stopped.", f"I couldn't reach T3 to stop {T}, so use the app."),
    (act(Kind.RUN_UPDATE, thread=T, status="running"), f"{T} is done.", f"{T} is on it now."),
    (act(Kind.REPLIED, thread=T, status="running", reply="I added clamp."), f"{T} finished adding clamp.",
     f"{T} says it added clamp."),
    (act(Kind.DONE, thread=T, status="failed"), f"{T} is still working.", f"{T} failed partway through."),
    (act(Kind.REFUSED, op="start", code="refused", reason="Model is off."), "T3 started it.",
     "T3 wouldn't start it because the model is off."),
]
CASES += [(f"claim {said.kind.value}", said, wrong, right) for said, wrong, right in CLAIMS]
SMOKE = "Add a Clamp Helper"
# Right lines the decider refused in the Replay smoke run (2026-11-18), each beside a wrong line for its act.
CASES += [
    ("smoke replied", act(Kind.REPLIED, thread=SMOKE, status="running",
                          reply="I added clamp next to sum. All 12 tests pass."),
     f"{SMOKE} has finished: it added clamp next to sum.", "It added clamp next to sum and the tests pass."),
    # The PM's two replied cases (2026-11-18), recorded TAKES times each, since one d1.2 answer can drift.
    ("pm replied finished work", act(Kind.REPLIED, thread=SMOKE, status="running",
                                     reply="I added clamp next to sum. All 12 tests pass."),
     None, "It added clamp, and the tests pass."),
    ("pm replied says done", act(Kind.REPLIED, thread=SMOKE, status="running",
                                 reply="I added clamp next to sum. All 12 tests pass."),
     f"{SMOKE} is done: it added clamp, and the tests pass.", None),
    ("smoke sent_followup", act(Kind.SENT_FOLLOWUP, thread=SMOKE), f"Your message is still waiting for {SMOKE}.",
     f"Your message reached {SMOKE}."),
]


TAKES = 3


def main() -> int:
    """Record each case's lines. A row already recorded for the same line, acts and questions is kept as it is, so a
    change to some questions re-asks only the lines it touches."""
    client = LiquidClient(max_attempts=3)
    kept = json.loads(FIXTURE.read_text(encoding="utf-8")) if FIXTURE.exists() else []
    out = []
    for name, said, escape, right in CASES:
        for line, should_pass in ((escape, False), (right, True)):
            if line is None:
                continue
            body = request([said], line)
            takes = TAKES if name.startswith("pm ") else 1
            old = [row for row in kept if row["line"] == line and row["acts"] == [said.to_json()]
                   and set(row["answers"] or ()) == set(body["questions"])]
            for take in range(takes):
                if take < len(old):
                    out.append({**old[take], "case": name, "take": take + 1})
                    continue
                record = client.call("decisions", body, label="t3_line_fixture", fresh=True, cache_record=False)
                out.append({"case": name, "take": take + 1, "acts": [said.to_json()], "line": line,
                            "should_pass": should_pass, "status": record["status"],
                            "answers": (record.get("response") or {}).get("answers"),
                            "ms": round(record["timing"]["ms"], 1)})
                print(json.dumps(out[-1]), flush=True)
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_bytes((json.dumps(out, indent=2) + "\n").encode())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
