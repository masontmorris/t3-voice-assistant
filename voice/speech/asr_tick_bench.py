"""Measures the live transcript's tick on llama.cpp `/v1/realtime`: when text arrives, how fast, how accurate.

One configuration per run. `--stagger K` opens K transcription sessions on the same audio; session i hears
i * 0.96 / K s of silence first, so its 12-row blocks end at different moments than the others'. `--chunk-rows N`
starts the server with LLAMA_ASR_CHUNK_ROWS=N (needs a patched build). `--prefix-step S` instead transcribes
prefixes of each clip, the way a mid-block peek would. `--with-voice` streams every clip to a voice session as well
and measures its first reply audio, to price the extra sessions.

Output: one JSON file with every event time, so the tables are recomputed from it, not from the console.
"""

import argparse
import asyncio
import base64
import json
import os
import re
import statistics
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

import numpy as np
import soundfile as sf
import websockets

VOICE_HOME = Path(os.environ.get("VOICE_HOME", Path.home() / "voice"))
RATE = 16_000
ROW_S = 0.08
BLOCK_ROWS = 12
DELTA = "conversation.item.input_audio_transcription.delta"
DONE = "conversation.item.input_audio_transcription.completed"
BURST_GAP_S = 0.2


def words(text):
    return re.findall(r"[a-z0-9']+", text.lower().replace("-", " "))


def edit_distance(ref, hyp):
    prev = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        cur = [i]
        for j, h in enumerate(hyp, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (r != h)))
        prev = cur
    return prev[-1]


def wer(ref_text, hyp_text):
    ref = words(ref_text)
    return edit_distance(ref, words(hyp_text)) / max(1, len(ref))


def common_prefix(a, b):
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


def bursts(deltas):
    """Groups of deltas less than BURST_GAP_S apart: one block's text arrives as one burst."""
    out = []
    for t, text in deltas:
        if out and t - out[-1][-1][0] < BURST_GAP_S:
            out[-1].append((t, text))
        else:
            out.append([(t, text)])
    return out


def load_clip(path):
    samples, rate = sf.read(str(path), dtype="float32", always_2d=True)
    mono = samples.mean(axis=1)
    if rate != RATE:
        mono = np.interp(np.arange(int(len(mono) * RATE / rate)) * rate / RATE, np.arange(len(mono)), mono).astype("float32")
    return (np.clip(mono, -1, 1) * 32767).astype("<i2").tobytes()


class GpuSampler(threading.Thread):
    """Samples nvidia-smi utilization and memory while a run is going."""

    def __init__(self, period=0.5):
        super().__init__(daemon=True)
        self.period, self.samples, self.stop = period, [], threading.Event()

    def run(self):
        while not self.stop.is_set():
            try:
                out = subprocess.run(["nvidia-smi", "--query-gpu=utilization.gpu,memory.used", "--format=csv,noheader,nounits"],
                                     capture_output=True, text=True, timeout=5).stdout.split(",")
                self.samples.append((time.perf_counter(), float(out[0]), float(out[1])))
            except (OSError, ValueError, subprocess.SubprocessError):
                pass
            self.stop.wait(self.period)


class Server:
    def __init__(self, bin_dir, port, slots, chunk_rows, log):
        self.bin_dir, self.port, self.slots, self.chunk_rows, self.log, self.proc = Path(bin_dir), port, slots, chunk_rows, log, None

    def __enter__(self):
        gguf = VOICE_HOME / "gguf"
        command = [str(self.bin_dir / "llama-server.exe"), "-m", str(gguf / "LFM3-Audio-2B-Q4_K_M.gguf"),
                   "-mm", str(gguf / "mmproj-LFM3-Audio-2B-Q8_0.gguf"), "--host", "127.0.0.1", "--port", str(self.port),
                   "--temp", "0", "-np", str(self.slots), "-c", str(4096 * self.slots), "-ngl", "99"]
        env = dict(os.environ)
        if self.chunk_rows:
            env["LLAMA_ASR_CHUNK_ROWS"] = str(self.chunk_rows)
        if os.environ.get("ASR_ENC_ROWS"):
            env["LLAMA_ASR_ENC_ROWS"] = os.environ["ASR_ENC_ROWS"]
        self.out = open(self.log, "w")
        self.proc = subprocess.Popen(command, stdout=self.out, stderr=subprocess.STDOUT, env=env)
        deadline = time.time() + 180
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"server exited {self.proc.returncode}, see {self.log}")
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/health", timeout=2) as r:
                    if r.status == 200:
                        return self
            except OSError:
                time.sleep(0.5)
        raise RuntimeError("server not healthy")

    def __exit__(self, *exc):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.out.close()


