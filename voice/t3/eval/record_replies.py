"""Record the hosted decider's reading of replies to a pending confirmation, so voice/t3/test_replies.py replays real
answers with no live call.

    VOICE_ENV_FILE=<env file> python -m voice.t3.eval.record_replies

Each case is a question the voice asked, a reply, and whether that reply must act. The yes shapes are the ways people
answer ("please do" with the rising question mark a transcript gives it, "yeah go ahead", "sure"); the others are
replies that must never act: a no, a question, a different target, a yes with a condition or a change ("yes, but in the
other project"), and words that answer nothing. Each new reply goes
to Liquid's d1.2.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from voice.liquid_decider.client import LiquidClient
from voice.t3.acts import Kind, act
from voice.t3.verify import ELSEWHERE, reading_request

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "reply_reads.json"
LAUNCH = act(Kind.CONFIRM_LAUNCH, project="Voice sandbox")
STOP = act(Kind.CONFIRM_STOP, thread="Add a clamp helper")
TELL = act(Kind.CONFIRM_TELL, thread="Add a clamp helper", message="also add a test")

YES = ["Yes.", "Yeah.", "Yeah, go ahead.", "Sure.", "Sure, why not.", "Please do.", "Please do?", "Please do!",
       "Yes, please.", "Do it.", "Go for it.", "Okay.", "Okay, sounds good.", "Yep.", "Alright, go ahead.", "Uh-huh.",
       "Yes, do it.", "Yes please do?", "Mm-hmm, go on."]
NOT_YES = ["No.", "No, don't.", "Nope.", "Wait.", "Never mind.", "Hold on, not yet.",
           "What do you mean?", "How long will it take?", "Which project is that?", "Why?",
           "Can you tell me more first?",
           "Start it in the voice server instead.", "No, the other project.", "Yes, but in the server project.",
           "Actually make it about the build.",
           "What's the weather like?", "Hmm, I'm not sure.", "Maybe later.", "I'm thinking about it.", "Thanks.",
           "Hello?",
           "Yes, but in the other project.", "Yes, but only after the tests pass.", "Sure, in the voice server.",
           "Yeah, but make it about the build instead.", "Okay, if it's quick.",
           "Yes, as long as it doesn't touch the main branch.", "Yes please, and also tell the other thread."]
CASES = [(LAUNCH, reply, True) for reply in YES] + [(LAUNCH, reply, False) for reply in NOT_YES]
CASES += [(STOP, reply, True) for reply in ("Yes, stop it.", "Please do.", "Go ahead.", "Yeah.")]
CASES += [(STOP, reply, False) for reply in ("No, leave it running.", "Which one is that?", "Don't stop it.",
                                              "Yes, but wait until it finishes the run.")]
CASES += [(TELL, reply, True) for reply in ("Yes, send it.", "Please do?", "Sure.")]
CASES += [(TELL, reply, False) for reply in ("Send it to the other thread.", "What does it say?", "No.",
                                              "Yes, but add that it should keep my formatting.")]


# Each case carries the exact options sent with the question.
CASES = [(a, r, (), yes, None) for a, r, yes in CASES]
ONE = ("Voice server",)
TWO = ("Voice server", "Docs site")
CASES += [(LAUNCH, r, ONE, yes, target) for r, yes, target in (
    ("Yes.", True, None), ("Please do?", True, None), ("No.", False, None),
    ("Yes, but in the other project.", False, "Voice server"),
    ("The other project please.", False, "Voice server"), ("No, the other one.", False, "Voice server"),
    ("Put it in the voice server.", False, "Voice server"), ("What do you mean?", False, None),
    ("Yes, but only after the tests pass.", False, None), ("Actually make it about the build.", False, None),
    ("Not that one.", False, "Voice server"))]
# With two alternatives a reply that names none changes "elsewhere", which asks which one.
CASES += [(LAUNCH, r, TWO, False, target) for r, target in (("The other one.", ELSEWHERE), ("The docs one.", "Docs site"),
                                                            ("Not that one.", ELSEWHERE), ("Yes.", None))]
CASES[-1] = (*CASES[-1][:3], True, None)
PROJECT = act(Kind.ASK_WHICH, of="project", candidates=list(TWO))
THREAD = act(Kind.ASK_WHICH, of="thread", candidates=["Add a clamp helper", "Fix the flaky ASR test"])
CASES += [(PROJECT, r, TWO, False, target) for r, target in (("Voice server.", "Voice server"),
    ("Docs site.", "Docs site"), ("The first one.", "Voice server"), ("The second.", "Docs site"),
    ("Never mind.", None), ("Neither.", ELSEWHERE))]
CASES += [(THREAD, r, (), False, None) for r in ("Never mind.", "No, cancel that.", "The clamp one.",
    "Neither.", "What are they again?")]
CASES += [(a, r, (), False, None) for a in (STOP, TELL) for r in ("Never mind.", "No, cancel that.", "Neither.")]


def main() -> int:
    client = LiquidClient(max_attempts=3)
    criteria = reading_request(LAUNCH, "")["questions"]["reading"]["criteria"]
    kept = json.loads(FIXTURE.read_text(encoding="utf-8")) if FIXTURE.exists() else {}
    old = {(json.dumps(row["act"]), row["reply"], tuple(row.get("options", ()))): row for row in kept.get("rows", [])} if kept.get("criteria") == criteria else {}
    rows, took = [], []
    for said, reply, options, must_act, target in CASES:
        key = (json.dumps(said.to_json()), reply, tuple(options))
        if key in old:
            rows.append({**old[key], "must_act": must_act, "expected_target": target})
            continue
        record = client.call("decisions", reading_request(said, reply, options), label="t3_reply_fixture", fresh=True,
                             cache_record=False)
        answer = ((record.get("response") or {}).get("answers") or {}).get("reading")
        if record["status"] != 200 or not isinstance(answer, dict):
            raise RuntimeError(f"Liquid gave an incomplete answer for {reply!r}: status {record['status']}")
        target_answer = ((record.get("response") or {}).get("answers") or {}).get("target")
        if target_answer is not None:
            target_answer = {"choice": target_answer.get("choice"),
                             "probabilities": {k: round(v, 3) for k, v in target_answer["probabilities"].items()}}
        rows.append({"act": said.to_json(), "reply": reply, "options": list(options), "must_act": must_act,
                     "expected_target": target, "target_answer": target_answer, "choice": answer.get("choice"),
                     "p": {k: round(v, 3) for k, v in answer["probabilities"].items()}})
        took.append(record["timing"]["ms"])
        print(json.dumps({"reply": reply, "choice": rows[-1]["choice"], "p": rows[-1]["p"], "target": target_answer,
                          "ms": round(took[-1])}), flush=True)
    if took:
        print(json.dumps({"calls": len(took), "median_ms": round(sorted(took)[len(took) // 2])}), flush=True)
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_bytes((json.dumps({"criteria": criteria, "rows": rows}, separators=(",", ":")) + "\n").encode())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
