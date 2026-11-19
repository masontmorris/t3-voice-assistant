"""Speech over the assistant's reply: a barge-in that stops it, or a backchannel, noise or echo that must not.

The live server knows when the client is still playing a reply from `Playback`: every reply's audio is sent as soon as
it is checked, and the client plays it at real time, so the reply is audible until the sent audio's total duration
has passed. Speech that starts while a reply is audible is an `Overlap`. It keeps every frame it hears, so the words
are never lost, and it becomes a barge-in once it holds `confirm_ms` of voiced audio. An overlap that ends sooner is
transcribed once (`transcribe`) and `classify` decides: a backchannel ("mm-hm", "yeah", "okay"), noise, or the reply's
own words coming back through speakers are dismissed and the reply plays on; anything else, such as "stop", is a
barge-in that arrived late.

The voiced count uses its own absolute floor, lower than the endpointer's: on Mason's live calls the endpointer's
-50 dBFS floor counts only 260-300 ms of quiet two- and three-word turns as voiced (measured, call 2 turns 29 and 34),
so a turn would never reach the confirm length.
"""

import asyncio
import base64
import re
import time
from dataclasses import dataclass, field
from enum import StrEnum

from voice.live.endpoint import FRAME_MS, RATE

BACKCHANNEL_WORDS = frozenset({
    "mm", "mhm", "hm", "hmm", "uh", "huh", "uhhuh", "um", "uhm", "umm", "erm", "ah", "oh", "ooh", "wow",
    "yeah", "yep", "yup", "yes", "ya", "right", "okay", "ok", "kay", "sure", "alright", "cool", "nice", "true",
    "exactly", "totally", "really", "gotcha", "thanks", "great", "good", "interesting",
})
BACKCHANNEL_PHRASES = ("i see", "got it", "all right", "thank you", "makes sense", "fair enough", "oh okay",
                       "uh huh", "mm hm")
HUM = re.compile(r"^(m+h*m+|m+-?h+m+|u+h+-?h+u+h+|h+m+|u+h*m+|a+h+)$")
MAX_BACKCHANNEL_WORDS = 4


class Verdict(StrEnum):
    BARGE_IN = "barge_in"
    BACKCHANNEL = "backchannel"
    NOISE = "noise"
    ECHO = "echo"


def words(text):
    return re.findall(r"[a-z']+(?:-[a-z']+)*", (text or "").lower())


def is_backchannel(text):
    tokens = words(text)
    if not tokens or len(tokens) > MAX_BACKCHANNEL_WORDS:
        return False
    rest = " ".join(tokens)
    for phrase in BACKCHANNEL_PHRASES:
        rest = rest.replace(phrase, " ")
    return all(t in BACKCHANNEL_WORDS or HUM.match(t.replace("-", "")) for t in rest.split())


def is_echo(text, playing):
    """Most of the heard words are words of the reply that is playing: the speakers coming back into the mic."""
    heard, said = words(text), set(words(playing))
    return len(heard) >= 2 and sum(t in said for t in heard) >= 0.6 * len(heard)


def classify(text, playing):
    """The verdict on a short overlap from its transcript."""
    if not words(text):
        return Verdict.NOISE
    if is_echo(text, playing):
        return Verdict.ECHO
    if is_backchannel(text):
        return Verdict.BACKCHANNEL
    return Verdict.BARGE_IN


def played_part(pcm, rate, remaining_s):
    """The part of a reply's released audio the client has played: all of it but its last `remaining_s` seconds.

    The server sends a reply before it plays, so the time left in `Playback` maps to the tail of what was sent."""
    played_s = len(pcm) / 2 / rate - max(0.0, remaining_s)
    return bytes(pcm[:max(0, int(played_s * rate)) * 2])


@dataclass
class Playback:
    """When the reply audio already sent will have finished playing on the client, and what it says."""

    until: float = 0.0
    text: str = ""
    turn: int | None = None

    def add(self, seconds, text, now, turn):
        if now >= self.until:
            self.text = ""
        self.until = max(self.until, now) + seconds
        self.text, self.turn = f"{self.text} {text}".strip(), turn

    def playing(self, now, grace_s):
        return now < self.until + grace_s

    def stop(self, now):
        self.until = min(self.until, now)


@dataclass
class OverlapSettings:
    confirm_ms: int = 600
    floor_dbfs: float = -60.0
    margin_db: float = 10.0
    min_voiced_ms: int = 120
    grace_ms: int = 250


@dataclass
class Overlap:
    """Speech heard while a reply plays, from the endpointer's Start until a verdict. `pcm` is all of it, pre-roll
    included, so a barge-in's turn starts with the words that caused it."""

    settings: OverlapSettings
    pcm: bytearray
    turn: int
    started: float = field(default_factory=time.perf_counter)
    voiced_ms: int = 0
    task: asyncio.Task | None = None
    before_reply: bool = False
    reply_heard: bool = False

    def feed(self, pcm, level_dbfs, noise_dbfs):
        """Adds one frame. True once the overlap holds enough voiced audio to be a barge-in."""
        self.pcm += pcm
        if level_dbfs > max(self.settings.floor_dbfs, noise_dbfs + self.settings.margin_db):
            self.voiced_ms += FRAME_MS
        return self.voiced_ms >= self.settings.confirm_ms

    def record(self, verdict, how, now, transcript=None):
        return {"turn": self.turn, "verdict": str(verdict), "how": how, "voiced_ms": self.voiced_ms,
                "before_reply": self.before_reply, "reply_heard": self.reply_heard,
                "audio_s": round(len(self.pcm) / 2 / RATE, 3), "transcript": transcript,
                "start_to_verdict_s": round(now - self.started, 3)}


async def transcribe(url, pcm, session_cls, timeout_s=5.0):
    """One-shot transcript of a short clip on llama-server's transcription session."""
    from voice.prototype.transcript import TranscriptAdapter

    adapter, done = TranscriptAdapter(), asyncio.Event()

    def on_event(event):
        update = adapter.feed(event)
        if update is not None and update.is_final:
            done.set()

    session = await session_cls.open(url + "?intent=transcription", "overlap", time.perf_counter, on_event=on_event)
    try:
        await session.wait_for("session.created")
        step = RATE // 10 * 2
        for offset in range(0, len(pcm), step):
            await session.send(type="input_audio_buffer.append",
                               audio=base64.b64encode(bytes(pcm[offset:offset + step])).decode())
        adapter.end_input()
        await session.send(type="input_audio_buffer.commit", final=True)
        await asyncio.wait_for(done.wait(), timeout_s)
        return adapter.text()
    finally:
        await session.close()
