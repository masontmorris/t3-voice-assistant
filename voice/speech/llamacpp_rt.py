"""Exercises the llama.cpp `/v1/realtime` WebSocket API with LFM3-Audio-2B GGUF: streaming in and out, live
transcription, tool calls, a function_call_output injection, and response.cancel. Starts and stops its own server.

Usage: python llamacpp_rt.py [runs] [gpu|cpu] [concurrent]
  concurrent streams the same clip to a voice session and a transcription session at once.
Env: VOICE_HOME (default ~/voice); the server binary is $VOICE_HOME/llamacpp/bin/llama-server.exe.
"""

import asyncio
import base64
import json
import os
import statistics
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import numpy as np
import websockets

import lfm

RUNS = int(sys.argv[1]) if len(sys.argv) > 1 else 5
ON_GPU = (sys.argv[2] if len(sys.argv) > 2 else "gpu") == "gpu"
CONCURRENT = len(sys.argv) > 3 and sys.argv[3] == "concurrent"
TAG = ("gpu" if ON_GPU else "cpu") + ("_concurrent" if CONCURRENT else "")
SERVER = lfm.VOICE_HOME / "llamacpp" / "bin" / "llama-server.exe"
GGUF = lfm.VOICE_HOME / "gguf"
PORT = 8093
URL = f"ws://127.0.0.1:{PORT}/v1/realtime"
TOOLS = [
    {"type": "function", "name": "find_file", "description": "Search the user's files and return the best match.",
     "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"type": "function", "name": "send_email", "description": "Send an email.",
     "parameters": {"type": "object", "properties": {"to": {"type": "string"}, "body": {"type": "string"}}, "required": ["to", "body"]}},
]


def pcm16(path):
    return (np.clip(lfm.load_wav(path), -1, 1) * 32767).astype("<i2").tobytes()


class Conn:
    """One realtime session that timestamps every server event relative to `t0`."""

    def __init__(self, ws):
        self.ws, self.events, self.t0 = ws, [], time.perf_counter()

    def now(self):
        return time.perf_counter() - self.t0

    async def send(self, **event):
        await self.ws.send(json.dumps(event))

    async def recv_until(self, *types, timeout=180):
        while True:
            event = json.loads(await asyncio.wait_for(self.ws.recv(), timeout))
            event["_t"] = self.now()
            self.events.append(event)
            if event["type"] == "error":
                print("server error:", event.get("error"), flush=True)
            if event["type"] in types:
                return event

    async def stream(self, pcm, rate=16_000, chunk_ms=100):
        """Sends PCM16 in chunks at real-time pace while draining server events in the background."""
        step = rate * chunk_ms // 1000 * 2
        start = self.now()
        for offset in range(0, len(pcm), step):
            due = start + (offset + step) / 2 / rate
            await self.send(type="input_audio_buffer.append", audio=base64.b64encode(pcm[offset:offset + step]).decode())
            while (wait := due - self.now()) > 0:
                try:
                    event = json.loads(await asyncio.wait_for(self.ws.recv(), wait))
                    event["_t"] = self.now()
                    self.events.append(event)
                except asyncio.TimeoutError:
                    break
        return start, self.now()


def response_summary(conn, since, audio_end, out_rate):
    events = [e for e in conn.events[since:]]
    text = [e for e in events if e["type"] == "response.output_text.delta"]
    audio = [e for e in events if e["type"] == "response.output_audio.delta"]
    done = next((e for e in events if e["type"] == "response.done"), None)
    samples = sum(len(base64.b64decode(e["delta"])) // 2 for e in audio)
    speech_s = samples / out_rate
    first_audio = audio[0]["_t"] if audio else None
    return {
        "first_text_after_audio_end_s": text[0]["_t"] - audio_end if text else None,
        "first_audio_after_audio_end_s": first_audio - audio_end if audio else None,
        "speech_s": speech_s,
        "reply_gen_rtf": (done["_t"] - first_audio) / speech_s if done and audio and speech_s else None,
        "text": "".join(e.get("delta", "") for e in text),
        "status": done.get("response", {}).get("status") if done else None,
        "output": done.get("response", {}).get("output") if done else None,
    }, b"".join(base64.b64decode(e["delta"]) for e in audio)


async def voice_turn(clip, tools=False, keep=False):
    async with websockets.connect(URL, max_size=None) as ws:
        conn = Conn(ws)
        created = await conn.recv_until("session.created")
        out_rate = created["session"]["audio"]["output"]["format"]["rate"]
        session = {"type": "realtime", "instructions": "Respond with interleaved text and audio.", "output_modalities": ["text", "audio"]}
        if tools:
            session.update(instructions="Respond with function calls, and interleaved text and audio.\n\nList of tools:", tools=TOOLS)
            del session["output_modalities"]
        await conn.send(type="session.update", session=session)
        await conn.recv_until("session.updated")
        conn.t0 = time.perf_counter()
        start, audio_end = await conn.stream(pcm16(clip))
        mark = len(conn.events)
        await conn.send(type="input_audio_buffer.commit")
        await conn.send(type="response.create")
        await conn.recv_until("response.done")
        summary, pcm = response_summary(conn, mark, audio_end, out_rate)
        result = {"turn": summary}
        if keep:
            result["conn_events"] = [e["type"] for e in conn.events]
        if tools:
            calls = [o for o in (summary["output"] or []) if o.get("type") == "function_call"]
            result["calls"] = calls
            if calls:
                call = calls[0]
                update = {"type": "task_update", "task_id": "task_1", "status": "completed",
                          "result_summary": "Found Northwind_Sales_Week45.xlsx. Revenue was 48,200 dollars, up 12 percent."}
                await conn.send(type="conversation.item.create", item={"type": "function_call_output", "call_id": call["call_id"], "output": json.dumps(update)})
                mark = len(conn.events)
                t_inject = conn.now()
                await conn.send(type="response.create")
                await conn.recv_until("response.done")
                result["after_injection"], _ = response_summary(conn, mark, t_inject, out_rate)
            await conn.send(type="conversation.item.create", item={"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Tell me a long story about a lighthouse keeper."}]})
            mark = len(conn.events)
            await conn.send(type="response.create")
            await conn.recv_until("response.output_audio.delta")
            t_cancel = conn.now()
            await conn.send(type="response.cancel")
            done = await conn.recv_until("response.done")
            result["cancel"] = {"done_after_cancel_ms": (done["_t"] - t_cancel) * 1000, "status": done.get("response", {}).get("status")}
            await conn.send(type="conversation.item.create", item={"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Stop. What was the revenue number again?"}]})
            mark = len(conn.events)
            t_ask = conn.now()
            await conn.send(type="response.create")
            await conn.recv_until("response.done")
            result["after_cancel"], _ = response_summary(conn, mark, t_ask, out_rate)
        return result, pcm, out_rate


