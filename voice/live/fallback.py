"""Fixed lines rendered once in the server's own TTS voice: the line for a turn whose reply failed the fact check
twice, and the question for a correction that names no value.

The turn cannot speak the model's second answer: on the recorded demo it was "I do not have the information needed
to answer the fact check.", which names the machinery and says nothing useful. It cannot ask a third time either,
because a third answer can be just as wrong. So the line is fixed text, rendered before any turn starts, and a turn
plays it at most once.

The clip is the running llama-server's own TTS (`voice/live/script.py`), the same `female` speaker T3 lines use,
which is not the call's chat voice (docs/voice/t3-same-voice.md). It is kept under `$VOICE_HOME/live/` so later
processes pay nothing. Rendering is the only part that needs the server; `pcm` is pure code. When rendering fails the
turn speaks nothing, and the record says so.
"""

import hashlib
import io
import wave
from pathlib import Path

import numpy as np

from voice.live import script
from voice.prototype.realtime import VOICE_HOME

CACHE = VOICE_HOME / "live"


def cache_path(line: str) -> Path:
    """Where the clip for `line` is kept. The name is the line's hash, so changed wording is a new clip."""
    return CACHE / f"fallback-{hashlib.sha256(line.encode()).hexdigest()[:12]}.wav"


def wav(server: str, line: str) -> bytes:
    """The line as a WAV in the server's voice, rendered on first use and read from disk after that."""
    path = cache_path(line)
    if path.is_file():
        return path.read_bytes()
    try:
        offered = script.voices(server)
        _, _, blob = script.render(server, line, offered or ["female"], script.candidates())
    except SystemExit as stop:
        raise OSError(f"no TTS for the fallback line: {stop}") from stop
    if blob[:4] != b"RIFF":
        raise OSError("the TTS answered with bare PCM and no WAV header")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(blob)
    return blob


class Clip:
    """One fixed line, rendered before any client connects, as PCM at each session rate it is asked for. Empty when it
    could not render, so a turn says the line in its record and plays nothing rather than another voice."""

    def __init__(self, line: str):
        self.line, self.wav, self.error, self.by_rate = line, b"", "not rendered yet", {}

    def render(self, server: str):
        try:
            self.wav, self.error = wav(server, self.line), None
        except (OSError, ValueError) as exc:
            self.error = repr(exc)
        print(f"fixed line {'ready' if self.wav else 'unavailable'}: {self.line!r}"
              f"{'' if self.wav else f' ({self.error})'}", flush=True)

    def pcm(self, rate: int) -> bytes:
        """The clip at `rate`, converted once per rate. Empty when no clip was rendered."""
        if not self.wav or not rate:
            return b""
        if rate not in self.by_rate:
            try:
                self.by_rate[rate] = pcm(self.wav, rate)
            except (ValueError, EOFError) as exc:
                self.error, self.by_rate[rate] = repr(exc), b""
        return self.by_rate[rate]


def pcm(blob: bytes, rate: int) -> bytes:
    """`blob` as mono PCM16 at `rate`, the session's own output rate, so the clip plays without a second player."""
    with wave.open(io.BytesIO(blob)) as source:
        if source.getnchannels() != 1 or source.getsampwidth() != 2:
            raise ValueError("the fallback clip is not mono PCM16")
        source_rate = source.getframerate()
        samples = np.frombuffer(source.readframes(source.getnframes()), dtype="<i2").astype(np.float32)
    if source_rate != rate:
        at = np.arange(int(len(samples) * rate / source_rate)) * source_rate / rate
        samples = np.interp(at, np.arange(len(samples)), samples)
    return np.clip(samples, -32768, 32767).astype("<i2").tobytes()
