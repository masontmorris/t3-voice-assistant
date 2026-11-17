"""LFM3 live ASR over the probe's clips, through llama-server's realtime transcription socket.

Phase 1 sends each clip unpaced and keeps the final transcript (text baseline; no timing claim).
Phase 2 streams the real turns at real-time pace and records when the first partial arrives and how long after audio
end the final arrives, against the voice onset from the energy VAD (timing claim; run it under quiet_run).
Run under the GPU lease. Writes transcripts_asr.json and asr_timing.json under the research dir.
"""
import asyncio
import base64
import json
import os
import subprocess
import sys
import time
import urllib.request

import numpy as np
import websockets

from voice.research.audio_probe import liquid as L, vad

VOICE = L.HOME
SERVER = VOICE / "llamacpp" / "bin" / "llama-server.exe"
GGUF = VOICE / "gguf"
PORT = 18193
URL = f"ws://127.0.0.1:{PORT}/v1/realtime?intent=transcription"
CLIPS = L.OUT / "clips"
TTS = L.OUT / "tts"


def pcm(path):
    x, r = L.read_wav(path)
    return L.resample16k(x, r).astype("<i2").tobytes()


async def transcribe(path, paced):
    data = pcm(path)
    step = 16000 * 100 // 1000 * 2
    async with websockets.connect(URL, max_size=None) as ws:
        t0 = time.perf_counter()
        created = json.loads(await asyncio.wait_for(ws.recv(), 30))
        events = []

        async def drain():
            try:
                async for m in ws:
                    e = json.loads(m)
                    e["_t"] = time.perf_counter() - t0
                    events.append(e)
                    if e["type"] == "conversation.item.input_audio_transcription.completed":
                        return
            except websockets.ConnectionClosed:
                return

        task = asyncio.create_task(drain())
        start = time.perf_counter() - t0
        for off in range(0, len(data), step):
            await ws.send(json.dumps({"type": "input_audio_buffer.append",
                                      "audio": base64.b64encode(data[off:off + step]).decode()}))
            if paced:
                due = start + (off + step) / 2 / 16000
                await asyncio.sleep(max(0, due - (time.perf_counter() - t0)))
        audio_end = time.perf_counter() - t0
        await ws.send(json.dumps({"type": "input_audio_buffer.commit", "final": True}))
        await asyncio.wait_for(task, 120)
    done = next(e for e in events if e["type"] == "conversation.item.input_audio_transcription.completed")
    deltas = [(round(e["_t"] - start, 3), e["delta"]) for e in events
              if e["type"] == "conversation.item.input_audio_transcription.delta" and e.get("delta", "").strip()]
    return {"text": done.get("transcript"), "deltas": deltas, "audio_end_s": audio_end - start,
            "final_after_audio_end_s": done["_t"] - audio_end}


def wait_healthy(proc, timeout=180):
    end = time.time() + timeout
    while time.time() < end:
        if proc.poll() is not None:
            raise RuntimeError(f"server exited {proc.returncode}")
        try:
            if urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=2).status == 200:
                return
        except OSError:
            pass
        time.sleep(0.5)
    raise RuntimeError("not healthy")


async def main():
    manifest = [json.loads(l) for l in (CLIPS / "manifest.jsonl").read_text().splitlines()]
    files = [CLIPS / r["file"] for r in manifest if r["set"] in ("eot", "bc")]
    tts = [TTS / json.loads(l)["file"] for l in (TTS / "manifest.jsonl").read_text().splitlines()]
    out = {}
    print("phase 1: unpaced ASR on", len(files) + len(tts), "clips", flush=True)
    for p in files + tts:
        out[p.name] = (await transcribe(p, False))["text"]
    (L.OUT / "transcripts_asr.json").write_text(json.dumps(out, indent=0))
    print("phase 2: paced timing on the real turns", flush=True)
    timing = []
    for r in manifest:
        if r["set"] != "kind":
            continue
        x, rate = L.read_wav(CLIPS / r["file"])
        spans, _ = vad.speech_and_pauses(x)
        res = await transcribe(CLIPS / r["file"], True)
        timing.append({"file": r["file"], "audio_s": len(x) / rate, "onset_s": spans[0][0], "speech_end_s": spans[-1][1],
                       "first_partial_s": res["deltas"][0][0] if res["deltas"] else None,
                       "final_after_audio_end_s": res["final_after_audio_end_s"], "deltas": res["deltas"],
                       "text": res["text"]})
    (L.OUT / "asr_timing.json").write_text(json.dumps(timing, indent=0))
    print("done", flush=True)


if __name__ == "__main__":
    log = open(L.OUT / "asr_server.log", "w")
    cmd = [str(SERVER), "-m", str(GGUF / "LFM3-Audio-2B-Q4_K_M.gguf"), "-mm", str(GGUF / "mmproj-LFM3-Audio-2B-Q8_0.gguf"),
           "--host", "127.0.0.1", "--port", str(PORT), "--temp", "0", "-np", "1", "-ngl", "99"]
    proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT)
    try:
        wait_healthy(proc)
        asyncio.run(main())
    finally:
        proc.terminate()
        proc.wait(timeout=30)
