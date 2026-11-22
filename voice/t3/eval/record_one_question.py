"""Record the hosted decider's reading of two pieces of one utterance about one thread: one question the segmenter cut
in two, or two questions of their own, so voice/t3/test_picks.py replays real answers with no live call.

    VOICE_ENV_FILE=<env file> python -m voice.t3.eval.record_one_question

Every piece and title is made up. `SEEN` was run while the wording of `ONE_QUESTION` was chosen; `HELD` was written
before any of it was run. Each pair is asked `REPEATS` times.
"""

from __future__ import annotations

import json
from pathlib import Path

from voice.liquid_decider.client import LiquidClient
from voice.t3.verify import one_question_request

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "one_question.json"
REPEATS = 3
# (first piece, second piece, the thread's title, whether they are one question)
SEEN = [
    ("Could you look at the clamp helper thread", "and say how it's doing?", "Add Clamp Helper", True),
    ("Hey, the save slots thread,", "what's it up to?", "Tidy the Save Slots", True),
    ("How is the clamp helper thread going?", "And is it waiting on me for anything?", "Add Clamp Helper", False),
    ("What did the save slots thread say last?", "And when did it start?", "Tidy the Save Slots", False),
]
HELD = [
    ("Can you check on the sprite batching thread", "and let me know where it's at?", "Batch the Sprite Draws", True),
    ("So the release notes thread,", "is it finished yet?", "Proof the Release Notes", True),
    ("Is the sprite batching thread done?", "And did it say why the frame rate dropped?", "Batch the Sprite Draws",
     False),
    ("What is the release notes thread working on?", "And has it asked me anything?", "Proof the Release Notes", False),
]


def cases():
    for held, batch in ((False, SEEN), (True, HELD)):
        for first, second, title, one in batch:
            yield {"first": first, "second": second, "title": title, "one": one, "held": held}


def body(case: dict) -> dict:
    return one_question_request(case["first"], case["second"], case["title"])


def main() -> int:
    client = LiquidClient(max_attempts=3)
    rows = []
    for case in cases():
        samples = []
        for _ in range(REPEATS):
            record = client.call("decisions", body(case), label="t3_one_question_fixture", fresh=True, cache_record=False)
            answer = ((record.get("response") or {}).get("answers") or {}).get("one")
            if record["status"] != 200 or not isinstance(answer, dict):
                raise RuntimeError(f"Liquid gave an incomplete answer for {case['first']!r}")
            samples.append(round(answer["noul"], 3))
        rows.append({**case, "samples": samples})
        print(json.dumps({"first": case["first"], "second": case["second"], "one": samples}), flush=True)
    FIXTURE.write_bytes((json.dumps({"rows": rows}, separators=(",", ":")) + "\n").encode())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
