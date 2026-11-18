"""First audio of a T3 line through the live server's own code: `say_verbatim`, `Conversation.speak_t3` and its
sender, to a WebSocket client, for the old whole-clip render and the streamed one.

    python -m voice.research.t3_voice.integrated --out DIR [--port 8196] [--note "..."]

The bench (`bench.py`) times each voice through a stand-in for the sender; this runs the shipped functions. The one
stand-in is the line: a `RenderedLine` comes only from `Speech.accept` with acts that match it, so `speakable` is
replaced here to take the bench's fixed lines. Times are from the `say_verbatim` call, in seconds:
`first_chunk` when the render gave its first audio, `server_first` when the sender wrote the first frame, and
`client_first` when the client read it.
"""

import argparse
import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace

import websockets

from voice.live import server
from voice.prototype import run as prototype_run
from voice.prototype.realtime import Server, Session
from voice.research.t3_voice.bench import RATE, Old, lines


class Line:
    def __init__(self, text):
        self.text = text


async def call(url, base, note):
    for module in (prototype_run, server):
        module.speakable = lambda line, entry_point: line
    renders = [("old", Old(base).stream), ("tts", prototype_run.tts_line(url))]
    voice = await Session.open(url, "voice", time.perf_counter)
    await voice.wait_for("session.created")
    accepted, client_ready = asyncio.Queue(), asyncio.Event()

    async def handler(ws):
        await accepted.put(ws)
        await ws.wait_closed()

    listener = await websockets.serve(handler, "127.0.0.1", 0)
    client = await websockets.connect(f"ws://127.0.0.1:{listener.sockets[0].getsockname()[1]}", max_size=None)
    peer = await accepted.get()
    live = SimpleNamespace(settings=server.Settings(silence_ms=700, start_ms=100, preroll_ms=300, floor_dbfs=-50.0,
                                                    margin_db=10.0, max_turn_s=30.0), turns=0)
    conversation = server.Conversation(live, peer, secret="unused")
    sender = asyncio.create_task(conversation._send_loop())
    rows = []
    try:
        for warm in renders:
            await speak(conversation, voice, client, warm, "Warm up the server before the timed lines.", -1)
        for n, line in enumerate(lines()):
            for render in renders[n % 2:] + renders[:n % 2]:
                rows.append({"line_n": n, "line": line, **await speak(conversation, voice, client, render, line, n)})
                print(json.dumps(rows[-1]), flush=True)
    finally:
        sender.cancel()
        await asyncio.gather(sender, return_exceptions=True)
        await client.close()
        listener.close()
        await voice.close()
    return {"note": note, "rows": rows}


async def speak(conversation, voice, client, render, text, n):
    name, stream = render
    turn = server.MicTurn(n, b"")
    turn.out_rate = RATE
    got = {"first": None, "bytes": 0}

    async def read():
        async for message in client:
            if isinstance(message, bytes):
                got["first"] = got["first"] or time.perf_counter()
                got["bytes"] += len(message)
            elif json.loads(message).get("type") == "reply_end":
                return

    reader = asyncio.create_task(read())
    started, since = time.perf_counter(), voice.clock()
    reply, pcm = await prototype_run.say_verbatim(
        voice, RATE, since, Line(text), lambda *args, **kwargs: None, render=stream,
        send_audio=lambda line, chunks: conversation.speak_t3(turn, line, chunks))
    done = time.perf_counter() - started
    await asyncio.wait_for(reader, 30)

    def since_start(t):
        return None if t is None else round(t - started, 4)

    return {"voice": name, "delivery": reply["delivery"], "audio_s": round(len(pcm) / 2 / RATE, 3),
            "client_bytes": got["bytes"], "first_chunk_s": round(reply["first_audio_after_s"], 4),
            "server_first_s": since_start(turn.first_audio), "client_first_s": since_start(got["first"]),
            "say_verbatim_s": round(done, 4)}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8196)
    parser.add_argument("--note", default="", help="the one line that differs from the previous run")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    print(f"run note: {args.note}", flush=True)
    with Server(args.port, args.out / "llama-server.log", ("-c", "32768"), slots=3):
        result = asyncio.run(call(f"ws://127.0.0.1:{args.port}/v1/realtime", f"http://127.0.0.1:{args.port}",
                                  args.note))
    (args.out / "integrated.json").write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