async def asr_session(url, pcm, silence_s, paced=True, chunk_ms=40):
    """Streams pcm (after silence_s of silence) to one transcription session. Times are seconds from the start of
    the clip's audio, so a staggered session's deltas line up with the others'."""
    async with websockets.connect(url + "?intent=transcription", max_size=None) as ws:
        json.loads(await ws.recv())
        deltas, done, text_box = [], {}, {}

        async def reader():
            async for raw in ws:
                event = json.loads(raw)
                t = time.perf_counter() - text_box["t0"]
                if event["type"] == DELTA and event.get("delta"):
                    deltas.append((t, event["delta"]))
                elif event["type"] == DONE:
                    done.update(t=t, text=event.get("transcript", ""))
                    return

        text_box["t0"] = time.perf_counter()
        task = asyncio.create_task(reader())
        silence = bytes(int(silence_s * RATE) * 2)
        step = RATE * chunk_ms // 1000 * 2
        for offset in range(0, len(silence), step * 25):
            await ws.send(json.dumps({"type": "input_audio_buffer.append",
                                      "audio": base64.b64encode(silence[offset:offset + step * 25]).decode()}))
        for offset in range(0, len(pcm), step):
            await ws.send(json.dumps({"type": "input_audio_buffer.append",
                                      "audio": base64.b64encode(pcm[offset:offset + step]).decode()}))
            if paced:
                await asyncio.sleep(max(0.0, text_box["t0"] + (offset + step) / 2 / RATE - time.perf_counter()))
        audio_end = time.perf_counter() - text_box["t0"]
        await ws.send(json.dumps({"type": "input_audio_buffer.commit", "final": True}))
        await asyncio.wait_for(task, 60)
        return {"silence_s": silence_s, "deltas": deltas, "audio_end": audio_end, "final_t": done.get("t"),
                "final_text": done.get("text", "")}


async def voice_turn(url, pcm):
    """Streams the clip to a realtime voice session in real time, then asks for a spoken reply."""
    async with websockets.connect(url, max_size=None) as ws:
        created = json.loads(await ws.recv())
        out_rate = created["session"]["audio"]["output"]["format"]["rate"]
        await ws.send(json.dumps({"type": "session.update", "session": {"type": "realtime", "output_modalities": ["text", "audio"],
                                                                         "instructions": "Respond with interleaved text and audio."}}))
        while json.loads(await ws.recv())["type"] != "session.updated":
            pass
        t0 = time.perf_counter()
        step = RATE * 100 // 1000 * 2
        for offset in range(0, len(pcm), step):
            await ws.send(json.dumps({"type": "input_audio_buffer.append", "audio": base64.b64encode(pcm[offset:offset + step]).decode()}))
            await asyncio.sleep(max(0.0, t0 + (offset + step) / 2 / RATE - time.perf_counter()))
        audio_end = time.perf_counter() - t0
        await ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
        await ws.send(json.dumps({"type": "response.create"}))
        first_audio, samples, done_t = None, 0, None
        while True:
            event = json.loads(await asyncio.wait_for(ws.recv(), 120))
            t = time.perf_counter() - t0
            if event["type"] == "response.output_audio.delta":
                first_audio = first_audio or t
                samples += len(base64.b64decode(event["delta"])) // 2
            elif event["type"] == "response.done":
                done_t = t
                break
        speech_s = samples / out_rate
        return {"first_audio_after_end_s": None if first_audio is None else first_audio - audio_end,
                "reply_rtf": (done_t - first_audio) / speech_s if first_audio and speech_s else None, "speech_s": speech_s}


