"""Record the hosted decider's reading of replies to a pending confirmation, so voice/t3/test_replies.py replays real
answers with no live call.

    VOICE_ENV_FILE=<env file> python -m voice.t3.eval.record_replies

Each case is a question the voice asked, a reply, and whether that reply must act. The yes shapes are the ways people
answer ("please do" with the rising question mark a transcript gives it, "yeah go ahead", "sure"); the others are
replies that must never act: a no, a question, a different target, and words that answer nothing. Each new reply goes
to Liquid's d1.2.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from voice.liquid_decider.client import LiquidClient
from voice.t3.acts import Kind, act
from voice.t3.verify import reading_request

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
           "Hello?"]
CASES = [(LAUNCH, reply, True) for reply in YES] + [(LAUNCH, reply, False) for reply in NOT_YES]
CASES += [(STOP, reply, True) for reply in ("Yes, stop it.", "Please do.", "Go ahead.", "Yeah.")]
CASES += [(STOP, reply, False) for reply in ("No, leave it running.", "Which one is that?", "Don't stop it.")]
CASES += [(TELL, reply, True) for reply in ("Yes, send it.", "Please do?", "Sure.")]
CASES += [(TELL, reply, False) for reply in ("Send it to the other thread.", "What does it say?", "No.")]


def main() -> int:
    client = LiquidClient(max_attempts=3)
    kept = json.loads(FIXTURE.read_text(encoding="utf-8")) if FIXTURE.exists() else []
    out = []
    for said, reply, must_act in CASES:
        body = reading_request(said, reply)
        old = next((row for row in kept if row["acts"] == [said.to_json()] and row["reply"] == reply
                    and row["asked"] == body["state"]["assistant_asked"]
                    and row["criteria"] == body["questions"]["reading"]["criteria"]), None)
        if old is not None:
            out.append({**old, "must_act": must_act})
            continue
        record = client.call("decisions", body, label="t3_reply_fixture", fresh=True, cache_record=False)
        answers = (record.get("response") or {}).get("answers")
        if record["status"] != 200 or not isinstance(answers, dict) or "reading" not in answers:
            raise RuntimeError(f"Liquid gave an incomplete answer for {reply!r}: status {record['status']}")
        out.append({"acts": [said.to_json()], "reply": reply, "must_act": must_act,
                    "asked": body["state"]["assistant_asked"], "criteria": body["questions"]["reading"]["criteria"],
                    "status": record["status"], "answers": answers})
        print(json.dumps({"reply": reply, "choice": answers["reading"].get("choice"),
                          "yes": round(answers["reading"]["probabilities"].get("yes", 0), 3)}), flush=True)
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_bytes((json.dumps(out, indent=2) + "\n").encode())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
