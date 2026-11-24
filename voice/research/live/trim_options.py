"""What it costs to make the voice session hold only what the caller heard of an interrupted reply, measured on the
real LFM3 server with synthetic speech.

    python -m voice.research.live.trim_options --out DIR --bin PATH [--trials 5]

`--bin` is a llama-server with the truncate patch (docs/voice/barge-in.md). Every arm builds the same three-turn call
on a fresh session: two short questions and a long request, each answered, the long reply being the one the caller
talks over. Then the caller's next turn ("go on") is answered and the time from the first step after the long reply
to the next reply's first audio is kept.

- `control`: nothing changes; the session holds the whole long reply.
- `truncate` (b): `conversation.item.truncate` puts back the state saved when the long reply began and keeps its first
  HEARD_WORDS words as text.
- `seed` (c): a new session, its instructions set, the call so far put in as one text item with the long reply cut to
  the same words, then the turn.
- `cut-only` (a): the same truncate on a server started with LLAMA_RT_REWIND_CUT_ONLY=1, which only cuts the sequence
  at the reply's start, as a KV rewind with no saved state would. Once.
- `slot-save` (d): llama-server's own `POST /slots/{id}?action=save` on the slot that holds the session. Once.

The server's own log lines give the checkpoint's size and time per response, and the rewind's. Each arm's next reply
is kept as a WAV for the one-voice check (`voice.research.live.one_voice`). Records stay under --out, outside the repo.
"""

import argparse
import asyncio
import base64
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np
import websockets

from voice.prototype.realtime import MODEL_RATE, Server, Session, read_pcm16, write_pcm16
from voice.prototype.run import FORMAT_LINES, NOTE_PERSONA, RESPONSE_CHECKPOINTS, last_item, plain_text, update_session

PORT = 8121
ARMS = ("control", "truncate", "seed", "slot-save", "cut-only")
CHUNK_BYTES = 32000
HEARD_WORDS = 12
LINES = ("Hi there. Can you tell me a little about the history of lighthouses?",
         "That's interesting. How did the keepers live back then?",
         "Now can you walk me through how to bake a loaf of bread at home, step by step, in detail?",
         "Sorry, someone was at the door. Please go on.")
LONG = 2
LOG_LINE = re.compile(r"realtime: (checkpoint \d+ at n_past = (\d+), ([\d.]+) MiB, ([\d.]+) ms|rewound checkpoint "
                      r"\d+ from n_past = (\d+) to (\d+), ([\d.]+) ms|heard text in, (\d+) tokens, ([\d.]+) ms|"
                      r"cut-only rewind.*seq_rm (\w+))")


def render(clips, port):
    clips.mkdir(parents=True, exist_ok=True)
    turns = clips / "turns.txt"
    turns.write_text("\n".join(LINES) + "\n", encoding="utf-8")
    subprocess.run([sys.executable, "-m", "voice.live.script", "--turns", str(turns), "--out", str(clips), "--server",
                    f"http://127.0.0.1:{port}"], check=True)
    return [at_model_rate(path) for path in sorted(clips.glob("turn_*.wav"))]


def at_model_rate(path):
    pcm, rate, _ = read_pcm16(path)
    if rate == MODEL_RATE:
        return pcm
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
    positions = np.arange(0, len(samples), rate / MODEL_RATE)
    return np.interp(positions, np.arange(len(samples)), samples).astype("<i2").tobytes()


async def opened(url, clock, checkpoints=0):
    """A session with the shipped instructions; only one that may be truncated asks for response checkpoints."""
    session = await Session.open(url, "voice", clock)
    created = await session.wait_for("session.created")
    await update_session(session, {"type": "realtime", "instructions": NOTE_PERSONA + FORMAT_LINES["c"],
                                   "output_modalities": ["text", "audio"],
                                   **({"response_checkpoints": checkpoints} if checkpoints else {})})
    return session, created["session"]["audio"]["output"]["format"]["rate"]


