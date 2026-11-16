"""Records what the laptop actually sent and actually played, for replay and debugging.

Why the client records. The server generates reply audio faster than the laptop plays it, and a barge-in throws away
buffered audio the person never heard. A recording rebuilt from server turns would put the reply at generation time
and include the cancelled tail. This module instead records the microphone frames as they leave for the server and
the PCM the output callback played. Both sit on one `time.perf_counter()` timeline, so the session replays as the
person experienced it.

Data shape. One session directory holds:

    microphone.wav              16 kHz mono PCM16, sent frames, silence where a capture gap was longer than 50 ms
    assistant-playback-NNN.wav  played PCM16, one file per output-rate epoch, silence inside an epoch for a pause
    conversation.wav            16 kHz mono mix of both tracks on the shared timeline
    manifest.json               origin, track rates/frames/offsets, dropped audio, finalization state
    client.jsonl                the client's control and transcript log, written by client.py

Sample i of a track sits at origin + i / rate. A track's manifest `offset_s` places its first frame on that
timeline. The writer thread owns every file handle and the final mix. The audio callback and the event loop only
enqueue, and a full queue drops a chunk with a counter rather than blocking the callback.
"""

import json
import queue
import threading
import time
import wave
from datetime import datetime
from pathlib import Path

import numpy as np

MIC_RATE = 16_000
SCHEMA = "voice.live.recording/1"
GAP_TOLERANCE_S = 0.05
BLOCK_FRAMES = MIC_RATE
DROP_KEYS = {"mic": "mic_chunks", "playback": "playback_chunks"}
FRAME_KEYS = {"mic": "mic_frames", "playback": "playback_frames"}


def unique_session_dir(parent, when=None):
    """A fresh timestamped directory. A second run in the same second gets `-01`, `-02`, and so on."""
    parent = Path(parent)
    parent.mkdir(parents=True, exist_ok=True)
    base = (when or datetime.now()).strftime("%Y%m%d-%H%M%S")
    for n in range(1000):
        path = parent / (base if n == 0 else f"{base}-{n:02d}")
        try:
            path.mkdir()
            return path
        except FileExistsError:
            continue
    raise RuntimeError(f"no free session directory under {parent} for {base}")


class _TrackWriter:
    """One mono PCM16 WAV whose frame position follows a clock. Only the writer thread touches it."""

    def __init__(self, path, rate):
        self.path, self.rate = Path(path), rate
        self._wav = wave.open(str(path), "wb")
        self._wav.setnchannels(1)
        self._wav.setsampwidth(2)
        self._wav.setframerate(rate)
        self.frames = 0

    def write_at(self, offset_s, pcm):
        frame = round(offset_s * self.rate)
        if frame > self.frames + round(GAP_TOLERANCE_S * self.rate):
            self._pad_to(frame)
        self._wav.writeframes(pcm)
        self.frames += len(pcm) // 2

    def _pad_to(self, frame):
        while self.frames < frame:
            count = min(frame - self.frames, self.rate)
            self._wav.writeframes(b"\x00" * (count * 2))
            self.frames += count

    def close(self):
        if self._wav is not None:
            self._wav.close()
            self._wav = None


def _read_resampled(reader, rate, begin, end):
    """Conversation frames [begin, end) from a track at `rate`, resampled to 16 kHz as int32."""
    count = end - begin
    if rate == MIC_RATE:
        reader.setpos(begin)
        data = reader.readframes(count)
        samples = np.frombuffer(data, dtype="<i2").astype(np.int32)
        return np.pad(samples, (0, count - samples.size))
    first = int(begin * rate / MIC_RATE)
    last = int(np.ceil(end * rate / MIC_RATE)) + 1
    reader.setpos(min(first, reader.getnframes()))
    data = reader.readframes(max(0, last - first))
    source = np.frombuffer(data, dtype="<i2").astype(np.float64)
    if source.size == 0:
        return np.zeros(count, dtype=np.int32)
    positions = np.arange(begin, end) * rate / MIC_RATE - first
    return np.interp(positions, np.arange(source.size), source).astype(np.int32)


