"""Bench LFM3-1.7B as the voice's T3 renderer beside LFM3-Audio and d1-2B. GPU: run inside the lease and quiet_run.

    python -m voice.research.local_renderer.bench --clips DIR --eval-dir DIR [--rounds 2]

One run loads LFM3-Audio at the live call's settings and d1-2B at the local decider's, and records total GPU memory
after each load. It then times LFM3-Audio's first reply audio on every synthetic clip in --clips three ways:
- alone: the 1.7B not loaded, once before it starts and once after it stops, so drift shows;
- idle: the 1.7B loaded and quiet;
- rendering: the 1.7B answering render and summary requests back to back for the whole reply, the lane's worst case.
Idle and rendering alternate clip by clip, flipping order each round. Between the alone and idle blocks it runs the
unit 11 summary and act evals through the 1.7B (`summary_eval --renderer local`) into --eval-dir, with the hosted
decider checking each line, and their per-call latency is the 1.7B's render latency with LFM3 and d1 loaded but quiet.

Results go to --eval-dir/bench.json. No request goes to OpenRouter: the cloud client's transport raises if called.
"""

import argparse
import asyncio
import json
import statistics
import threading
import time
from itertools import cycle
from pathlib import Path

import voice.t3.pick.model as cloud
from voice.local_decider.client import VOICE_HOME, LlamaServer, d1_server
from voice.local_decider.gpu import gpu_used_mib
from voice.research.local_decider.bench import VramPeak
from voice.t3.acts import Act
from voice.t3.eval import summary_eval
from voice.t3.speech import LocalText, render_messages, summary_messages

MODEL = VOICE_HOME / "gguf" / "LFM3-1.7B-Q4_K_M.gguf"
# One slot: the lane keeps one request in flight. 8192 tokens holds the longest render or summary prompt with room.
RENDERER_FLAGS = ("-ngl", "99", "-np", "1", "-c", "8192", "-fa", "on")
LFM_ARGS = ("-c", "32768", "-kvu")  # voice/live/server.py main_async
LFM_SLOTS = 3  # voice/live/server.py slots() at the default ASR stagger
INSTRUCTIONS = "You are a friendly voice assistant. Answer in two or three sentences."


def no_openrouter(*_args, **_kwargs):
    raise RuntimeError("this bench makes no OpenRouter call")


def load_messages(eval_dir: Path) -> list:
    acts = json.loads((eval_dir / "acts.json").read_text(encoding="utf-8"))
    states = json.loads((eval_dir / "states.json").read_text(encoding="utf-8"))
    labels = {label["thread_id"]: label for label in
              json.loads((eval_dir / "labels.json").read_text(encoding="utf-8"))}
    out = [render_messages(tuple(Act.from_json(a) for a in item["acts"]), item.get("turns", ())) for item in acts]
    out += [summary_messages(summary_eval.thread_state(raw), labels[raw["thread_id"]]["ask"]) for raw in states]
    return out


async def trial(lfm_port, clip, load, renderer, messages):
    """One spoken reply to `clip`. With `load`, the 1.7B answers `messages` back to back until the reply is done."""
    from voice.prototype.realtime import Session, read_pcm16
    pcm, rate, _ = read_pcm16(clip)
    t0 = time.perf_counter()
    voice = await Session.open(f"ws://127.0.0.1:{lfm_port}/v1/realtime", "voice", lambda: time.perf_counter() - t0)
    created = await voice.wait_for("session.created")
    out_rate = created["session"]["audio"]["output"]["format"]["rate"]
    session = {"type": "realtime", "output_modalities": ["text", "audio"], "instructions": INSTRUCTIONS}
    if rate != 16_000:
        session["audio"] = {"input": {"format": {"type": "audio/pcm", "rate": rate}}}
    await voice.send(type="session.update", session=session)
    await voice.wait_for("session.updated")
    await voice.stream(pcm, rate)
    await voice.send(type="input_audio_buffer.commit")
    mark = len(voice.events)
    speaking = threading.Event()
    render_ms = []

    def hammer():
        while not speaking.is_set():
            render_ms.append(renderer.complete(next(messages))["ms"])

    asked = voice.clock()
    await voice.send(type="response.create")
    worker = asyncio.get_running_loop().run_in_executor(None, hammer) if load else None
    with VramPeak() as vram:
        done = await voice.wait_for("response.done", "error", after=mark)
    speaking.set()
    if worker:
        await worker
    audio = [e for e in voice.events[mark:] if e["type"] == "response.output_audio.delta"]
    await voice.close()
    return {"first_audio_s": audio[0]["_t"] - asked if audio else None, "reply_s": done["_t"] - asked,
            "reply_audio_s": sum(e["delta"]["bytes"] for e in audio) / 2 / out_rate, "status": done["type"],
            "render_ms": render_ms, "peak_mib": vram.peak}


