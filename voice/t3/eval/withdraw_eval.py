"""Score what the call voice says after a which-thread question is withdrawn, with the private note as the variable.
GPU: run inside the lease.

    python -m voice.t3.eval.withdraw_eval --out DIR [--repeats 8] [--arms ...]

Each trial opens a fresh voice session with the live persona, plays a thread question as audio, relays a made-up
which-thread line as a turn does, plays "Never mind." and then three more turns about other things, taking each free
reply as the live turn does. An arm decides which private note rides with which turn. A reply offers the old choices when
the hosted d1.2 says it asks the person to choose between threads or lists threads as options: the same question the
whole-call scenario asks (voice/live/scenarios.py).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

from voice.live.scenario_score import Judge
from voice.prototype import run
from voice.prototype.realtime import Server, Session, read_pcm16
from voice.t3.acts import Kind, act
from voice.t3.eval.relay_eval import checked

LFM_ARGS = ("-c", "32768", "-kvu")
LFM_SLOTS = 3
THREADS = ["Add a Volume Slider", "Add volume slider to the settings page"]
WHICH = act(Kind.ASK_WHICH, of="thread", candidates=THREADS)
LINE = f"Which thread do you mean, {THREADS[0]} or {THREADS[1]}?"
TURNS = ("How is the volume slider thread going?", "Never mind.",
         "Can you tell me something interesting about octopuses?", "Can we start over?",
         "Okay. Hello again, how are you?")
QUESTION = "Does the reply ask the person to choose between threads, or list threads as options to choose from?"
CONTEXT = f"The threads are {THREADS[0]} and {THREADS[1]}."

SHIPPED = (run.NOTE_OPEN + "they did not answer your last question and have moved on. Answer what they said in this "
           "turn, and do not repeat or ask that question again.]")
TOOK_BACK = run.WITHDRAWN_NOTE
CLOSED = (run.NOTE_OPEN + "your earlier question about their coding threads is closed. Do not bring up its choices "
          "again. Answer what they said in this turn.]")
# arm: the note for the withdrawing turn, the note for each later turn, and whether the note comes before the audio
ARMS = {
    "none": (None, None, False),
    "shipped": (SHIPPED, None, False),
    "took-back": (TOOK_BACK, None, False),
    "took-back-before-audio": (TOOK_BACK, None, True),
    "took-back-and-closed-after": (TOOK_BACK, CLOSED, False),
}


def render(url: str, out: Path) -> list[Path]:
    turns = out / "turns.txt"
    turns.write_text("\n".join(TURNS) + "\n", encoding="utf-8")
    subprocess.run([sys.executable, "-m", "voice.live.script", "--turns", str(turns), "--out", str(out / "clips"),
                    "--server", url.replace("ws://", "http://").removesuffix("/v1/realtime")], check=True,
                   stdout=subprocess.DEVNULL)
    return sorted((out / "clips").glob("turn_*.wav"))


async def trial(url: str, clips: list[Path], arm: tuple, judge: Judge) -> list[dict]:
    first, later, before = arm
    t0 = time.perf_counter()
    clock = lambda: time.perf_counter() - t0
    voice = await Session.open(url, "voice", clock)
    out = []
    try:
        created = await voice.wait_for("session.created")
        rate = created["session"]["audio"]["output"]["format"]["rate"]
        await run.update_session(voice, {"type": "realtime", "output_modalities": ["text", "audio"],
                                         "instructions": run.NOTE_PERSONA + run.FORMAT_LINES["c"]})

        async def person(clip: Path, note: str | None):
            if note is not None and before:
                await run.create_item(voice, run.plain_text(note))
            pcm, in_rate, _ = read_pcm16(clip)
            await voice.stream(pcm, in_rate)
            await voice.send(type="input_audio_buffer.commit")
            if note is not None and not before:
                await run.create_item(voice, run.plain_text(note))

        await person(clips[0], None)
        relayed, _ = await run.relay_draft(voice, rate, clock(), checked(WHICH, LINE))
        out.append({"turn": TURNS[0], "said": relayed["text"], "relay": True})
        for i, clip in enumerate(clips[1:], 1):
            await person(clip, first if i == 1 else later)
            reply, _ = await run.respond(voice, rate, clock())
            offers = judge.yes(QUESTION, reply["text"], CONTEXT)
            out.append({"turn": TURNS[i], "said": reply["text"], "offers": offers})
        return out
    finally:
        await voice.close()


async def main_async(args) -> int:
    args.out.mkdir(parents=True, exist_ok=True)
    judge, rows = Judge(), []
    with Server(args.port, args.out / "lfm3-audio.log", LFM_ARGS, LFM_SLOTS):
        url = f"ws://127.0.0.1:{args.port}/v1/realtime"
        clips = render(url, args.out)
        for name in args.arms:
            for n in range(args.repeats):
                turns = await trial(url, clips, ARMS[name], judge)
                rows.append({"arm": name, "trial": n, "turns": turns})
                print(json.dumps({"arm": name, "offers": [t["offers"] for t in turns if "offers" in t]}), flush=True)
    summary = {}
    for name in args.arms:
        mine = [r for r in rows if r["arm"] == name]
        summary[name] = {"trials": len(mine), "offered_on_the_withdrawing_turn": sum(r["turns"][1]["offers"] for r in mine),
                         "offered_on_any_later_turn": sum(any(t["offers"] for t in r["turns"][2:]) for r in mine),
                         "offered_on_any_turn": sum(any(t.get("offers") for t in r["turns"]) for r in mine)}
    (args.out / "withdraw.json").write_text(json.dumps({"summary": summary, "rows": rows}, indent=1), encoding="utf-8")
    print(json.dumps(summary, indent=1))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=8)
    parser.add_argument("--port", type=int, default=8095)
    parser.add_argument("--arms", nargs="+", default=list(ARMS), choices=list(ARMS))
    return asyncio.run(main_async(parser.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
