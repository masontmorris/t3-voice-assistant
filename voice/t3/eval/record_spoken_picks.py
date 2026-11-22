"""Record the hosted decider's thread picks after the voice has just told the caller news of one thread, with that
thread's option marked and without, and its reading of whether words say a thread's title, so voice/t3/test_picks.py
replays real answers with no live call.

    VOICE_ENV_FILE=<env file> python -m voice.t3.eval.record_spoken_picks

The cases have the shape of the 2026-11-22 in-app call: news that a just-started thread needs permission, then a short
reply about "it" (one misheard), beside a thread whose title sounds like the misheard word; and a thread named by its
title beside a thread that a launch titled with a near synonym. Every thread and request here is made up. `SEEN` was
run while the wording of `SPOKEN_MARK` and `NAMES_QUESTION` was chosen; `HELD` was written before any of it was run.
Each case is asked `REPEATS` times.
"""

from __future__ import annotations

import json
from pathlib import Path

from voice.liquid_decider.client import LiquidClient
from voice.t3.verify import Listed, names_request, pick_request

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "spoken_picks.json"
REPEATS = 3
THREADS = (("Catalogue the Garden Plants", "waiting_on_you"), ("Proof the Release Notes", "finished"),
           ("Add a Farewell Screen", "finished"),
           ("Start a thread that adds a goodbye screen in the sandbox project.", "finished"),
           ("Add Clamp Helper", "finished"), ("Fix the flaky UI test", "working"), ("Write the onboarding guide", "idle"))
SPOKEN = "Catalogue the Garden Plants"
# (request, the thread the pick must choose: a title, or "none")
SEEN_PICKS = [
    ("Okay, it's approved.", SPOKEN),
    ("Go ahead, it's improved.", SPOKEN),
    ("What does it want?", SPOKEN),
    ("How are the release notes coming along?", "Proof the Release Notes"),
    ("Could you tell me how the farewell screen thread is going", "Add a Farewell Screen"),
]
HELD_PICKS = [
    ("Yeah, I approve it.", SPOKEN),
    ("Right, approve that one.", SPOKEN),
    ("Is the clamp helper one done?", "Add Clamp Helper"),
    ("No, I mean the garden plants thread, approve its request.", SPOKEN),
]
# (request, title, whether the words say that title)
SEEN_NAMES = [
    ("Could you tell me how the farewell screen thread is going", "Add a Farewell Screen", True),
    ("Could you tell me how the farewell screen thread is going",
     "Start a thread that adds a goodbye screen in the sandbox project.", False),
    ("How is it going?", "Add a Farewell Screen", False),
    ("The one that's waiting on me.", SPOKEN, False),
    ("No, I mean the garden plants thread, approve its request.", SPOKEN, True),
]
HELD_NAMES = [
    ("What about the clamp helper thread?", "Add Clamp Helper", True),
    ("Is the goodbye thing finished?", "Add a Farewell Screen", False),
    ("And the release notes one, is it done?", "Proof the Release Notes", True),
    ("Did that thread finish?", "Proof the Release Notes", False),
]


def listed() -> tuple[Listed, ...]:
    return tuple(Listed(f"t{i}", title, "Voice sandbox", activity) for i, (title, activity) in enumerate(THREADS, 1))


def thread_id(title: str) -> str:
    return next(t.thread_id for t in listed() if t.title == title)


def pick_cases():
    for held, batch in ((False, SEEN_PICKS), (True, HELD_PICKS)):
        for request, want in batch:
            for shown in (False, True):
                yield {"request": request, "want": want, "shown": shown, "held": held}


def name_cases():
    for held, batch in ((False, SEEN_NAMES), (True, HELD_NAMES)):
        for request, title, says in batch:
            yield {"request": request, "title": title, "says": says, "held": held}


def pick_body(case: dict) -> dict:
    return pick_request(case["request"], listed(), SPOKEN, spoken=thread_id(SPOKEN) if case["shown"] else None)


def names_body(case: dict) -> dict:
    return names_request(case["request"], next(t for t in listed() if t.title == case["title"]))


def main() -> int:
    client = LiquidClient(max_attempts=3)

    def ask(body, label, name):
        record = client.call("decisions", body, label=label, fresh=True, cache_record=False)
        answer = ((record.get("response") or {}).get("answers") or {}).get(name)
        if record["status"] != 200 or not isinstance(answer, dict):
            raise RuntimeError(f"Liquid gave an incomplete answer for {body['state']['request']!r}")
        return answer

    picks, names = [], []
    for case in pick_cases():
        samples = []
        for _ in range(REPEATS):
            answer = ask(pick_body(case), "t3_spoken_pick_fixture", "pick")
            samples.append({"choice": answer.get("choice"),
                            "p": {k: round(v, 3) for k, v in answer["probabilities"].items() if v >= 0.001}})
        picks.append({**case, "samples": samples})
        print(json.dumps({"request": case["request"], "shown": case["shown"],
                          "choices": [s["choice"] for s in samples]}), flush=True)
    for case in name_cases():
        samples = [round(ask(names_body(case), "t3_names_fixture", "names")["noul"], 3) for _ in range(REPEATS)]
        names.append({**case, "samples": samples})
        print(json.dumps({"request": case["request"], "title": case["title"], "names": samples}), flush=True)
    FIXTURE.write_bytes((json.dumps({"picks": picks, "names": names}, separators=(",", ":")) + "\n").encode())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
