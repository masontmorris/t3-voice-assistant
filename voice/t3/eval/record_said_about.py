"""Record the hosted decider's reading of whether a caller's free turn is about the thread line the voice just said
(`verify.ABOUT_LINE`), so voice/t3/test_picks.py replays real answers with no live call.

    VOICE_ENV_FILE=<env file> python -m voice.t3.eval.record_said_about

Every line and turn is made up. `SEEN` was run while the wording of `ABOUT_LINE` was chosen; `HELD` was written before
any of it was run. Each pair is asked `REPEATS` times. `about` is what the turn should be read as: a doubt about the
line or a question about it (True), or thanks, a goodbye, an acknowledgement or another subject (False).
"""

from __future__ import annotations

import json
from pathlib import Path

from voice.liquid_decider.client import LiquidClient
from voice.t3.verify import about_line_request

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "said_about.json"
REPEATS = 3
SAVE = {"thread": "Tidy the Save Slots", "status": "cancelled",
        "summary": "Its last reply said it was merging the old save slots into one list."}
CLAMP = {"thread": "Add a Clamp Helper", "status": "waiting_on_you", "need": "command_approval"}
ICONS = {"thread": "Sort the Inventory Icons", "status": "working", "summary": "It says the icons now sort by rarity."}
# (the said line's facts, the caller's next words, whether they are about the line)
SEEN = [
    (SAVE, "That does not sound right to me.", True),
    (SAVE, "Why did you say that?", True),
    (SAVE, "Where did you get that from?", True),
    (SAVE, "Okay, thanks.", False),
    (SAVE, "Okay, thanks, that is all.", False),
    (SAVE, "Can you tell me something interesting about octopuses?", False),
    (CLAMP, "Really? I thought I approved that already.", True),
    (CLAMP, "Alright, bye for now.", False),
    (CLAMP, "What time is it in Tokyo?", False),
]
HELD = [
    (ICONS, "Hmm, are you sure about that?", True),
    (ICONS, "Wait, how do you know that?", True),
    (ICONS, "I don't think that's right.", True),
    (ICONS, "Great, thank you.", False),
    (ICONS, "Okay, I'm done for today. Goodbye.", False),
    (ICONS, "By the way, what's a good name for a cat?", False),
    (CLAMP, "That seems wrong, it shouldn't need anything from me.", True),
    (CLAMP, "Cool, thanks a lot.", False),
    (CLAMP, "Let's talk about something else.", False),
]


def cases():
    for held, batch in ((False, SEEN), (True, HELD)):
        for said, words, about in batch:
            yield {"said": said, "words": words, "about": about, "held": held}


def body(case: dict) -> dict:
    return about_line_request(case["words"], case["said"])


def main() -> int:
    client = LiquidClient(max_attempts=3)
    rows = []
    for case in cases():
        samples = []
        for _ in range(REPEATS):
            record = client.call("decisions", body(case), label="t3_about_line_fixture", fresh=True,
                                 cache_record=False)
            answer = ((record.get("response") or {}).get("answers") or {}).get("about")
            if record["status"] != 200 or not isinstance(answer, dict):
                raise RuntimeError(f"Liquid gave an incomplete answer for {case['words']!r}")
            samples.append(round(answer["noul"], 3))
        rows.append({**case, "samples": samples})
        print(json.dumps({"words": case["words"], "about": case["about"], "samples": samples}), flush=True)
    FIXTURE.write_bytes((json.dumps({"rows": rows}, separators=(",", ":")) + "\n").encode())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
