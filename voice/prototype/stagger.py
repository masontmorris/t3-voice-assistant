"""A faster live transcript from several ASR sessions whose text blocks end at different moments.

LFM3's live ASR writes text once per 12 audio rows (0.96 s), and the model only works with that block size
(docs/voice/asr-tick.md). Send the same audio to `k` sessions, where lane `i` first hears `i / k` of a block of
silence, and the lanes' blocks end `0.96 / k` apart. Lane 0 hears no silence and is the authority: its text is what
the call would have had with one session. A shifted lane only adds the words lane 0 has not produced yet, so the
live text still grows by appending, and lane 0's next block replaces it.
"""

import asyncio
import base64
import re
from dataclasses import dataclass, field

from voice.prototype.realtime import MODEL_RATE, Session
from voice.prototype.transcript import TranscriptAdapter
from voice.segmentation.contract import TranscriptUpdate

BLOCK_ROWS = 12
ROW_S = 0.08
LANE = "_lane"
MAX_LANES = BLOCK_ROWS


def lane_offsets_s(lanes):
    """Seconds of silence each lane hears first. Whole rows, so every lane's blocks stay on the model's row grid."""
    if lanes < 1 or BLOCK_ROWS % lanes:
        raise ValueError(f"lanes must divide {BLOCK_ROWS} rows: got {lanes}")
    return [i * (BLOCK_ROWS // lanes) * ROW_S for i in range(lanes)]


def _norm(token):
    return re.sub(r"[^a-z0-9']", "", token.lower())


def tail_after(base, lane):
    """The tokens of `lane` that follow the part of it matching `base`, and how far `lane` is from `base`.

    Aligns all of `base` to a prefix of `lane` by word edit distance and returns the rest of `lane`."""
    a, b = [_norm(t) for t in base], [_norm(t) for t in lane]
    row = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        cur = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            cur[j] = min(row[j] + 1, cur[j - 1] + 1, row[j - 1] + (a[i - 1] != b[j - 1]))
        row = cur
    end = min(range(len(b) + 1), key=lambda j: (row[j], -j))
    return lane[end:], row[end]


@dataclass
class StaggeredTranscript:
    """`TranscriptAdapter`'s interface over `lanes` sessions. Events carry their lane in `event["_lane"]`."""

    lanes: int
    revision: int = 0
    final_sent: bool = False
    base: str = ""
    extension: list = field(default_factory=list)
    adapters: list = field(default_factory=list)

    def __post_init__(self):
        lane_offsets_s(self.lanes)
        self.adapters = [TranscriptAdapter() for _ in range(self.lanes)]

    def end_input(self):
        for adapter in self.adapters:
            adapter.end_input()

    def text(self):
        return " ".join([self.base, *self.extension]).strip()

    def feed(self, event):
        if self.final_sent:
            return None
        lane = event.get(LANE, 0)
        update = self.adapters[lane].feed(event)
        if update is None:
            return None
        if lane == 0:
            self.base, self.extension = update.text, []
            return self._emit(update.is_final)
        base = self.base.split()
        tokens, distance = tail_after(base, update.text.split())
        if distance > len(base) * 0.5 + 2:
            return None
        before = self.text()
        self.extension = tokens
        return self._emit(False) if self.text() != before else None

    def finish(self):
        update = self.adapters[0].finish()
        if update is None:
            return None
        self.base, self.extension = update.text, []
        return self._emit(True)

    def _emit(self, is_final):
        self.final_sent = is_final
        self.revision += 1
        return TranscriptUpdate(self.revision, self.text(), is_final)


class StaggeredAsr:
    """`Session`'s interface over several transcription sessions fed the same audio.

    Lane 0 is the call's ASR session: its events are the ones recorded in `events` and `sent`. Lane `i` sends
    `lane_offsets_s(k)[i]` seconds of silence before its first audio, so its text blocks end between lane 0's."""

    def __init__(self, lanes, offsets):
        self.lanes, self.offsets, self.rate = lanes, offsets, MODEL_RATE
        self.name = lanes[0].name
        self._started = [False] * len(lanes)
        self._marks = [0] * len(lanes)

    @classmethod
    async def open(cls, url, name, clock, on_event, k, session_cls=Session):
        offsets = lane_offsets_s(k)

        def tagged(i):
            return lambda event: on_event({**event, LANE: i})

        lanes = [await session_cls.open(url, name if i == 0 else f"{name}{i}", clock, on_event=tagged(i)) for i in range(k)]
        return cls(lanes, offsets)

    @property
    def events(self):
        return self.lanes[0].events

    @property
    def sent(self):
        return self.lanes[0].sent

    @property
    def extra_events(self):
        return [lane.events for lane in self.lanes[1:]]

    def _silence(self, i):
        return bytes(round(self.offsets[i] * self.rate) * 2)

    async def send(self, **event):
        kind = event.get("type")
        if kind == "session.update":
            audio = (event.get("session") or {}).get("audio", {}).get("input", {}).get("format", {})
            self.rate = audio.get("rate", self.rate)
            self._marks = [len(lane.events) for lane in self.lanes]
        if kind == "input_audio_buffer.append":
            for i, lane in enumerate(self.lanes):
                await self._prime(i)
                await lane.send(**event)
            return
        for lane in self.lanes:
            await lane.send(**event)

    async def _prime(self, i):
        if not self._started[i]:
            self._started[i] = True
            if self.offsets[i]:
                await self.lanes[i].send(type="input_audio_buffer.append",
                                         audio=base64.b64encode(self._silence(i)).decode())

    async def wait_for(self, *types, after=0, timeout=120):
        hits = await asyncio.gather(*(lane.wait_for(*types, after=after if i == 0 else self._marks[i], timeout=timeout)
                                      for i, lane in enumerate(self.lanes)))
        return next((h for h in hits if h.get("type") == "error"), hits[0])

    async def stream(self, pcm, rate):
        for i in range(len(self.lanes)):
            await self._prime(i)
        results = await asyncio.gather(*(lane.stream(pcm, rate) for lane in self.lanes))
        return results[0]

    async def close(self):
        await asyncio.gather(*(lane.close() for lane in self.lanes))
