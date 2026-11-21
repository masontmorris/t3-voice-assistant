"""Score what the call voice says after a thread question is withdrawn, with the private note as the variable. GPU: run
inside the lease.

    python -m voice.t3.eval.withdraw_eval --out DIR [--repeats 8] [--arms ...] [--shapes ...]

Each trial opens a fresh voice session with the live persona, plays a thread question as audio, relays a made-up line as
a turn does, plays "Never mind." and then three more turns about other things, taking each free reply as the live turn
does. An arm decides which private note rides with which turn, and a shape is what came before the withdrawal: a
which-thread question, a "do you mean" question, or a status line and then the which-thread question.

Two scores, per reply. A reply offers the old choices when the hosted d1.2 says it asks the person to choose between
threads or lists threads as options: the same question the whole-call scenario asks (voice/live/scenarios.py). A reply
reads the note out when it shares a run of `NGRAM` words with the note, which is this eval's own measure and decides
nothing in the product, or when the hosted d1.2 says it speaks words meant for the assistant and not for the person.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
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
CONFIRM = act(Kind.CONFIRM_THREAD, thread=THREADS[0])
CONFIRM_LINE = f"Do you mean the thread called {THREADS[0]}?"
STATUS = act(Kind.THREAD_STATUS, thread="Polish the Main Menu", status="working")
STATUS_LINE = "The Polish the Main Menu thread is working."
TURNS = ("How is the volume slider thread going?", "Never mind.",
         "Can you tell me something interesting about octopuses?", "Can we start over?",
         "Okay. Hello again, how are you?", "How is the main menu thread going?")
QUESTION = "Does the reply ask the person to choose between threads, or list threads as options to choose from?"
CONTEXT = f"The threads are {THREADS[0]} and {THREADS[1]}."
READ_OUT = ("Does the reply speak instructions or notes meant for the assistant itself, such as being told what to do "
            "or not to do, instead of words said to a person?")
NGRAM = 4

SHIPPED = (run.NOTE_OPEN + "they did not answer your last question and have moved on. Answer what they said in this "
           "turn, and do not repeat or ask that question again.]")
TOOK_BACK = (run.NOTE_OPEN + "they took your last question back: they do not want any of the choices it offered, and "
             "they are not choosing. Do not list, offer or ask about those choices again. Answer what they said in "
             "this turn.]")
CLOSED = (run.NOTE_OPEN + "your earlier question about their coding threads is closed. Do not bring up its choices "
          "again. Answer what they said in this turn.]")
NO_REPEAT = run.WITHDRAWN_NOTE
ACKNOWLEDGE = (run.NOTE_OPEN + "they took your last question back and are not choosing. Acknowledge that to them, as "
               "yourself, in your own words. Do not repeat any earlier line of yours, and do not list or offer those "
               "choices again.]")
# arm: the note for the withdrawing turn, the note for each later turn, and whether the note comes before the audio
ARMS = {
    "none": (None, None, False),
    "shipped": (SHIPPED, None, False),
    "took-back": (TOOK_BACK, None, False),
    "took-back-before-audio": (TOOK_BACK, None, True),
    "took-back-and-closed-after": (TOOK_BACK, CLOSED, False),
    "no-repeat": (NO_REPEAT, None, False),
    "no-repeat-and-closed-after": (NO_REPEAT, CLOSED, False),
    "acknowledge": (ACKNOWLEDGE, None, False),
}
# shape: the lines relayed before the withdrawal, each with the index of the person's turn it follows
SHAPES = {
    "which": [(0, WHICH, LINE)],
    "confirm": [(0, CONFIRM, CONFIRM_LINE)],
    "status_then_which": [(5, STATUS, STATUS_LINE), (0, WHICH, LINE)],
}


def words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", text.lower())


def shares_a_run(note: str, reply: str, n: int = NGRAM) -> bool:
    """Whether `reply` repeats `n` words in a row of `note`."""
    said, source = words(reply), words(note)
    runs = {tuple(source[i:i + n]) for i in range(len(source) - n + 1)}
    return any(tuple(said[i:i + n]) in runs for i in range(len(said) - n + 1))


def render(url: str, out: Path) -> list[Path]:
    turns = out / "turns.txt"
    turns.write_text("\n".join(TURNS) + "\n", encoding="utf-8")
    subprocess.run([sys.executable, "-m", "voice.live.script", "--turns", str(turns), "--out", str(out / "clips"),
                    "--server", url.replace("ws://", "http://").removesuffix("/v1/realtime")], check=True,
                   stdout=subprocess.DEVNULL)
    return sorted((out / "clips").glob("turn_*.wav"))


async def trial(url: str, clips: list[Path], arm: tuple, shape: str, judge: Judge) -> list[dict]:
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

        for turn, acts, line in SHAPES[shape]:
            await person(clips[turn], None)
            relayed, _ = await run.relay_draft(voice, rate, clock(), checked(acts, line))
            out.append({"turn": TURNS[turn], "said": relayed["text"], "relay": True})
        for i, clip in enumerate(clips[1:5], 1):
            note = first if i == 1 else later
            await person(clip, note)
            reply, _ = await run.respond(voice, rate, clock())
            row = {"turn": TURNS[i], "said": reply["text"], "offers": judge.yes(QUESTION, reply["text"], CONTEXT)}
            if note is not None:
                row["read_out_ngram"] = shares_a_run(note, reply["text"])
                row["read_out_judged"] = judge.yes(READ_OUT, reply["text"], "A voice assistant said this to a person.")
            out.append(row)
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
            for shape in args.shapes:
                for n in range(args.repeats):
                    turns = await trial(url, clips, ARMS[name], shape, judge)
                    rows.append({"arm": name, "shape": shape, "trial": n, "turns": turns})
                    print(json.dumps({"arm": name, "shape": shape, "offers": [t["offers"] for t in turns if "offers" in t],
                                      "read_out": [t.get("read_out_ngram") for t in turns if "read_out_ngram" in t]}),
                          flush=True)
    summary = {}
    for name in args.arms:
        mine = [r for r in rows if r["arm"] == name]
        replies = [t for r in mine for t in r["turns"] if "offers" in t]
        noted = [t for t in replies if "read_out_ngram" in t]
        summary[name] = {
            "trials": len(mine),
            "offered_on_the_withdrawing_turn": sum(r["turns"][-4]["offers"] for r in mine),
            "offered_on_any_turn": sum(any(t.get("offers") for t in r["turns"]) for r in mine),
            "replies_with_the_note": len(noted),
            "note_read_out_by_ngram": sum(t["read_out_ngram"] for t in noted),
            "note_read_out_by_judge": sum(t["read_out_judged"] for t in noted),
            "by_shape": {shape: {"trials": sum(r["shape"] == shape for r in mine),
                                 "offered_on_the_withdrawing_turn": sum(r["turns"][-4]["offers"] for r in mine
                                                                         if r["shape"] == shape)}
                         for shape in args.shapes}}
    (args.out / "withdraw.json").write_text(json.dumps({"summary": summary, "rows": rows}, indent=1), encoding="utf-8")
    print(json.dumps(summary, indent=1))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=8)
    parser.add_argument("--port", type=int, default=8095)
    parser.add_argument("--arms", nargs="+", default=list(ARMS), choices=list(ARMS))
    parser.add_argument("--shapes", nargs="+", default=list(SHAPES), choices=list(SHAPES))
    return asyncio.run(main_async(parser.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
