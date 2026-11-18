"""First audio, total time and exactness of a T3 line, per voice, on the synthetic lines.

    python -m voice.research.t3_voice.bench --out DIR [--port 8196] [--note "what differs from the last run"]

Starts llama-server as the live server does (LFM3-Audio Q4_K_M, three slots, every layer on the GPU), then speaks
each line of `lines.txt` three ways, in an order that rotates per line:

- `old`: the path before this change, `script.render` then `fallback.pcm`: the whole WAV from `/v1/audio/speech`.
- `tts`: `ServerTts`, the same endpoint and voice, streamed.
- `call`: `CallVoice`, the call's own voice, gated on the line's exact words, falling back to `tts` when it strays.

Each voice's chunks go through a loopback WebSocket in 40 ms frames, as the live server's sender does, so `client`
times include that transport. Every clip is saved, then transcribed by the server's own live ASR session and scored
against the line by `score.py`. Five chat replies in the call's voice are saved as `chat_N.wav`, the reference for
the speaker check (`speaker.py`). Times are from the moment the voice was asked, in seconds.
"""

import argparse
import asyncio
import json
import time
from pathlib import Path

import numpy as np
import websockets

from voice.live import fallback, script
from voice.live.overlap import transcribe
from voice.live.tts import ServerTts
from voice.prototype.realtime import MODEL_RATE, Server, Session, read_pcm16, write_pcm16
from voice.prototype.run import FORMAT_LINES, NOTE_PERSONA
from voice.research.t3_voice.call_voice import CallVoice
from voice.research.t3_voice.score import score

LINES = Path(__file__).with_name("lines.txt")
RATE = 24_000
FRAME_BYTES = RATE * 40 // 1000 * 2
PERSONA = NOTE_PERSONA + FORMAT_LINES["a"]
CHAT_ASKS = ["Hi, how are you doing today?", "Can you tell me something nice about mornings?",
             "What should I have for lunch?", "Any tips for a rainy afternoon?", "Thanks, that's all for now."]


def lines():
    return [line for line in LINES.read_text(encoding="utf-8").splitlines() if line and not line.startswith("#")]


class Old:
    engine = "old"

    def __init__(self, base):
        self.base = base

    async def stream(self, text, rate, log=None):
        started = time.perf_counter()
        _, _, blob = await asyncio.to_thread(script.render, self.base, text, ["female"],
                                             script.candidates("/v1/audio/speech"))
        pcm = fallback.pcm(blob, rate)
        if log is not None:
            log("t3_voice", engine=self.engine, first_chunk_s=round(time.perf_counter() - started, 4))
        yield pcm


class Loopback:
    """A WebSocket pair on 127.0.0.1: `send` frames on the server side, the client stamps the first frame it reads."""

    async def __aenter__(self):
        self.peer, self.ready = None, asyncio.Event()

        async def handler(ws):
            self.peer = ws
            self.ready.set()
            await ws.wait_closed()

        self.server = await websockets.serve(handler, "127.0.0.1", 0)
        port = self.server.sockets[0].getsockname()[1]
        self.client = await websockets.connect(f"ws://127.0.0.1:{port}", max_size=None)
        await self.ready.wait()
        return self

    async def __aexit__(self, *exc):
        await self.client.close()
        self.server.close()
        await self.server.wait_closed()

    async def speak(self, voice, text):
        """Stream `text` through `voice` and the socket. Returns the audio, the voice's log and the times."""
        logs, pcm, received = [], bytearray(), {"first": None, "bytes": 0, "end": None}
        started = time.perf_counter()

        async def read():
            async for frame in self.client:
                if frame == b"end":
                    break
                received["first"] = received["first"] or time.perf_counter() - started
                received["bytes"] += len(frame)
            received["end"] = time.perf_counter() - started

        reader = asyncio.create_task(read())
        first_chunk, error = None, None
        try:
            async for chunk in voice.stream(text, RATE, lambda kind, **detail: logs.append(detail)):
                first_chunk = first_chunk or time.perf_counter() - started
                pcm += chunk
                for offset in range(0, len(chunk), FRAME_BYTES):
                    await self.peer.send(chunk[offset:offset + FRAME_BYTES])
        except Exception as exc:  # noqa: BLE001 - recorded per line, the bench goes on
            error = repr(exc)
        await self.peer.send(b"end")
        await reader
        return bytes(pcm), {"engine_logs": logs, "error": error,
                            "first_chunk_s": None if first_chunk is None else round(first_chunk, 4),
                            "client_first_s": None if received["first"] is None else round(received["first"], 4),
                            "total_s": round(received["end"], 4), "audio_s": round(len(pcm) / 2 / RATE, 3)}


async def heard(url, pcm):
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
    at = np.arange(0, len(samples), RATE / MODEL_RATE)
    audio = np.interp(at, np.arange(len(samples)), samples).astype("<i2").tobytes()
    return await transcribe(url, audio + bytes(MODEL_RATE * 2), Session, timeout_s=15.0)


async def chat(url, ask):
    session = await Session.open(url, "chat", time.perf_counter)
    try:
        await session.wait_for("session.created")
        await session.send(type="session.update", session={"type": "realtime", "instructions": PERSONA})
        await session.send(type="conversation.item.create", item={
            "type": "message", "role": "user", "content": [{"type": "input_text", "text": ask}]})
        await session.send(type="response.create")
        await session.wait_for("response.done")
        return b"".join(chunk for _, chunk in session.audio)
    finally:
        await session.close()


async def bench(url, base, out, note):
    tts = ServerTts(base)
    voices = [Old(base), tts, CallVoice(url, tts, PERSONA)]
    rows = []
    async with Loopback() as loop:
        for voice in voices:
            await loop.speak(voice, "Warm up the server before the timed lines.")
        for n, line in enumerate(lines()):
            order = voices[n % 3:] + voices[:n % 3]
            for voice in order:
                pcm, row = await loop.speak(voice, line)
                name = f"{voice.engine}_{n:02d}"
                write_pcm16(out / f"{name}.wav", pcm, RATE)
                rows.append({"line_n": n, "line": line, "voice": voice.engine, "wav": f"{name}.wav", **row})
                print(json.dumps(rows[-1]), flush=True)
    for row in rows:
        pcm, _, _ = read_pcm16(out / row["wav"])
        row["asr"] = await heard(url, pcm) if len(pcm) > RATE else ""
        row.update(score(row["line"], row["asr"]))
    for n, ask in enumerate(CHAT_ASKS):
        write_pcm16(out / f"chat_{n}.wav", await chat(url, ask), RATE)
    (out / "bench.json").write_text(json.dumps({"note": note, "rows": rows}, indent=1) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8196)
    parser.add_argument("--note", default="", help="the one line that differs from the previous run")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    print(f"run note: {args.note}", flush=True)
    with Server(args.port, args.out / "llama-server.log", ("-c", "32768"), slots=3):
        asyncio.run(bench(f"ws://127.0.0.1:{args.port}/v1/realtime", f"http://127.0.0.1:{args.port}", args.out,
                          args.note))


if __name__ == "__main__":
    main()