async def say(session, pcm):
    for offset in range(0, len(pcm), CHUNK_BYTES):
        await session.send(type="input_audio_buffer.append",
                           audio=base64.b64encode(pcm[offset:offset + CHUNK_BYTES]).decode())


async def respond(session, started):
    """One response: its text, its item, its audio, and its first audio's time after `started`."""
    mark = len(session.events)
    await session.send(type="response.create")
    done = await session.wait_for("response.done", "error", after=mark, timeout=300)
    events = session.events[mark:]
    first = next((e["_t"] for e in events if e["type"] == "response.output_audio.delta"), None)
    item = next((e.get("item_id") for e in events if e["type"] == "response.output_audio.delta"), None)
    pcm = b"".join(chunk for index, chunk in session.audio if index >= mark)
    return {"text": "".join(e.get("delta", "") for e in events if e["type"] == "response.output_text.delta"),
            "status": (done.get("response") or {}).get("status") or done.get("error"), "item": item,
            "first_audio_s": None if first is None else round(first - started, 4)}, pcm


async def call_so_far(url, clips, clock, checkpoints=0):
    """A session that has heard the first three turns and answered each, and those answers."""
    session, rate = await opened(url, clock, checkpoints)
    replies = []
    for pcm in clips[:LONG + 1]:
        await say(session, pcm)
        reply, _ = await respond(session, clock())
        replies.append(reply)
    return session, rate, replies


def heard_part(text):
    return " ".join(text.lstrip().split(" ")[:HEARD_WORDS])


async def arm(name, url, clips, clock, out, trial):
    if name == "seed":
        built, _, replies = await call_so_far(url, clips, clock)
        await built.close()
        started = clock()
        session, rate = await opened(url, clock)
        lines = []
        for line, reply in zip(LINES[:LONG + 1], replies):
            said = heard_part(reply["text"]) if line == LINES[LONG] else reply["text"]
            lines += [f"Caller: {line}", f"You: {said}"]
        await session.send(type="conversation.item.create",
                           item=plain_text("Earlier in this call:\n" + "\n".join(lines)))
        await session.wait_for("conversation.item.added", "error", after=0)
    else:
        session, rate, replies = await call_so_far(url, clips, clock, RESPONSE_CHECKPOINTS if name == "truncate" else 0)
        started = clock()
        if name == "truncate":
            mark = len(session.events)
            await session.send(type="conversation.item.truncate", item_id=replies[LONG]["item"], content_index=0,
                               text=heard_part(replies[LONG]["text"]), expect_last_item_id=last_item(session))
            answer = await session.wait_for("conversation.item.truncated", "error", after=mark)
            if answer["type"] == "error":
                raise RuntimeError(f"truncate refused: {answer}")
    await say(session, clips[LONG + 1])
    reply, pcm = await respond(session, started)
    write_pcm16(out / f"{name}_{trial}.wav", pcm, rate)
    await session.close()
    return {"arm": name, "trial": trial, "long_reply": replies[LONG]["text"], "heard": heard_part(replies[LONG]["text"]),
            "next": reply}


async def cut_only(url, clips, clock):
    session, _, replies = await call_so_far(url, clips, clock, RESPONSE_CHECKPOINTS)
    mark = len(session.events)
    await session.send(type="conversation.item.truncate", item_id=replies[LONG]["item"], content_index=0,
                       text=heard_part(replies[LONG]["text"]), expect_last_item_id=last_item(session))
    await session.wait_for("conversation.item.truncated", "error", after=mark)
    try:
        await say(session, clips[LONG + 1])
        mark = len(session.events)
        await session.send(type="response.create")
        answer = await session.wait_for("response.done", "error", after=mark, timeout=30)
    except (ConnectionError, asyncio.TimeoutError, websockets.ConnectionClosed) as exc:
        answer = {"type": type(exc).__name__, "detail": str(exc)}
    errors = [e for e in session.events[mark:] if e.get("type") == "error"]
    await session.close()
    return {"arm": "cut-only", "answer": answer, "errors": errors}


