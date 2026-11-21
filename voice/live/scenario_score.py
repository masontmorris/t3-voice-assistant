"""What a held call is scored on: the turns' records, the sandbox T3 afterwards, and a model's answer to a stated question.

Structured facts come first: the decider's tool and status, the acts a T3 line carried and whether its words were
said, the lookups that ran, the barge-in, the timings, and what the T3 holds when the call ends. Where a verdict
depends on what a free reply says, a model answers one stated question about that one reply (`Judge`). Nothing here
reads spoken text with a pattern or a word list.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Any, Callable

from voice.liquid_decider.client import LiquidClient
from voice.segmentation.client import Pacer
from voice.t3.verify import noul

FREE = ("t3", "t3_update")
JUDGE_TIMEOUT_S = 10.0
JUDGE_YES = 0.5


@dataclass(frozen=True)
class Turn:
    """One turn of the call as the server recorded it: the caller's, or news the assistant said by itself."""

    index: int
    n: int
    idle: bool
    heard: str | None
    t3: tuple[dict, ...]
    replies: tuple[tuple[str, str | None, str], ...]
    tools: tuple[dict, ...]
    cancelled: str | None
    barge_in: dict | None
    first_audio_s: float | None
    decider_calls: int

    def said(self, *kinds: str) -> bool:
        """Whether a T3 line of one of these act kinds was said (its words reached the caller) in this turn."""
        return any(e["delivery"] == "sent" and set(kinds) & set(e["kinds"]) for e in self.t3)

    @property
    def said_kinds(self) -> set[str]:
        return {k for e in self.t3 if e["delivery"] == "sent" for k in e["kinds"]}

    @property
    def free(self) -> tuple[str, ...]:
        """What the caller heard in this turn that was not a checked T3 line."""
        return tuple(text for kind, delivery, text in self.replies
                     if kind not in FREE and delivery in (None, "sent") and text)

    @property
    def refused(self) -> tuple[dict, ...]:
        """T3 lines the turn tried and did not say."""
        return tuple(e for e in self.t3 if e["delivery"] != "sent")

    @property
    def lookups(self) -> tuple[dict, ...]:
        return tuple(t for t in self.tools if t.get("outcome") == "ok")


def turn_of(index: int, record: dict, entries: Callable[[dict], list[dict]]) -> Turn:
    live = record.get("live") or {}
    return Turn(
        index=index, n=int(str(record.get("clip") or "turn_0").split("_")[-1].split(".")[0] or 0),
        idle=bool(record.get("idle")), heard=record.get("final_transcript"), t3=tuple(entries(record)),
        replies=tuple((r.get("kind"), r.get("delivery"), r.get("text")) for r in record.get("replies", [])),
        tools=tuple(e["tool"] for e in (record.get("executions") or {}).values() if e.get("tool")),
        cancelled=live.get("cancelled") or record.get("cancelled"), barge_in=live.get("barge_in"),
        first_audio_s=live.get("speech_end_to_first_audio_sent_s"),
        decider_calls=int((record.get("metrics") or {}).get("decider_live_calls") or 0))


@dataclass(frozen=True)
class Verdict:
    """One check's result. `turn` is the index of the turn it failed on, when one turn did."""

    check: str
    ok: bool
    why: str = ""
    turn: int | None = None


@dataclass
class Judge:
    """A model's answer to a stated yes-or-no question about one text, with the question and the number kept. It is
    Liquid's hosted decider, which already answers the voice's own line checks. An error is not an answer: the check
    fails with the error stated."""

    client: Any = None
    asked: list[dict] = field(default_factory=list)

    def p_yes(self, question: str, text: str, context: str) -> float | None:
        if self.client is None:
            self.client = LiquidClient(pacer=Pacer(first_s=0.0, cap_s=0.0), timeout_s=JUDGE_TIMEOUT_S, max_attempts=2)
        body = {"model": "d1.2", "state": {"context": context, "text": text},
                "questions": {"answers": {"type": "noul", "instructions": question,
                                          "criteria": {"true": "Yes.", "false": "No."}}}}
        try:
            record = self.client.call("decisions", body, label="scenario_judge")
        except Exception as exc:
            self.asked.append({"question": question, "error": f"{type(exc).__name__}: {exc}"})
            return None
        answer = noul(record, "answers")
        self.asked.append({"question": question, "p_yes": answer})
        return answer

    def yes(self, question: str, text: str, context: str, at: float = JUDGE_YES) -> bool | None:
        answer = self.p_yes(question, text, context)
        return None if answer is None else answer >= at


@dataclass
class Call:
    """Everything a scenario's checks read: the turns in order, the sandbox afterwards, and the judge."""

    turns: tuple[Turn, ...]
    world: dict
    judge: Judge
    notes: dict = field(default_factory=dict)

    def caller_turns(self) -> tuple[Turn, ...]:
        return tuple(t for t in self.turns if not t.idle)

    def news(self) -> tuple[Turn, ...]:
        return tuple(t for t in self.turns if t.idle)


@dataclass(frozen=True)
class Result:
    scenario: str
    run: int
    verdicts: tuple[Verdict, ...]
    first_audio_s: tuple[float, ...]
    decider_calls: int
    judge_calls: int
    seconds: float
    error: str | None = None
    window: tuple[float, float] = (0.0, 0.0)

    @property
    def ok(self) -> bool:
        return self.error is None and all(v.ok for v in self.verdicts)

    @property
    def failed(self) -> tuple[Verdict, ...]:
        return tuple(v for v in self.verdicts if not v.ok)


def passed(runs: list[Result], needed: int) -> bool:
    return sum(r.ok for r in runs) >= needed


def median_audio(runs: list[Result]) -> float | None:
    seconds = [s for r in runs for s in r.first_audio_s]
    return round(statistics.median(seconds), 2) if seconds else None
