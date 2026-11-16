"""End-of-turn detection for live speech: an energy-and-silence endpointer over 20 ms PCM16 frames.

llama.cpp b12428's `/v1/realtime` has no server-side voice activity detection, so the live server decides turns
itself. A frame is voiced when its level clears both an absolute floor and the tracked noise floor by a margin. The
noise floor starts at the first frame's level, follows quiet frames down at once and rises slowly, so it settles on
the room's level for any microphone. The stream must start before the speaker does.
A turn starts after `start_ms` of voiced frames and ends after `silence_ms` without one.
"""

import math
from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum

import numpy as np

RATE = 16_000
FRAME_MS = 20
FRAME_BYTES = RATE * FRAME_MS // 1000 * 2
SILENT_DBFS = -100.0


class State(StrEnum):
    IDLE = "idle"
    SPEECH = "speech"


@dataclass(frozen=True)
class Start:
    """Speech began. `pcm` is the pre-roll plus the voiced frames that started the turn."""
    pcm: bytes


@dataclass(frozen=True)
class Audio:
    pcm: bytes


@dataclass(frozen=True)
class End:
    """The turn ended. `trailing_ms` of silence or `max_turn_s` came after the last voiced frame."""
    trailing_ms: int
    forced: bool = False


def frame_dbfs(frame):
    samples = np.frombuffer(frame, dtype="<i2").astype(np.float64) / 32768
    rms = math.sqrt(float(np.mean(samples * samples))) if len(samples) else 0.0
    return 20 * math.log10(rms) if rms > 0 else SILENT_DBFS


@dataclass
class Settings:
    silence_ms: int = 700
    start_ms: int = 100
    preroll_ms: int = 300
    floor_dbfs: float = -50.0
    margin_db: float = 10.0
    noise_rise_db_per_s: float = 1.0
    max_turn_s: float = 30.0


@dataclass
class Endpointer:
    settings: Settings = field(default_factory=Settings)
    state: State = State.IDLE
    noise_dbfs: float | None = None
    _buffer: bytes = b""
    _preroll: deque = field(default_factory=deque)
    _voiced_run: int = 0
    _silent_run: int = 0
    _turn_frames: int = 0

    def threshold(self):
        return max(self.settings.floor_dbfs, self.noise_dbfs + self.settings.margin_db)

    def push(self, pcm):
        """Feed any amount of PCM16; returns the Start, Audio and End events of the whole frames it completes."""
        self._buffer += pcm
        events = []
        while len(self._buffer) >= FRAME_BYTES:
            frame, self._buffer = self._buffer[:FRAME_BYTES], self._buffer[FRAME_BYTES:]
            events.extend(self._frame(frame))
        return events

    def _reset(self):
        self.state, self._voiced_run, self._silent_run, self._turn_frames = State.IDLE, 0, 0, 0
        self._preroll.clear()

    def _frame(self, frame):
        level = frame_dbfs(frame)
        if self.noise_dbfs is None:
            self.noise_dbfs = level
        voiced = level > self.threshold()
        if not voiced:
            rise = self.settings.noise_rise_db_per_s * FRAME_MS / 1000
            self.noise_dbfs = min(level, self.noise_dbfs + rise)
        if self.state is State.IDLE:
            self._preroll.append(frame)
            self._voiced_run = self._voiced_run + 1 if voiced else 0
            start_frames = max(1, self.settings.start_ms // FRAME_MS)
            keep = start_frames + self.settings.preroll_ms // FRAME_MS
            while len(self._preroll) > keep:
                self._preroll.popleft()
            if self._voiced_run < start_frames:
                return []
            pcm = b"".join(self._preroll)
            self.state, self._silent_run, self._turn_frames = State.SPEECH, 0, len(self._preroll)
            self._preroll.clear()
            return [Start(pcm)]
        self._turn_frames += 1
        self._silent_run = 0 if voiced else self._silent_run + 1
        events = [Audio(frame)]
        if self._silent_run * FRAME_MS >= self.settings.silence_ms:
            events.append(End(self._silent_run * FRAME_MS))
            self._reset()
        elif self._turn_frames * FRAME_MS >= self.settings.max_turn_s * 1000:
            events.append(End(self._silent_run * FRAME_MS, forced=True))
            self._reset()
        return events
