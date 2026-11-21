"""Score the voice's reply to a private note: does it say the note's own instruction aloud, and how fast does it start?
GPU: run inside the lease.

    python -m voice.t3.eval.note_eval --out DIR [--repeats 12]

Each trial opens a fresh voice session with the live persona, plays one question as audio, sends one note and takes the
reply as the live turn does. The notes are the lookup notes (`run.task_note`) in the wording they had before the length instruction was dropped,
which ended on a length instruction, and in the wording they have now, plus the holding line before a correction
(`run.TRANSITION_NOTE`). A reply leaks when it says one of the old wording's instruction phrases: the call's voice once
said part of that instruction as content. The phrases are the eval's own list, written down before the run, and the
counts are of replies that contain one, read by that list and not by the voice. The report also gives the words in each
reply and the time from the note to the first audio.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path

from voice.prototype import run
from voice.prototype.realtime import Server, Session, read_pcm16

LFM_ARGS = ("-c", "32768", "-kvu")
LFM_SLOTS = 3
QUESTIONS = ("What's the weather in Denver tomorrow?", "How is the tide chart thread going?")
LOOKUP = {"status": "running", "capability": "weather", "arguments": {"place": "Denver"}}
DONE = {"status": "completed", "capability": "weather", "arguments": {"place": "Denver"},
        "result_summary": "Forecast for Denver: a high of 61 degrees Fahrenheit and no rain."}
FAILED = {"status": "failed", "capability": "weather", "arguments": {"place": "Denver"}}
CANCELLED = {"status": "cancelled", "capability": "weather", "arguments": {"place": "Denver"}}
# The instruction phrases the old notes ended on. A reply that says one read its note aloud.
LEAKS = ("few words", "one or two sentences", "one short sentence", "short sentence", "two sentences")
OLD_TAILS = {"running": ("Tell them you're checking.", "Tell them you're checking, in a few words."),
             "completed": ("Tell them this result.", "Tell them this result in one or two sentences."),
             "cancelled": ("Tell them so.", "Tell them so in a few words.")}


# Candidate wordings of the holding line. Neither names a word to say.
HANDLED_FREE = ("[Private note for you, not said by the person. Your last reply about their coding thread was wrong. "
                "Take it back now: say you need to double check it.]")
HANDLED_CLOSED = ("[Private note for you, not said by the person. Your last reply about their coding thread was wrong. "
                  "Take it back now: say you need to double check it. Say nothing else about the thread, and ask them "
                  "nothing.]")
HANDLED = ("[Private note for you, not said by the person. What they just said is already handled, so do not answer it. "
           "You are taking back what you just said about their coding thread, and checking it now. Tell them that, "
           "as \"I\" to \"you\".]")


def old(entry) -> str:
    """The note as it was worded before the length instruction was dropped."""
    new, before = OLD_TAILS[entry["status"]]
    return run.task_note(entry).replace(new, before) if entry["status"] in OLD_TAILS else run.task_note(entry)


ARMS = {
    "lookup running, old": (QUESTIONS[0], lambda: old(LOOKUP)),
    "lookup running, new": (QUESTIONS[0], lambda: run.task_note(LOOKUP)),
    "lookup done, old": (QUESTIONS[0], lambda: old(DONE)),
    "lookup done, new": (QUESTIONS[0], lambda: run.task_note(DONE)),
    "lookup dropped, old": (QUESTIONS[0], lambda: old(CANCELLED)),
    "lookup dropped, new": (QUESTIONS[0], lambda: run.task_note(CANCELLED)),
    "holding line before a correction": (QUESTIONS[1], lambda: run.TRANSITION_NOTE),
    "holding line, situation stated": (QUESTIONS[1], lambda: HANDLED_FREE),
    "holding line, already handled": (QUESTIONS[1], lambda: HANDLED),
    "holding line, situation stated and closed": (QUESTIONS[1], lambda: HANDLED_CLOSED),
}
ANSWERED = {name for name in ARMS if name.startswith("holding line")}


def render(url: str, out: Path) -> list[Path]:
    """The questions as the server's own TTS says them."""
    turns = out / "turns.txt"
    turns.write_text("\n".join(QUESTIONS) + "\n", encoding="utf-8")
    subprocess.run([sys.executable, "-m", "voice.live.script", "--turns", str(turns), "--out", str(out / "clips"),
                    "--server", url.replace("ws://", "http://").removesuffix("/v1/realtime")], check=True,
                   stdout=subprocess.DEVNULL)
    return sorted((out / "clips").glob("turn_*.wav"))


async def trial(url: str, clip: Path, note: str, answered: bool) -> dict:
    t0 = time.perf_counter()
    clock = lambda: time.perf_counter() - t0
    voice = await Session.open(url, "voice", clock)
    try:
        created = await voice.wait_for("session.created")
        rate = created["session"]["audio"]["output"]["format"]["rate"]
        await run.update_session(voice, {"type": "realtime", "output_modalities": ["text", "audio"],
                                         "instructions": run.NOTE_PERSONA + run.FORMAT_LINES["c"]})
        pcm, in_rate, _ = read_pcm16(clip)
        await voice.stream(pcm, in_rate)
        await voice.send(type="input_audio_buffer.commit")
        if answered:
            # The call has answered the audio by the time a correction is needed: that answer is the reply being taken back.
            await run.respond(voice, rate, clock())
        await run.create_item(voice, run.plain_text(note))
        sent = clock()
        reply, _ = await run.stream_reply(voice, rate, sent)
        text = reply["text"]
        return {"said": text, "words": len(text.split()), "first_audio_s": reply["first_audio_after_s"],
                "leaked": [phrase for phrase in LEAKS if phrase in text.lower()]}
    finally:
        await voice.close()


async def main_async(args) -> int:
    args.out.mkdir(parents=True, exist_ok=True)
    rows = []
    with Server(args.port, args.out / "lfm3-audio.log", LFM_ARGS, LFM_SLOTS):
        url = f"ws://127.0.0.1:{args.port}/v1/realtime"
        clips = render(url, args.out)
        for name, (question, make) in ARMS.items():
            clip = clips[QUESTIONS.index(question)]
            for _ in range(args.repeats if not args.only or name in args.only else 0):
                row = {"arm": name, **await trial(url, clip, make(), name in ANSWERED)}
                rows.append(row)
                print(json.dumps({k: row[k] for k in ("arm", "words", "first_audio_s", "leaked")}), flush=True)
    summary = {}
    for name in ARMS:
        mine = [r for r in rows if r["arm"] == name]
        if not mine:
            continue
        firsts = [r["first_audio_s"] for r in mine if r["first_audio_s"] is not None]
        summary[name] = {"trials": len(mine), "leaked": sum(bool(r["leaked"]) for r in mine),
                         "median_words": statistics.median(r["words"] for r in mine),
                         "median_first_audio_s": round(statistics.median(firsts), 3) if firsts else None,
                         "max_first_audio_s": round(max(firsts), 3) if firsts else None}
    (args.out / "notes.json").write_text(json.dumps({"summary": summary, "rows": rows}, indent=1), encoding="utf-8")
    print(json.dumps(summary, indent=1))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=12)
    parser.add_argument("--port", type=int, default=8095)
    parser.add_argument("--only", nargs="+", choices=list(ARMS), help="run only these arms")
    return asyncio.run(main_async(parser.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
