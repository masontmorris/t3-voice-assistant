"""The check of a free reply against T3, run beside the reply and never in front of it.

A free reply is the voice model's own words: no draft, no slot check, no relay. When it says something about a coding
thread (its status, that something was sent, approved, started, stopped or finished), `Decider.claims` asks the local
d1-2B whether T3's records support it (voice/t3/verify.py). The reply is spoken as it is generated. The check starts when
its text is complete, which is when `response.done` arrives: text and audio finish together, and the audio plays for
seconds after (docs/voice/live-reply-check.md).

An unsupported claim is corrected, never silently dropped. What happens depends on where the caller is:

- The reply is still playing and the caller is quiet: the rest of its audio is stopped through the barge-in path
  (`stop_playback`) and the correction is spoken in its place.
- The reply has finished playing and the caller is quiet: the correction is spoken at once, as a follow-up.
- The caller is already speaking, or the reply was cut off by them: the correction waits for a quiet moment, like thread
  news (`hold`). It is issued only when what the caller heard before the cut already held an unsupported claim, judged
  again on that part of the text. A reply cut off before its claim is never corrected.

The correction is a typed act worded by the text model and checked like any T3 line (`T3Tasks.correction`).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class Plan:
    """What to do about one wrong reply: say `update` now, or hold it until the call is quiet."""

    update: Any
    hold: bool
    record: dict = field(default_factory=dict)


@dataclass
class ReplyCheck:
    """One turn's checks. `facts()` is the thread in view and what is true of it, or None; `judge(text, facts)` is the
    decider's reading (`Claim`); `correction(thread_id)` is the update that says the reply was wrong. The rest is the
    live server's view of the caller: `playing()` is (seconds of this reply the caller has heard, seconds it sent) or
    None when it was never played, `muted()` whether the caller has cut the turn off, `speaking()` whether they are
    talking now, and `stop_playback()` the barge-in path that drops what the client has buffered."""

    facts: Callable[[], Any]
    judge: Callable[[str, dict], Any]
    correction: Callable[[str, str], Any]
    playing: Callable[[], tuple[float, float] | None]
    muted: Callable[[], bool]
    speaking: Callable[[], bool]
    stop_playback: Callable[[], None]
    tasks: list = field(default_factory=list)
    records: list = field(default_factory=list)

    def watch(self, text: str) -> None:
        """Start the check of a reply whose text is complete. It runs beside the reply's playback."""
        self.tasks.append(asyncio.ensure_future(self._check(text, time.perf_counter())))

    async def settled(self) -> list[Plan]:
        """The plans of every reply watched so far, once their checks finish. A check that failed plans nothing."""
        tasks, self.tasks = self.tasks, []
        plans = await asyncio.gather(*tasks, return_exceptions=True)
        self.records.extend({"action": "check failed", "error": repr(plan)} for plan in plans
                            if isinstance(plan, BaseException))
        return [plan for plan in plans if isinstance(plan, Plan)]

    async def _check(self, text: str, started: float) -> Plan | None:
        record: dict = {"text_done_to_verdict_s": None, "action": "none"}
        found = await asyncio.to_thread(self.facts)
        if found is None:
            record["action"] = "no thread in view"
            return self._done(record)
        thread_id, facts = found
        claim = await asyncio.to_thread(self.judge, text, facts)
        record.update(unsupported=claim.unsupported, nothing=claim.nothing, ms=claim.ms, error=claim.error,
                      text_done_to_verdict_s=round(time.perf_counter() - started, 3))
        if not claim.wrong:
            return self._done(record)
        played = self.playing()
        cut = self.muted()
        heard, sent = played if played is not None else (0.0, 0.0)
        record.update(heard_s=round(heard, 2), sent_s=round(sent, 2), cut_off=cut)
        if cut:
            heard_part = _heard_words(text, heard, sent)
            if not heard_part:
                record["action"] = "cut off before the claim"
                return self._done(record)
            again = await asyncio.to_thread(self.judge, heard_part, facts)
            record["heard_unsupported"] = again.unsupported
            if not again.wrong:
                record["action"] = "cut off before the claim"
                return self._done(record)
        update = await asyncio.to_thread(self.correction, thread_id, text)
        if update is None:
            record["action"] = "thread unreadable"
            return self._done(record)
        hold = cut or self.speaking()
        if not hold and played is not None and heard < sent:
            self.stop_playback()
            record["stopped_audio_s"] = round(sent - heard, 2)
        record["action"] = "held" if hold else ("replaced the rest" if record.get("stopped_audio_s") else "follow-up")
        self.records.append(record)
        return Plan(update, hold, record)

    def _done(self, record: dict) -> None:
        self.records.append(record)
        return None

    def take_records(self) -> list[dict]:
        records, self.records = self.records, []
        return records


def _heard_words(text: str, heard_s: float, sent_s: float) -> str:
    """The start of `text` that the caller had heard when the turn was cut off, by the share of its audio that played.
    Audio lags the text it was released on (docs/voice/stream-release.md), so this errs towards more words heard."""
    words = text.split()
    if not words or sent_s <= 0 or heard_s <= 0:
        return ""
    return " ".join(words[: max(1, round(len(words) * min(1.0, heard_s / sent_s)))])