async def block(rows, condition, lfm_port, clips, renderer=None, messages=None):
    for clip in clips:
        row = {"condition": condition, "clip": clip.name,
               **await trial(lfm_port, clip, condition == "rendering", renderer, messages)}
        rows.append(row)
        print(json.dumps({k: v for k, v in row.items() if k != "render_ms"} | {"renders": len(row["render_ms"])}),
              flush=True)


def summary(values):
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    p95 = values[min(len(values) - 1, round(0.95 * (len(values) - 1)))]
    return {"n": len(values), "median": statistics.median(values), "p95": p95, "min": values[0], "max": values[-1]}


async def run(args):
    cloud._post = no_openrouter
    from voice.prototype.realtime import Server
    clips = sorted(args.clips.glob("*.wav"))
    messages = cycle(load_messages(args.eval_dir))
    logs = args.eval_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    vram = {"idle": gpu_used_mib()}
    rows = []
    out = {"clips": [c.name for c in clips], "renderer_flags": RENDERER_FLAGS, "lfm_args": LFM_ARGS, "vram_mib": vram}
    with Server(args.lfm_port, logs / "lfm3-audio.log", LFM_ARGS, LFM_SLOTS):
        vram["lfm3_audio"] = gpu_used_mib()
        with d1_server(port=args.d1_port, log_path=logs / "d1.log"):
            vram["lfm3_audio_d1"] = gpu_used_mib()
            await block(rows, "alone", args.lfm_port, clips)
            with LlamaServer(["-m", str(MODEL), *RENDERER_FLAGS], args.renderer_port, logs / "renderer.log") as server:
                out["renderer_startup_s"] = server.startup_s
                vram["all_three"] = gpu_used_mib()
                renderer = LocalText(f"http://127.0.0.1:{args.renderer_port}")
                for _ in range(2):
                    renderer.complete(next(messages))
                with VramPeak() as peak:
                    for command in ("run", "acts"):
                        summary_eval.main([command, "--dir", str(args.eval_dir), "--renderer", "local"])
                vram["all_three_eval_peak"] = peak.peak
                for r in range(args.rounds):
                    for i, clip in enumerate(clips):
                        order = ("idle", "rendering") if (i + r) % 2 == 0 else ("rendering", "idle")
                        for condition in order:
                            await block(rows, condition, args.lfm_port, [clip], renderer, messages)
                vram["all_three_after"] = gpu_used_mib()
            await block(rows, "alone_after", args.lfm_port, clips)
    ledgers = [json.loads((args.eval_dir / f"ledger-{c}.json").read_text(encoding="utf-8")) for c in ("run", "acts")]
    out["first_audio_s"] = {c: summary(r["first_audio_s"] for r in rows if r["condition"] == c)
                            for c in ("alone", "idle", "rendering", "alone_after")}
    out["render_ms_quiet"] = summary(e["ms"] for ledger in ledgers for e in ledger)
    out["render_ms_beside_reply"] = summary(ms for r in rows for ms in r["render_ms"])
    out["rows"] = rows
    (args.eval_dir / "bench.json").write_text(json.dumps(out, indent=1) + "\n", encoding="utf-8")
    print(json.dumps({k: out[k] for k in ("vram_mib", "first_audio_s", "render_ms_quiet", "render_ms_beside_reply")},
                     indent=1), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--clips", type=Path, required=True)
    parser.add_argument("--eval-dir", type=Path, required=True)
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--lfm-port", type=int, default=8095)
    parser.add_argument("--d1-port", type=int, default=8096)
    parser.add_argument("--renderer-port", type=int, default=8098)
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
