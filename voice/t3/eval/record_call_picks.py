"""Record the hosted decider's pick of a thread on a call opened from one, with the call's thread marked and without,
so voice/t3/test_picks.py replays real answers with no live call.

    VOICE_ENV_FILE=<env file> python -m voice.t3.eval.record_call_picks

The case is the in-app call's dry run (2026-11-21): a call opened on a cancelled thread beside a waiting thread with a
similar title, where "What is this thread working on right now?" was confirmed back and "Is it waiting on anything
from me?" was asked about as one of the two. The `twins` listing is the gate's harder form: the other title says
nearly the same thing and waits on the person. Every thread and request here is made up. `SEEN` was run while the
wording of `CALL_MARK` was chosen; `HELD` was written before any of it was run. Each case is asked `REPEATS` times.
"""

from __future__ import annotations

import json
from pathlib import Path

from voice.liquid_decider.client import LiquidClient
from voice.t3.verify import Listed, pick_request

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "call_picks.json"
REPEATS = 3
OTHERS = (("Add Farewell Function", "finished"), ("Add Clamp Helper", "finished"), ("Add a Sum Helper", "interrupted"),
          ("Fix the flaky UI test", "working"), ("Write the onboarding guide", "idle"))
# Each listing, newest first, and the title of the thread the call is opened on.
LISTINGS = {
    "similar": ((("Recipe App Photo Upload", "waiting_on_you"), ("Build a Recipe App", "cancelled"), *OTHERS),
                "Build a Recipe App"),
    "twins": ((("Add mute toggle next to volume", "waiting_on_you"), ("Add a Mute Toggle", "finished"), *OTHERS),
              "Add a Mute Toggle"),
}
OTHER = "Fix the flaky UI test"
CALL = None  # a case that wants the call's own thread
# (request, the thread last spoken about or CALL, the thread the pick must choose: CALL, a title, or "none")
SEEN = [
    ("What is this thread working on right now?", CALL, CALL),
    ("Is it waiting on anything from me?", CALL, CALL),
    ("How's it going?", CALL, CALL),
    ("What did it say last?", CALL, CALL),
    ("Is this one done?", CALL, CALL),
    ("What about the flaky UI test thread?", CALL, OTHER),
    ("How's the kubernetes migration?", CALL, "none"),
    ("Is it done?", OTHER, OTHER),
    ("How is it going?", OTHER, OTHER),
]
# The other title of each listing, named outright: the mark must not pull it to the call's thread.
NAMED = {"similar": "How is the photo upload thread doing?", "twins": "How is the mute toggle next to volume thread doing?"}
HELD = [
    ("Can you check on this one for me?", CALL, CALL),
    ("Has it finished yet?", CALL, CALL),
    ("Did this thread get stuck?", CALL, CALL),
    ("What's the latest on the farewell function?", CALL, "Add Farewell Function"),
    ("And what did it say?", OTHER, OTHER),
]


def listed(name: str) -> tuple[Listed, ...]:
    return tuple(Listed(f"t{i}", title, "Voice sandbox", activity)
                 for i, (title, activity) in enumerate(LISTINGS[name][0], 1))


def call_id(name: str) -> str:
    return next(t.thread_id for t in listed(name) if t.title == LISTINGS[name][1])


def cases():
    for name, (threads, call) in LISTINGS.items():
        other_title = threads[0][0]
        batches = ((False, [*SEEN, (NAMED[name], CALL, other_title)]), (True, HELD))
        for held, batch in batches:
            for request, recent, want in batch:
                for shown in (False, True):
                    yield {"listing": name, "request": request, "recent": recent or call, "want": want or call,
                           "shown": shown, "held": held}


def body(case: dict) -> dict:
    return pick_request(case["request"], listed(case["listing"]), case["recent"],
                        call_thread=call_id(case["listing"]) if case["shown"] else None)


def main() -> int:
    client = LiquidClient(max_attempts=3)
    rows = []
    for case in cases():
        samples = []
        for _ in range(REPEATS):
            record = client.call("decisions", body(case), label="t3_call_pick_fixture", fresh=True, cache_record=False)
            answer = ((record.get("response") or {}).get("answers") or {}).get("pick")
            if record["status"] != 200 or not isinstance(answer, dict):
                raise RuntimeError(f"Liquid gave an incomplete answer for {case['request']!r}")
            samples.append({"choice": answer.get("choice"),
                            "p": {k: round(v, 3) for k, v in answer["probabilities"].items() if v >= 0.001}})
        rows.append({**case, "samples": samples})
        print(json.dumps({"listing": case["listing"], "request": case["request"], "shown": case["shown"],
                          "choices": [s["choice"] for s in samples]}), flush=True)
    FIXTURE.write_bytes((json.dumps({"rows": rows}, separators=(",", ":")) + "\n").encode())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
