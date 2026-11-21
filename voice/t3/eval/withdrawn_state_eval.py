"""Does the decider's `withdrawn_question` state suppress a request the caller wants, or only keep chat out of T3?

    VOICE_ENV_FILE=<env file> python -m voice.t3.eval.withdrawn_state_eval

Ten made-up turns, each read twice by the hosted d1.2 through the hybrid segmenter, with no state and with a withdrawn
pick question for a thread: four that are chat and six that name the thread again or ask the same thing again. Counts
are turns routed as the case says (a T3 tool for the six, none for the four).
"""

from __future__ import annotations

import collections

from voice.liquid_decider.client import LiquidClient
from voice.live.t3_tasks import T3Registry
from voice.segmentation import hybrid
from voice.segmentation.contract import TranscriptUpdate
from voice.tools import registry

TOOLS = T3Registry(registry.build(None, None, None)).choices()
STATE = {"asked": "whether this is the thread they meant", "threads": ["Add a Volume Slider"]}
T3_TOOLS = ("t3_ask", "t3_tell", "t3_stop", "t3_start", "t3_approve", "t3_deny", "t3_answer")
CASES = [("chat", "Can you tell me something interesting about octopuses?"), ("chat", "Can we start over?"),
         ("chat", "Okay, hello again, how are you?"), ("chat", "What is two plus two?"),
         ("thread", "How is the volume slider thread going?"), ("thread", "Check on the Add a Volume Slider thread again."),
         ("thread", "Actually, how is that volume slider one doing?"), ("thread", "Is the volume slider thread done?"),
         ("thread", "Tell me where the volume slider thread is at."),
         ("thread", "Go back to the volume slider thread, what's its status?")]


def routed_to_t3(client, text: str, state, draw: int) -> bool:
    segmenter = hybrid.HybridSegmenter(client, f"withdrawn-state-{draw}", tools=TOOLS)
    segmenter.conversation_mode, segmenter.withdrawn_question = True, state
    segmenter.update(TranscriptUpdate(1, text, True))
    tools = [(record.get("tool") or {}).get("tool") for record in segmenter.records()]
    return bool(tools) and tools[0] in T3_TOOLS


def main() -> int:
    client = LiquidClient(max_attempts=3)
    for name, state in (("without the state", None), ("with the state", STATE)):
        counts = collections.Counter()
        for want, text in CASES:
            for draw in range(2):
                counts[want, routed_to_t3(client, text, state, draw) == (want == "thread")] += 1
        print(name, {f"{want} {'as expected' if ok else 'not'}": n for (want, ok), n in sorted(counts.items())})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