class SessionRecorder:
    """Append-only timeline recorder. `mic` and `playback` are safe to call from the event loop and audio callback."""

    def __init__(self, root, *, mic_rate=MIC_RATE, mode="vad", input_source="mic", server=None, max_queue=512):
        self.root = Path(root)
        self.mic_rate = mic_rate
        self.mode, self.input_source, self.server = mode, input_source, server
        self.manifest_path = self.root / "manifest.json"
        self._queue = queue.Queue(maxsize=max_queue)
        self._dropped = {key: 0 for key in (*DROP_KEYS.values(), *FRAME_KEYS.values())}
        self._origin = None
        self._origin_unix = None
        self._mic = None
        self._epochs = []
        self._error = None
        self._closed = False
        self._started_at = datetime.now().astimezone()
        self._thread = threading.Thread(target=self._drain, name="voice-recording", daemon=True)
        self._thread.start()

    def mic(self, pcm, t=None):
        """A PCM16 frame sent to the server, stamped when it was captured."""
        if pcm:
            self._put("mic", time.perf_counter() if t is None else t, pcm, None)

    def playback(self, pcm, rate, t=None):
        """A PCM16 chunk actually handed to the output callback, stamped when the callback ran."""
        if pcm:
            self._put("playback", time.perf_counter() if t is None else t, pcm, rate)

    def _put(self, kind, t, pcm, rate):
        try:
            self._queue.put_nowait((kind, t, pcm, rate))
        except queue.Full:
            self._dropped[DROP_KEYS[kind]] += 1
            self._dropped[FRAME_KEYS[kind]] += len(pcm) // 2

    def _drain(self):
        while True:
            item = self._queue.get()
            if item is None:
                return
            try:
                self._write(*item)
            except Exception as exc:
                self._error = self._error or repr(exc)

    def _write(self, kind, t, pcm, rate):
        if self._origin is None:
            self._origin, self._origin_unix = t, time.time()
        if kind == "mic":
            if self._mic is None:
                self._mic = _TrackWriter(self.root / "microphone.wav", self.mic_rate)
            self._mic.write_at(t - self._origin, pcm)
            return
        if not self._epochs or self._epochs[-1]["rate"] != rate:
            path = self.root / f"assistant-playback-{len(self._epochs) + 1:03d}.wav"
            self._epochs.append({"rate": rate, "start_s": max(0.0, t - self._origin),
                                 "writer": _TrackWriter(path, rate)})
        epoch = self._epochs[-1]
        epoch["writer"].write_at(t - self._origin - epoch["start_s"], pcm)

    def _write_conversation(self):
        tracks = []
        total = self._mic.frames if self._mic else 0
        for epoch in self._epochs:
            writer = epoch["writer"]
            start = round(epoch["start_s"] * MIC_RATE)
            frames = round(writer.frames * MIC_RATE / writer.rate)
            tracks.append((start, frames, writer.rate, writer.path))
            total = max(total, start + frames)
        if total == 0:
            return None, 0
        path = self.root / "conversation.wav"
        mic = wave.open(str(self._mic.path), "rb") if self._mic else None
        readers = []
        try:
            for _start, _frames, rate, track_path in tracks:
                reader = wave.open(str(track_path), "rb")
                readers.append((rate, reader))
            with wave.open(str(path), "wb") as out:
                out.setnchannels(1)
                out.setsampwidth(2)
                out.setframerate(MIC_RATE)
                for block in range(0, total, BLOCK_FRAMES):
                    size = min(BLOCK_FRAMES, total - block)
                    if mic is None:
                        mix = np.zeros(size, dtype=np.int32)
                    else:
                        data = mic.readframes(size)
                        mix = np.frombuffer(data, dtype="<i2").astype(np.int32)
                        mix = np.pad(mix, (0, size - mix.size))
                    for (start, frames, rate, _path), (_rate, reader) in zip(tracks, readers):
                        lo, hi = max(block, start), min(block + size, start + frames)
                        if lo < hi:
                            mix[lo - block:hi - block] += _read_resampled(reader, rate, lo - start, hi - start)
                    out.writeframes(np.clip(mix, -32768, 32767).astype("<i2").tobytes())
        finally:
            if mic is not None:
                mic.close()
            for _rate, reader in readers:
                reader.close()
        return path, total

    def _track_entries(self):
        entries = []
        if self._mic is not None:
            entries.append({"kind": "microphone", "file": "microphone.wav", "rate": self.mic_rate,
                            "frames": self._mic.frames, "offset_s": 0.0,
                            "duration_s": round(self._mic.frames / self.mic_rate, 3)})
        for epoch in self._epochs:
            writer = epoch["writer"]
            entries.append({"kind": "assistant_playback", "file": writer.path.name, "rate": writer.rate,
                            "frames": writer.frames, "offset_s": round(epoch["start_s"], 3),
                            "duration_s": round(writer.frames / writer.rate, 3)})
        return entries

    def finalize(self):
        """Stop the writer, close the tracks, mix the conversation, and write the manifest. Safe to call twice."""
        if self._closed:
            return self.manifest_path
        self._closed = True
        self._queue.put(None)
        self._thread.join(timeout=30)
        if self._thread.is_alive():
            self._error = self._error or "recording writer did not stop within 30 s"
        for epoch in self._epochs:
            epoch["writer"].close()
        if self._mic is not None:
            self._mic.close()
        error = self._error
        conversation, frames = None, 0
        try:
            conversation, frames = self._write_conversation()
        except Exception as exc:
            error = error or repr(exc)
        manifest = {
            "schema": SCHEMA,
            "started_at": self._started_at.isoformat(timespec="milliseconds"),
            "finalized_at": datetime.now().astimezone().isoformat(timespec="milliseconds"),
            "origin": None if self._origin is None else {"monotonic_s": self._origin,
                                                         "unix_s": self._origin_unix},
            "mic_rate": self.mic_rate,
            "channels": 1,
            "dtype": "int16",
            "mode": self.mode,
            "input_source": self.input_source,
            "server": self.server,
            "conversation": None if conversation is None else
                            {"file": conversation.name, "rate": MIC_RATE, "frames": frames,
                             "duration_s": round(frames / MIC_RATE, 3)},
            "tracks": self._track_entries(),
            "dropped": dict(self._dropped),
            "finalized": True,
            "finalize_error": error,
            "notes": "Microphone is the PCM16 sent to the server, including frames sent while PTT is up. "
                     "Playback is the PCM16 handed to the output callback, so a barge-in tail is absent. "
                     "WAV input records the simulated input, not a microphone.",
        }
        self.manifest_path.write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")
        return self.manifest_path