async def slot_save(port, url, clips, clock):
    session, _, _ = await call_so_far(url, clips, clock)
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/slots", timeout=10) as response:
        slots = json.loads(response.read())
    busy = [s["id"] for s in slots if s.get("is_processing")]
    request = urllib.request.Request(f"http://127.0.0.1:{port}/slots/{busy[0]}?action=save",
                                     data=json.dumps({"filename": "voice.bin"}).encode(),
                                     headers={"Content-Type": "application/json"})
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            result = {"status": response.status, "body": json.loads(response.read())}
    except (urllib.error.URLError, TimeoutError) as exc:
        result = {"error": f"{type(exc).__name__}: {exc}"}
    result["s"] = round(time.perf_counter() - started, 2)
    await session.close()
    return {"arm": "slot-save", "busy_slots": busy, **result}


def server_lines(log):
    found = {"checkpoint": [], "rewind": [], "heard_text": [], "cut_only": []}
    for match in LOG_LINE.finditer(log.read_text(encoding="utf-8", errors="replace")):
        if match.group(2):
            found["checkpoint"].append({"n_past": int(match.group(2)), "mib": float(match.group(3)),
                                        "ms": float(match.group(4))})
        elif match.group(5):
            found["rewind"].append({"from": int(match.group(5)), "to": int(match.group(6)), "ms": float(match.group(7))})
        elif match.group(8):
            found["heard_text"].append({"tokens": int(match.group(8)), "ms": float(match.group(9))})
        elif match.group(10):
            found["cut_only"].append(match.group(10))
    return found


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--bin", type=Path, required=True)
    parser.add_argument("--trials", type=int, default=5)
    parser.add_argument("--arms", default=",".join(ARMS), help=f"a comma-separated subset of {', '.join(ARMS)}")
    parser.add_argument("--clips", type=Path, help="the callers' clips of an earlier run, so two runs hear the same audio")
    args = parser.parse_args()
    arms = args.arms.split(",")
    args.out.mkdir(parents=True, exist_ok=True)
    url, started = f"ws://127.0.0.1:{PORT}/v1/realtime", time.perf_counter()

    def clock():
        return time.perf_counter() - started

    rows = []
    flags = ("-c", "32768", "-kvu", "--slot-save-path", str(args.out / "slots"))
    (args.out / "slots").mkdir(exist_ok=True)
    with Server(PORT, args.out / "llama-server.log", flags, slots=2, binary=args.bin):
        clips = ([at_model_rate(path) for path in sorted(args.clips.glob("turn_*.wav"))] if args.clips
                 else render(args.out / "clips", PORT))
        for trial in range(args.trials):
            for name in (a for a in ARMS[:3] if a in arms):
                rows.append(asyncio.run(arm(name, url, clips, clock, args.out, trial)))
                print(json.dumps({"arm": name, "heard": rows[-1]["heard"], "next": rows[-1]["next"]["text"],
                                  "first_audio_s": rows[-1]["next"]["first_audio_s"]}), flush=True)
        if "slot-save" in arms:
            rows.append(asyncio.run(slot_save(PORT, url, clips, clock)))
            print(json.dumps(rows[-1]), flush=True)
    found = server_lines(args.out / "llama-server.log")
    (args.out / "rows.json").write_text(json.dumps({"rows": rows, "server": found}, indent=1), encoding="utf-8")
    if "cut-only" not in arms:
        return
    os.environ["LLAMA_RT_REWIND_CUT_ONLY"] = "1"
    try:
        with Server(PORT, args.out / "llama-server-cut-only.log", flags, slots=2, binary=args.bin):
            rows.append(asyncio.run(cut_only(url, clips, clock)))
    finally:
        del os.environ["LLAMA_RT_REWIND_CUT_ONLY"]
    found["cut_only"] = server_lines(args.out / "llama-server-cut-only.log")["cut_only"]
    print(json.dumps(rows[-1]), flush=True)
    (args.out / "rows.json").write_text(json.dumps({"rows": rows, "server": found}, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
