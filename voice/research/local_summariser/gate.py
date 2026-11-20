"""A truth gate on local gists: the local d1-2B asked, per gist, whether the thread's last reply and status support it.

The decider classifies; it writes nothing. Each wording is one typed yes/no question, asked in its own request so its
latency is its own. `unsupported(answer, wording, threshold)` says whether a gist is dropped, in which case the voice
says the thread's status with no summary.
"""

from __future__ import annotations

from dataclasses import dataclass

from voice.t3 import types as T
from voice.t3.local_summary import GATE_CONTEXT, GATE_QUESTION

CONTEXT = GATE_CONTEXT


@dataclass(frozen=True)
class Wording:
    name: str
    question: str
    unsupported_when: bool  # the answer that means the gist is not supported


WORDINGS = (
    Wording("unsupported", GATE_QUESTION, True),
    Wording("supported", "Is everything `gist` says about the thread supported by `last_reply` and `status`?", False),
    Wording("misreads", "Does `gist` get wrong who did what, whether the work is done, or what `last_reply` says?",
            True),
)
BY_NAME = {w.name: w for w in WORDINGS}


def request(state: T.ThreadState, gist: str, wording: Wording) -> dict:
    """One decisions body asking `wording` about `gist`."""
    pending = state.pending
    facts = {"thread": state.title, "status": state.activity.value,
             "pending": pending.prompt if pending is not None else None,
             "last_reply": state.last_reply.text if state.last_reply is not None else None, "gist": gist,
             "context": CONTEXT}
    return {"model": "d1.2", "state": facts,
            "questions": {wording.name: {"type": "noul", "instructions": wording.question,
                                         "criteria": {"true": "Yes.", "false": "No."}}}}


def p_unsupported(p_yes: float, wording: Wording) -> float:
    """The probability that the gist is unsupported, whichever way the wording asks."""
    return p_yes if wording.unsupported_when else 1 - p_yes


def dropped(p_yes: float, wording: Wording, threshold: float) -> bool:
    return p_unsupported(p_yes, wording) >= threshold