def clips(selection):
    real = VOICE_HOME / "audio" / "real"
    out = {}
    if selection in ("real", "all"):
        script = {}
        for line in (real / "script.txt").read_text(encoding="utf-8").splitlines():
            if line and not line.startswith("#") and "\t" in line:
                name, ref = line.split("\t")[:2]
                script[name] = ref
        for path in sorted(real.glob("*.wav")):
            out[f"real/{path.stem}"] = (path, script.get(path.name))
    if selection in ("turns", "all"):
        for path in sorted((VOICE_HOME / "evidence" / "live").glob("2026*/turn_*_input.wav")):
            out[f"turns/{path.parent.name}/{path.stem}"] = (path, None)
    return out


async def run_stagger(url, args, selected):
    results = {}
    for name, (path, ref) in selected.items():
        pcm = load_clip(path)
        offsets = [i * BLOCK_ROWS * ROW_S / args.stagger for i in range(args.stagger)]
        jobs = [asr_session(url, pcm, off) for off in offsets]
        if args.with_voice:
            jobs.append(voice_turn(url, pcm))
        got = await asyncio.gather(*jobs)
        results[name] = {"ref": ref, "audio_s": len(pcm) / 2 / RATE, "sessions": got[:args.stagger],
                         "voice": got[args.stagger] if args.with_voice else None}
        print(name, ascii(got[0]["final_text"][:60]), flush=True)
    return results


async def run_prefix(url, args, selected):
    """Transcribes prefixes of each clip, one fresh session per cut, without real-time pacing."""
    results = {}
    for name, (path, ref) in selected.items():
        pcm = load_clip(path)
        total = len(pcm) / 2 / RATE
        cuts = []
        cut = args.prefix_step
        while cut < total:
            got = await asr_session(url, pcm[:int(cut * RATE) * 2], 0.0, paced=False)
            cuts.append({"cut_s": cut, "text": got["final_text"]})
            cut += args.prefix_step
        full = await asr_session(url, pcm, 0.0, paced=False)
        results[name] = {"ref": ref, "audio_s": total, "cuts": cuts, "full": full["final_text"]}
        print(name, len(cuts), "cuts", flush=True)
    return results


async def main(args):
    selected = clips(args.clips)
    if args.limit:
        selected = dict(list(selected.items())[:args.limit])
    slots = args.stagger + (1 if args.with_voice else 0)
    sampler = GpuSampler()
    with Server(args.bin, args.port, max(slots, 1), args.chunk_rows, args.out.with_suffix(".server.log")):
        url = f"ws://127.0.0.1:{args.port}/v1/realtime"
        warm = next(iter(selected.values()))[0]
        await asr_session(url, load_clip(warm)[:RATE * 2 * 3], 0.0, paced=False)
        sampler.start()
        started = time.perf_counter()
        body = await (run_prefix if args.prefix_step else run_stagger)(url, args, selected)
        wall = time.perf_counter() - started
        sampler.stop.set()
    util = [s[1] for s in sampler.samples]
    record = {"config": {k: str(v) for k, v in vars(args).items()}, "results": body, "wall_s": wall,
              "gpu_util_mean": statistics.mean(util) if util else None, "gpu_mem_max_mib": max((s[2] for s in sampler.samples), default=None)}
    args.out.write_text(json.dumps(record), encoding="utf-8")
    print("wrote", args.out, "gpu util mean", record["gpu_util_mean"], flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bin", type=Path, default=VOICE_HOME / "llamacpp" / "bin", help="directory holding llama-server.exe")
    parser.add_argument("--port", type=int, default=8197)
    parser.add_argument("--chunk-rows", type=int, default=0)
    parser.add_argument("--stagger", type=int, default=1)
    parser.add_argument("--prefix-step", type=float, default=0.0)
    parser.add_argument("--with-voice", action="store_true")
    parser.add_argument("--clips", choices=("real", "turns", "all"), default="all")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--out", type=Path, required=True)
    asyncio.run(main(parser.parse_args()))
