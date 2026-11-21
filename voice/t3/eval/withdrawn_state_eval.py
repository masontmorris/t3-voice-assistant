"""Does the decider's `withdrawn_question` state change how a turn is routed, other than keeping chat out of T3?

    VOICE_ENV_FILE=<env file> python -m voice.t3.eval.withdrawn_state_eval

Each made-up turn is read three times by the hosted d1.2 through the hybrid segmenter, with no state and with a withdrawn
pick question for one thread (the state names only that thread). Five kinds of turn: chat; the same thread named again;
a request about another existing thread; a new launch; a status question that names no thread. The state may only keep
chat out of T3: every other kind must route as it does with no state. Counts are turns routed to a T3 tool, by kind, and
the tools picked, with and without.
"""

from __future__ import annotations

import collections

from voice.liquid_decider.client import LiquidClient
from voice.live.t3_tasks import T3Registry
from voice.segmentation import hybrid
from voice.segmentation.contract import TranscriptUpdate
from voice.tools import registry

TOOLS = T3Registry(registry.build(None, None, None)).choices()
STATE = {"threads": ["Add a Volume Slider"]}
T3_TOOLS = ("t3_ask", "t3_tell", "t3_stop", "t3_start", "t3_approve", "t3_deny", "t3_answer")
CASES = {
    "chat": ["Can you tell me something interesting about octopuses?", "Can we start over?",
             "Okay, hello again, how are you?", "What is two plus two?"],
    "the same thread again": ["How is the volume slider thread going?", "Check on the Add a Volume Slider thread again.",
                              "Is the volume slider thread done?", "Go back to the volume slider thread, what's its status?"],
    "another existing thread": ["How is the audio mixer thread going?", "Check on the shader cache thread.",
                                "Is the pause menu thread done?", "What's the status of the controller rumble thread?"],
    "a new launch": ["Start a new thread to speed up the shader cache.", "Can you open a thread to fix the minimap zoom?",
                     "I need a coding thread started for adding a photo mode.",
                     "Please kick off a thread that trims the intro video."],
    "a status question naming no thread": ["How are my threads doing?", "Is anything waiting on me?",
                                           "What's the status of everything?", "Is it done yet?"],
}


def tool_of(client, text: str, state, draw: int) -> str:
    segmenter = hybrid.HybridSegmenter(client, f"withdrawn-state-{draw}", tools=TOOLS)
    segmenter.conversation_mode, segmenter.withdrawn_question = True, state
    segmenter.update(TranscriptUpdate(1, text, True))
    tools = [(record.get("tool") or {}).get("tool") for record in segmenter.records()]
    return tools[0] if tools else "none"


def main() -> int:
    client = LiquidClient(max_attempts=3)
    for kind, texts in CASES.items():
        routed = {}
        for name, state in (("without", None), ("with", STATE)):
            tools = collections.Counter(tool_of(client, text, state, draw) for text in texts for draw in range(3))
            routed[name] = tools
        count = lambda tools: sum(n for tool, n in tools.items() if tool in T3_TOOLS)
        total = len(texts) * 3
        print(f"{kind:36} to a T3 tool: without {count(routed['without'])}/{total}, with {count(routed['with'])}/{total}; "
              f"without {dict(routed['without'])}; with {dict(routed['with'])}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
