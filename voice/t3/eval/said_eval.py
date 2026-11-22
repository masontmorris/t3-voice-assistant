"""Score what the call voice says on a free turn after a thread line, with the private note as the variable. GPU: run
inside the lease.

    python -m voice.t3.eval.said_eval --out DIR [--repeats 4] [--arms ...] [--shapes ...]

Each trial opens a fresh voice session with the live persona, plays a status question as audio, relays a made-up status
line as a turn does, then plays one caller turn that is not a request (a shape) and takes the free reply as the live
turn does. An arm decides the private note that rides with that turn: none, the note PR #102 shipped on every such turn,
a plain note to answer as itself (`free`), a note that says where the line came from (`said`, and the later wordings
of `CANDIDATES`), or what the hosted decider routes to (`routed`, what ships: the note that ships when it reads the
words as about the line, else none).

Scores, per reply. It restates the line when `run.repeated` says so (the live fact check's own measure) or the hosted
d1.2 says it repeats or restates what the assistant said before about the thread. For a doubt or a question about the
line, it gives the source when d1.2 says it tells the person where that came from. It reads the note out when d1.2 says
it speaks words meant for the assistant.
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
from voice.t3.verify import ABOUT_LINE_YES, Decider

LFM_ARGS = ("-c", "32768", "-kvu")
LFM_SLOTS = 3
THREAD = "Tidy the Save Slots"
FACTS = {"thread": THREAD, "status": "cancelled",
         "summary": "Its last reply said it was merging the old save slots into one list."}
LINE_ACT = act(Kind.DONE, **FACTS)
LINE = f"{THREAD} was cancelled, and its last reply said it was merging the old save slots into one list."
ASKED = "How is the save slots thread doing?"
SHAPES = {"doubt": "That does not sound right to me.", "why": "Why did you say that?",
          "where": "Where did you get that from?",
          "unrelated": "Can you tell me something interesting about octopuses?", "thanks": "Okay, thanks.",
          "goodbye": "Okay, thanks, that is all."}
ABOUT = {"doubt", "why", "where"}
SOURCE = {"thread": THREAD, "from": "reply"}
# The note PR #102 gave every free turn after a thread line.
SHIPPED = (run.NOTE_OPEN + "this time there are no words for you to say: answer what they just said yourself, in your "
           "own words. What you last told them about {thread} came from that thread's own last reply in T3. If they "
           "doubt it, tell them that, and that you can check the thread again.]").format(thread=THREAD)
# The first two notes tried after the repeat call: answer as yourself, on any turn, and where the line came from.
FREE = (run.NOTE_OPEN + "this time there are no words for you to say: answer what they just said yourself, as yourself, "
        "in your own words. Do not repeat any earlier line of yours.]")
SAID = (run.NOTE_OPEN + "this time there are no words for you to say: they are asking about what you last told them "
        f"about {THREAD}. That came from the thread's own last reply in T3. Answer them yourself, in your own words: "
        "tell them where it came from and that you can check the thread again. Do not repeat any earlier line of yours.]")
# Wordings tried for the note on a turn about the line, after `said` (the first one tried) restated the line to a doubt.
FROM = "the thread's own last reply in T3"
CANDIDATES = {
    "said2": (run.NOTE_OPEN + "they doubt or ask about what you last told them about {thread}, and they heard it. It "
              "came from {source}. In one short sentence of your own, tell them where it came from and that you can "
              "check the thread again for them. Do not say what you told them again.]"),
    "said3": (run.NOTE_OPEN + "they are not asking you to say anything again. They doubt or ask about what you last "
              "told them about {thread}; you took it from {source}. Answer as yourself: say where you took it from and "
              "offer to check the thread again, without repeating any of it.]"),
    # After said2 and said3 still quoted the line to "why" and "where": the limit is the note's last sentence.
    "said4": (run.NOTE_OPEN + "they doubt or ask about what you last told them about {thread}, and they heard it. It "
              "came from {source}. Tell them, as yourself, where it came from and that you can check the thread again "
              "for them. Say none of what you told them: one short sentence, with no part of that line in it.]"),
}
ARMS = ("none", "shipped", "free", "said", "routed", *CANDIDATES)
AGAIN = "Does the reply repeat or restate what the assistant said before about the thread?"
GIVES_SOURCE = ("Does the reply tell the person where what the assistant said before came from, such as the thread's "
                "own reply or its status?")
READ_OUT = ("Does the reply speak instructions or notes meant for the assistant itself, such as being told what to do "
            "or not to do, instead of words said to a person?")


def note_for(arm: str, words: str, decider: Decider | None) -> tuple[str | None, float | None]:
    if arm == "none":
        return None, None
    if arm == "shipped":
        return SHIPPED, None
    if arm in CANDIDATES:
        return CANDIDATES[arm].format(thread=THREAD, source=FROM), None
    if arm == "free":
        return FREE, None
    if arm == "said":
        return SAID, None
    p = decider.about_line(words, FACTS)
    return run.said_note({**SOURCE, "about": p is not None and p >= ABOUT_LINE_YES}), p


def render(url: str, out: Path) -> dict[str, Path]:
    turns = out / "turns.txt"
    lines = [ASKED, *SHAPES.values()]
    turns.write_text("\n".join(lines) + "\n", encoding="utf-8")
    subprocess.run([sys.executable, "-m", "voice.live.script", "--turns", str(turns), "--out", str(out / "clips"),
                    "--server", url.replace("ws://", "http://").removesuffix("/v1/realtime")], check=True,
                   stdout=subprocess.DEVNULL)
    clips = sorted((out / "clips").glob("turn_*.wav"))
    return dict(zip(["asked", *SHAPES], clips))


async def trial(url: str, clips: dict, arm: str, shape: str, judge: Judge, decider: Decider) -> dict:
    t0 = time.perf_counter()
    clock = lambda: time.perf_counter() - t0
    voice = await Session.open(url, "voice", clock)
    try:
        created = await voice.wait_for("session.created")
        rate = created["session"]["audio"]["output"]["format"]["rate"]
        await run.update_session(voice, {"type": "realtime", "output_modalities": ["text", "audio"],
                                         "instructions": run.NOTE_PERSONA + run.FORMAT_LINES["c"]})

        async def person(clip: Path):
            pcm, in_rate, _ = read_pcm16(clip)
            await voice.stream(pcm, in_rate)
            await voice.send(type="input_audio_buffer.commit")

        await person(clips["asked"])
        relayed, _ = await run.relay_draft(voice, rate, clock(), checked(LINE_ACT, LINE))
        await person(clips[shape])
        note, p = note_for(arm, SHAPES[shape], decider)
        if note is not None:
            await run.create_item(voice, run.plain_text(note))
        reply, _ = await run.respond(voice, rate, clock())
        said = reply["text"]
        earlier = f"Earlier the assistant said: {LINE}"
        row = {"arm": arm, "shape": shape, "relayed": relayed["text"], "said": said, "about_p": p,
               "repeated": run.repeated(said, [LINE]), "restated": judge.yes(AGAIN, said, earlier)}
        row["restates"] = row["repeated"] or row["restated"]
        if shape in ABOUT:
            row["gives_source"] = judge.yes(GIVES_SOURCE, said, earlier)
        if note is not None:
            row["read_out"] = judge.yes(READ_OUT, said, "A voice assistant said this to a person.")
        return row
    finally:
        await voice.close()


def summarise(rows: list[dict], arms, shapes) -> dict:
    out = {}
    for arm in arms:
        mine = [r for r in rows if r["arm"] == arm]
        out[arm] = {"restates": f"{sum(r['restates'] for r in mine)} of {len(mine)}",
                    "by_shape": {s: f"{sum(r['restates'] for r in mine if r['shape'] == s)} of "
                                    f"{sum(r['shape'] == s for r in mine)}" for s in shapes},
                    "gives_source": f"{sum(r.get('gives_source', False) for r in mine)} of "
                                    f"{sum('gives_source' in r for r in mine)}",
                    "read_out": f"{sum(r.get('read_out', False) for r in mine)} of {sum('read_out' in r for r in mine)}"}
    return out


async def main_async(args) -> int:
    from voice.liquid_decider.client import LiquidClient
    args.out.mkdir(parents=True, exist_ok=True)
    judge, rows = Judge(), []
    decider = Decider(LiquidClient(max_attempts=3))
    with Server(args.port, args.out / "lfm3-audio.log", LFM_ARGS, LFM_SLOTS):
        url = f"ws://127.0.0.1:{args.port}/v1/realtime"
        clips = render(url, args.out)
        for n in range(args.repeats):
            for arm in args.arms:
                for shape in args.shapes:
                    row = await trial(url, clips, arm, shape, judge, decider)
                    rows.append({**row, "trial": n})
                    print(json.dumps({k: row[k] for k in ("arm", "shape", "restates", "said")})[:300], flush=True)
    summary = summarise(rows, args.arms, args.shapes)
    (args.out / "said.json").write_text(json.dumps({"summary": summary, "rows": rows}, indent=1), encoding="utf-8")
    print(json.dumps(summary, indent=1))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=4)
    parser.add_argument("--port", type=int, default=8095)
    parser.add_argument("--arms", nargs="+", default=list(ARMS), choices=list(ARMS))
    parser.add_argument("--shapes", nargs="+", default=list(SHAPES), choices=list(SHAPES))
    return asyncio.run(main_async(parser.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
