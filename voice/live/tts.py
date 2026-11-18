"""A checked line through llama-server's TTS, played as its audio is made.

`/v1/audio/speech` with `stream_format: "audio"` answers with PCM as the model generates it, so the first 80 ms frame
can play before the rest of the line exists. Rendered whole, a T3 line waited 0.32 to 1.09 s for its last frame
before any of it played; streamed, its first chunk came 0.037 to 0.069 s after the request (measured, 20 lines,
docs/voice/t3-same-voice.md).

The speaker is the TTS `female` voice, not the call's chat voice: no TTS prompt names the chat speaker, and the only
mode that speaks in it rewords (same doc).
"""

import asyncio
import json
import re
import time
import urllib.request

import numpy as np

VOICE = "female"
# Longest wait for the next chunk. The model makes speech several times faster than it plays, so this only catches a
# stalled server.
STALL_S = 10.0


def resampled(pcm, source, rate):
    if source == rate or not pcm:
        return pcm
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
    at = np.arange(int(len(samples) * rate / source)) * source / rate
    return np.interp(at, np.arange(len(samples)), samples).astype("<i2").tobytes()


class ServerTts:
    """`stream(text, rate)` yields the line's PCM16 at `rate`, chunk by chunk, as llama-server makes it."""

    engine = "tts"

    def __init__(self, base, voice=VOICE):
        self.base, self.voice = base, voice

    async def stream(self, text, rate, log=None):
        loop, chunks, stop = asyncio.get_running_loop(), asyncio.Queue(), False
        body = json.dumps({"input": text, "voice": self.voice, "response_format": "pcm",
                           "stream_format": "audio"}).encode()
        request = urllib.request.Request(self.base + "/v1/audio/speech", data=body,
                                         headers={"Content-Type": "application/json"})

        def pump():
            try:
                with urllib.request.urlopen(request, timeout=STALL_S) as response:
                    source = int(re.search(r"rate=(\d+)", response.headers.get("Content-Type", "")).group(1))
                    carry = b""
                    while not stop and (piece := response.read1(16_384)):
                        piece, carry = carry + piece, b""
                        if len(piece) % 2:
                            piece, carry = piece[:-1], piece[-1:]
                        loop.call_soon_threadsafe(chunks.put_nowait, (source, piece))
                loop.call_soon_threadsafe(chunks.put_nowait, None)
            except Exception as exc:  # noqa: BLE001 - handed to the awaiting side, which raises it
                loop.call_soon_threadsafe(chunks.put_nowait, exc)

        started, first = time.perf_counter(), None
        worker = loop.run_in_executor(None, pump)
        try:
            while (item := await asyncio.wait_for(chunks.get(), STALL_S)) is not None:
                if isinstance(item, Exception):
                    raise item
                source, piece = item
                first = first or time.perf_counter() - started
                yield resampled(piece, source, rate)
        finally:
            stop = True
            if log is not None:
                log("t3_voice", engine=self.engine, first_chunk_s=None if first is None else round(first, 4),
                    total_s=round(time.perf_counter() - started, 4))
            await asyncio.shield(worker)