async def live_transcription(clip):
    async with websockets.connect(URL + "?intent=transcription", max_size=None) as ws:
        conn = Conn(ws)
        await conn.recv_until("session.created")
        conn.t0 = time.perf_counter()
        start, audio_end = await conn.stream(pcm16(clip))
        await conn.send(type="input_audio_buffer.commit", final=True)
        completed = await conn.recv_until("conversation.item.input_audio_transcription.completed")
        deltas = [e for e in conn.events if e["type"] == "conversation.item.input_audio_transcription.delta" and e.get("delta", "").strip()]
        gaps = [b["_t"] - a["_t"] for a, b in zip(deltas, deltas[1:])]
        return {
            "first_partial_s": deltas[0]["_t"] if deltas else None,
            "update_interval_median_s": statistics.median(gaps) if gaps else None,
            "final_after_audio_end_s": completed["_t"] - audio_end,
            "transcript": completed.get("transcript"),
            "deltas": [(round(e["_t"], 3), e["delta"]) for e in deltas],
        }


def summarize(rows):
    out = {}
    for key, value in rows[0].items():
        if isinstance(value, float):
            values = [r[key] for r in rows if r.get(key) is not None]
            if values:
                out[key] = {"median": statistics.median(values), "min": min(values), "max": max(values), "n": len(values)}
    return out


async def main():
    results = {"server": str(SERVER), "gpu": ON_GPU, "model": "LFM3-Audio-2B-Q4_K_M.gguf + mmproj Q8_0 @39670cb5"}
    probe, pcm, rate = await voice_turn(lfm.AUDIO_DIR / "two_request.wav", tools=True, keep=True)
    results["probe_tools"] = probe
    print(json.dumps({k: v for k, v in probe.items() if k != "conn_events"}, indent=1)[:4000], flush=True)
    results["live_transcription_probe"] = await live_transcription(lfm.AUDIO_DIR / "correction.wav")
    print(results["live_transcription_probe"], flush=True)
    await voice_turn(lfm.AUDIO_DIR / "two_request.wav")
    await live_transcription(lfm.AUDIO_DIR / "long.wav")
    turns, asr = [], []
    for index in range(RUNS):
        if CONCURRENT:
            (turn, pcm, rate), transcription = await asyncio.gather(
                voice_turn(lfm.AUDIO_DIR / "two_request.wav"), live_transcription(lfm.AUDIO_DIR / "two_request.wav"))
        else:
            turn, pcm, rate = await voice_turn(lfm.AUDIO_DIR / "two_request.wav")
            transcription = await live_transcription(lfm.AUDIO_DIR / "long.wav")
        turns.append(turn["turn"])
        asr.append(transcription)
        print(index, {k: v for k, v in turns[-1].items() if k not in ("output", "text")}, {k: v for k, v in asr[-1].items() if k not in ("deltas", "transcript")}, flush=True)
    results["voice_turn"] = {"rows": turns, "summary": summarize(turns)}
    results["live_transcription"] = {"rows": asr, "summary": summarize(asr)}
    out = lfm.EVIDENCE_DIR / f"llamacpp_reply_{TAG}.wav"
    import soundfile as sf
    sf.write(out, np.frombuffer(pcm, dtype="<i2"), rate)
    lfm.write_json(f"llamacpp_rt_{TAG}.json", results)


def wait_healthy(proc, timeout=180):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"server exited with {proc.returncode}")
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=2) as response:
                if response.status == 200:
                    return
        except OSError:
            pass
        time.sleep(0.5)
    raise RuntimeError("server did not become healthy")


if __name__ == "__main__":
    log = open(lfm.EVIDENCE_DIR / f"llamacpp_server_{TAG}.log", "w")
    command = [str(SERVER), "-m", str(GGUF / "LFM3-Audio-2B-Q4_K_M.gguf"), "-mm", str(GGUF / "mmproj-LFM3-Audio-2B-Q8_0.gguf"),
               "--host", "127.0.0.1", "--port", str(PORT), "--temp", "0", "-np", "2", "-ngl", "99" if ON_GPU else "0"]
    env = dict(os.environ, CUDA_VISIBLE_DEVICES="" if not ON_GPU else os.environ.get("CUDA_VISIBLE_DEVICES", "0"))
    t = time.perf_counter()
    server = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, env=env)
    try:
        wait_healthy(server)
        print(f"server pid {server.pid} healthy after {time.perf_counter() - t:.1f} s", flush=True)
        asyncio.run(main())
    finally:
        server.terminate()
        server.wait(timeout=30)
