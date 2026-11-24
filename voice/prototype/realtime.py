"""llama.cpp's `/v1/realtime` server and sessions, for the prototype.

The server flags and the protocol steps are the ones `voice/research/speech/llamacpp_rt.py` measured in Stage 1. That script
parses its arguments and imports torch on import, and it reads events only while it waits, so this module keeps a
background reader per session instead. Each session hands every event to a callback as it arrives.
"""

import asyncio
import base64
import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path

import numpy as np
import soundfile as sf
import websockets

VOICE_HOME = Path(os.environ.get("VOICE_HOME", Path.home() / "voice"))
SERVER = VOICE_HOME / "llamacpp" / "bin" / "llama-server.exe"
GGUF = VOICE_HOME / "gguf"
MODEL_RATE = 16_000


def read_pcm16(path):
    """PCM16 bytes, sample rate, and duration in seconds. The server resamples any rate the session declares."""
    samples, rate = sf.read(str(path), dtype="float32", always_2d=True)
    mono = samples.mean(axis=1)
    return (np.clip(mono, -1, 1) * 32767).astype("<i2").tobytes(), rate, len(mono) / rate


def write_pcm16(path, pcm, rate):
    sf.write(str(path), np.frombuffer(pcm, dtype="<i2"), rate)


class Server:
    """Starts llama-server with every layer on the GPU and stops that one process on exit."""

    def __init__(self, port, log_path, extra_args=(), slots=2, binary=SERVER):
        self.port, self.log_path, self.proc, self.extra_args, self.slots = port, log_path, None, list(extra_args), slots
        self.binary = binary

    def __enter__(self):
        # `--temp 0` only repeats the text sampling the GGUF already asks for (general.sampling.temp 0, top_k 1).
        # Audio codes are sampled from the projector's own settings whatever this says, with a fresh seed per
        # response, so two runs differ (docs/voice/spoken-acts.md, "The prompt format").
        command = [str(self.binary), "-m", str(GGUF / "LFM3-Audio-2B-Q4_K_M.gguf"),
                   "-mm", str(GGUF / "mmproj-LFM3-Audio-2B-Q8_0.gguf"),
                   "--host", "127.0.0.1", "--port", str(self.port), "--temp", "0", "-np", str(self.slots), "-ngl", "99",
                   *self.extra_args]
        self.log = open(self.log_path, "w")
        started = time.perf_counter()
        self.proc = subprocess.Popen(command, stdout=self.log, stderr=subprocess.STDOUT)
        self.log_path.with_suffix(".pid").write_text(str(self.proc.pid))
        self._wait_healthy()
        self.startup_s = time.perf_counter() - started
        return self

    def _wait_healthy(self, timeout=180):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"llama-server exited with {self.proc.returncode}, see {self.log_path}")
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/health", timeout=2) as response:
                    if response.status == 200:
                        return
            except OSError:
                pass
            time.sleep(0.5)
        raise RuntimeError("llama-server did not become healthy")

    def __exit__(self, *exc):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=30)
        self.log.close()
        self.log_path.with_suffix(".pid").unlink(missing_ok=True)


def _loggable(event):
    """The event as recorded: audio payloads become their byte count."""
    out = dict(event)
    if "audio" in out:
        out["audio"] = {"bytes": len(base64.b64decode(out["audio"]))}
    if out.get("type") == "response.output_audio.delta":
        out["delta"] = {"bytes": len(base64.b64decode(out["delta"]))}
    return out


class Session:
    """One realtime WebSocket. A background task reads every event, stamps it with `clock()`, and records it."""

    def __init__(self, ws, name, clock, on_event=None):
        self.ws, self.name, self.clock, self.on_event = ws, name, clock, on_event
        self.events, self.sent, self.audio = [], [], []
        self._changed = asyncio.Condition()
        self._reader = asyncio.create_task(self._read())

    @classmethod
    async def open(cls, url, name, clock, on_event=None):
        return cls(await websockets.connect(url, max_size=None), name, clock, on_event)

    async def _read(self):
        try:
            async for raw in self.ws:
                event = json.loads(raw)
                event["_t"] = self.clock()
                if event.get("type") == "response.output_audio.delta":
                    self.audio.append((len(self.events), base64.b64decode(event["delta"])))
                self.events.append(_loggable(event))
                if self.on_event is not None:
                    self.on_event(event)
                async with self._changed:
                    self._changed.notify_all()
        finally:
            async with self._changed:
                self._changed.notify_all()

    async def send(self, **event):
        self.sent.append({**_loggable(event), "_t": self.clock()})
        await self.ws.send(json.dumps(event))

    async def wait_for(self, *types, after=0, timeout=120):
        """The first event at index `after` or later whose type is one of `types`."""
        async def find():
            async with self._changed:
                while True:
                    hit = next((e for e in self.events[after:] if e.get("type") in types), None)
                    if hit is not None:
                        return hit
                    if self.ws.state is websockets.State.CLOSED:
                        raise ConnectionError(f"{self.name} closed while waiting for {types}")
                    await self._changed.wait()
        return await asyncio.wait_for(find(), timeout)

    async def stream(self, pcm, rate, chunk_ms=100):
        """Sends PCM16 in chunks paced at real time. Returns the clock times at which the audio starts and ends."""
        step = rate * chunk_ms // 1000 * 2
        start = self.clock()
        for offset in range(0, len(pcm), step):
            await self.send(type="input_audio_buffer.append", audio=base64.b64encode(pcm[offset:offset + step]).decode())
            await asyncio.sleep(max(0.0, start + (offset + step) / 2 / rate - self.clock()))
        return start, self.clock()

    async def close(self):
        try:
            await self.ws.close()
        except BaseException:
            self.ws.transport.abort()
            raise
        finally:
            self._reader.cancel()
