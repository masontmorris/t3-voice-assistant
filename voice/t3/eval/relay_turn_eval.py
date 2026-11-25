"""Score the call voice's relay of a checked line said right after the caller spoke. GPU: run inside the lease.

    python -m voice.t3.eval.relay_turn_eval --out DIR [--arms shipped ...] [--shapes yes ...]

Each trial opens a fresh voice session with the live persona and plays one short made-up call: caller turns as audio
from the server's own TTS, and after each the turn's checked line relayed through the product's own path
(`run.say_acts`: two relays, then nothing). Idle news is relayed with no caller audio before it. A shape is what comes
just before the scored relay: a yes to a confirmation and its result, a thanks and the confirmation asked again, an
approval asked for after the news of it (in two sentences as the writer worded it before 2026-11-24, or in one of
four one-sentence forms, `FORMS`), a question about a status just said and that status again, or idle news. Each
shape has 15 calls (10 for a one-sentence form) that differ in thread, project, message and the caller's words: the
voice's text is decoded greedily, so a shape is many different calls rather than repeats of one; the caller audio and
the reply's audio codes are still sampled afresh, so a rerun of one call can differ. A relay recites on the first try, on the
second, or falls back, by the live recital check (`recites`). An arm is the relay note's wording, or a text-only
answer to the audio before the relay. Results: docs/voice/spoken-acts.md.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

from voice.live.t3_tasks import update_entry
from voice.prototype import run
from voice.prototype.realtime import Server, Session, read_pcm16
from voice.t3 import lifecycle as L
from voice.t3.acts import RULES, Kind, act
from voice.t3.eval.relay_eval import checked
from voice.t3.speech import Rendered, Speech

LFM_ARGS = ("-c", "32768", "-kvu")
LFM_SLOTS = 3
THREADS = ["Polish the Main Menu", "Fix the Pause Menu", "Sort the Seed Catalogue", "Trim the Harbour Tiles",
           "Speed Up the Save Screen"]
PROJECTS = [("Moss Notes", "sorting the seed packets by season"),
            ("Tide Tables", "exporting the tide times as a table"),
            ("Kiln Log", "giving each firing its own page"), ("Fern Atlas", "adding a search box to the atlas"),
            ("Brook Survey", "plotting the survey points on a map")]
MESSAGES = ["also add a unit test", "keep the old colours", "write a short changelog", "check it on a small screen",
            "leave the save format alone"]
YES = ["Yeah.", "Yes, please.", "Sure, go ahead."]
THANKS = ["Okay, thanks.", "Thanks.", "Got it, thanks."]
APPROVE = ["Okay, go ahead and approve it.", "Yes, approve it.", "Sure, let it run the command."]
QUESTIONS = ["Is it done yet?", "Is it still going?", "So it's not finished?"]


def confirmations(i: int):
    """The i-th request, its confirmation and the result of a yes to it, cycling through start, send and stop."""
    thread, (project, purpose), message = THREADS[i % 5], PROJECTS[i % 5], MESSAGES[i % 5]
    return [
        (f"Uhm, I was wondering if you could start a thread in {project} for, uhm, {purpose}? Can you kick that off?",
         act(Kind.CONFIRM_LAUNCH, project=project), f"Should I start a new coding thread in {project}?",
         act(Kind.LAUNCHED, project=project), f"A new coding thread has started in {project}."),
        (f"Uhm, so, could you tell the {thread.lower()} thread to, uhm, {message}? That would be great.",
         act(Kind.CONFIRM_TELL, thread=thread, message=message),
         f"Should I send '{message}' to {thread} as a follow-up?",
         act(Kind.SENT_FOLLOWUP, thread=thread), f"Your follow-up reached {thread}."),
        (f"Uhm, I think the {thread.lower()} thread is going the wrong way. Can you, uhm, stop it?",
         act(Kind.CONFIRM_STOP, thread=thread), f"Should I stop {thread}?",
         act(Kind.STOPPING, thread=thread), f"I'm stopping {thread} now."),
    ][i // 5]


# Threads the call starts, as the start scenarios ask for them: (project, the request as said, what it is for).
STARTED = [
    ("Garden Sandbox",
     "So uh, I want to get some work going on, uhm, a small project, a tool that sorts the seed packets by season, "
     "and uhm, could you start a thread for that? And you know, keep it organized.",
     "a tool that sorts the seed packets by season"),
    ("Harbour Sandbox",
     "Okay, so uhm, I was thinking, uh, maybe we could have a thread that works out why the tide chart loads so "
     "slowly. Sort of a little project, if that makes sense? Can you kick that off for me?",
     "working out why the tide chart loads slowly"),
    ("Kiln Sandbox",
     "Uh, so quick one, well, not that quick, uhm, I need some work started on the, the firing log, the part that "
     "keeps each firing on its own page, a small project really, could you open a thread for it?",
     "keeping each firing of the kiln log on its own page"),
    ("Atlas Sandbox",
     "Uhm, I was wondering if you could start a thread to get work going on, uhm, a little project to make, uhm, the "
     "fern atlas search faster? Can you kick that off and keep it tidy?",
     "a project to make the fern atlas search faster"),
    ("Survey Sandbox",
     "So, uhm, could you start a thread for me, uh, to plot the brook survey points on a map, a small thing, and, "
     "uhm, just get it going?",
     "plotting the brook survey points on a map"),
]
# How the confirmation of an approval for a thread the call started is worded, by shape: two sentences, as the writer
# worded it before 2026-11-24, and four ways of asking it in one.
FORMS = {
    "approve": "{named} is waiting for your approval to run a command. Should I approve it?",
    "give": "Should I give {thread} your approval to run a command?",
    "comma": "{named} is waiting for your approval to run a command, so should I approve it?",
    "now": "Should I approve it now that {thread} is waiting for your approval to run a command?",
    "for": "Should I approve it for {thread}, which is waiting for your approval to run a command?",
}


def approval(i: int, form: str) -> list:
    """A call that starts a thread, is told it needs approval to run a command, and is asked to approve it, as the
    `news-permission` scenario goes. The first ten are about the thread the call started, which has no title, and are
    asked in `form`. The last five are about a named thread, asked in its one-sentence example's shape, and are scored
    under "approve" only."""
    need = {"decision": "approve", "need": "command_approval"}
    if i < 10:
        project, request, purpose = STARTED[i % 5]
        begun = {"begun_in": project, "begun_for": purpose}
        thread = f"the thread you started in {project} for {purpose}"
        named = thread[0].upper() + thread[1:]
        return [(request, act(Kind.CONFIRM_LAUNCH, project=project),
                 f"Should I start a new coding thread in {project}?", "request"),
                ("Yeah.", act(Kind.LAUNCHED, project=project), f"A new coding thread has started in {project}.", "yes"),
                (None, act(Kind.NEEDS_YOU, need="command_approval", **begun),
                 f"{named} needs your approval to run a command.", "idle"),
                (APPROVE[i // 5], act(Kind.CONFIRM_RESPOND, **need, **begun),
                 FORMS[form].format(thread=thread, named=named), "approve")]
    thread = THREADS[i % 5]
    return [(None, act(Kind.NEEDS_YOU, thread=thread, need="command_approval"),
             f"{thread} needs your approval to run a command.", "idle"),
            (APPROVE[i % 3], act(Kind.CONFIRM_RESPOND, thread=thread, **need),
             f"Should I approve {thread} running a command?", "approve")]


def calls() -> list[tuple[str, list]]:
    """(shape, steps) per call. A step is (caller words or None for idle news, acts, line, label)."""
    out = []
    for i in range(15):
        ask, confirm, question, result, said = confirmations(i)
        out.append(("yes", [(ask, confirm, question, "request"), (YES[i % 3], result, said, "yes")]))
        out.append(("thanks", [(ask, confirm, question, "request"), (THANKS[i % 3], confirm, question, "thanks")]))
        out.append(("approve", approval(i, "approve")))
        out.extend((form, approval(i, form)) for form in FORMS if form != "approve" and i < 10)
        thread = THREADS[i % 5]
        need = act(Kind.NEEDS_YOU, thread=thread, need="command_approval")
        working = act(Kind.THREAD_STATUS, thread=thread, status="working")
        out.append(("question", [(f"How is the {thread.lower()} thread going?", working,
                                  f"The {thread} thread is working.", "request"),
                                 (QUESTIONS[i // 5], working, f"The {thread} thread is still working.", "question")]))
        out.append(("idle", [(f"How is the {thread.lower()} thread going?", working,
                              f"The {thread} thread is working.", "request"),
                             (None, need, f"{thread} needs your approval to run a command.", "idle")]))
    return out


class Words:
    """The call's say/accept/refuse over a line already checked: this eval scores the saying, not the wording."""

    def __init__(self, line):
        self.line = line

    def say(self, entry, turns=()):
        return Rendered(self.line, verdict=self.line.verdict)

    def accept(self, entry, text, draft):
        return Speech.recite(draft, text)

    def refuse(self, entry, rendered):
        pass


def render(url: str, out: Path, texts: list[str]) -> dict[str, Path]:
    (out / "turns.txt").write_text("\n".join(texts) + "\n", encoding="utf-8")
    subprocess.run([sys.executable, "-m", "voice.live.script", "--turns", str(out / "turns.txt"),
                    "--out", str(out / "clips"),
                    "--server", url.replace("ws://", "http://").removesuffix("/v1/realtime")], check=True,
                   stdout=subprocess.DEVNULL)
    return dict(zip(texts, sorted((out / "clips").glob("turn_*.wav")), strict=True))


SHIPPED = run.relay_note


def repeat_note(text, attempt=0):
    """The shipped note, told that the words may repeat what it said before."""
    return SHIPPED(text, attempt).replace(
        "from the first word to the last, ",
        "from the first word to the last, even where they repeat what you said before, ")


def again_note(text, attempt=0):
    """The shipped note, told that the person needs to hear the words though it may have said them before."""
    return SHIPPED(text, attempt).replace(
        "Say exactly these words",
        "They need to hear these words now, though you may have said them before. Say exactly these words")


def reask_note(text, attempt=0, reasked=False):
    """For a confirmation asked again because the reply did not answer it, the note says so instead of saying the
    reply was handled."""
    note = SHIPPED(text, attempt)
    if not reasked:
        return note
    return note.replace("What they just said is already handled", "What they just said does not answer your question")


def pending_note(text, attempt=0, reasked=False):
    """For a confirmation asked again, the note says the question is still open."""
    note = SHIPPED(text, attempt)
    if not reasked:
        return note
    return note.replace("What they just said is already handled, so do not answer it.",
                        "Your question to them is still open: what they just said did not answer it.")


def ask_note(text, attempt=0, asks=False):
    """For a line that asks the person something, the note says to ask them it, not to say some words."""
    note = SHIPPED(text, attempt)
    return note.replace("Say exactly these words to them now", "Ask them this now, in exactly these words") if asks \
        else note


# arm: the relay note, and whether the caller's audio is first answered in text nobody hears (`run.answer_unheard`)
ARMS = {"shipped": (SHIPPED, False), "repeat": (repeat_note, False), "again": (again_note, False),
        "unheard": (SHIPPED, True), "ask": (ask_note, False), "reask": (reask_note, False),
        "pending": (pending_note, False)}


async def trial(url: str, clips: dict[str, Path], steps: list, arm: tuple) -> list[dict]:
    note, unheard = arm
    t0 = time.perf_counter()
    clock = lambda: time.perf_counter() - t0
    voice = await Session.open(url, "voice", clock)
    rows = []
    try:
        created = await voice.wait_for("session.created")
        rate = created["session"]["audio"]["output"]["format"]["rate"]
        await run.update_session(voice, {"type": "realtime", "output_modalities": ["text", "audio"],
                                         "instructions": run.NOTE_PERSONA + run.FORMAT_LINES["c"]})
        said = []
        for words, acts, line, label in steps:
            events = []
            log = lambda kind, **fields: events.append({"kind": kind, **fields})
            if words is not None:
                pcm, in_rate, _ = read_pcm16(clips[words])
                await voice.stream(pcm, in_rate)
                await voice.send(type="input_audio_buffer.commit")
                if unheard:
                    await run.answer_unheard(voice, rate, clock(), log)
            entry = update_entry(L.Update(f"eval:{label}", (acts,), "eval", "eval"), "eval")

            async def send(spoken, pcm):
                return pcm, clock()

            flag = {ask_note: {"asks": RULES[acts.kind].question}, reask_note: {"reasked": label == "thanks"},
                    pending_note: {"reasked": label == "thanks"}}.get(note)
            run.relay_note, kept = ((lambda text, attempt=0: note(text, attempt, **flag)) if flag is not None
                                    else note), run.relay_note
            try:
                await run.say_acts(voice, rate, clock(), entry, Words(checked(acts, line)),
                                   [{"user": words or "", "assistant": said}], log, send_audio=send)
            finally:
                run.relay_note = kept
            tries = [e for e in events if e["kind"] == "t3_call_reply"]
            ok = [t["error"] is None and not t["problems"] and t["status"] == "completed" for t in tries]
            result = "first" if ok[:1] == [True] else "second" if ok[1:2] == [True] else "fell"
            rows.append({"label": label, "turn": words, "line": line, "result": result,
                         "tries": [t["text"] for t in tries]})
            if result != "fell":
                said.append(line)
        return rows
    finally:
        await voice.close()


def table(rows: list[dict]) -> dict:
    """Per arm and shape, the scored (last) relay of each call; `history` is every relay before one."""
    out = {}
    for arm in dict.fromkeys(r["arm"] for r in rows):
        for shape in [*dict.fromkeys(r["shape"] for r in rows), "history"]:
            chosen = [r for r in rows if r["arm"] == arm and (r["last"] and r["shape"] == shape
                                                             or shape == "history" and not r["last"])]
            counts = Counter(r["result"] for r in chosen)
            out[f"{arm} {shape}"] = {k: counts.get(k, 0) for k in ("first", "second", "fell")}
    return out


async def main_async(args) -> int:
    args.out.mkdir(parents=True, exist_ok=True)
    chosen = [(shape, steps) for shape, steps in calls() if shape in args.shapes]
    texts = list(dict.fromkeys(w for _, steps in chosen for w, *_ in steps if w is not None))
    rows = []
    with Server(args.port, args.out / "lfm3-audio.log", LFM_ARGS, LFM_SLOTS):
        url = f"ws://127.0.0.1:{args.port}/v1/realtime"
        clips = render(url, args.out, texts)
        for n, (shape, steps) in enumerate(chosen):
            for arm in args.arms:
                got = await trial(url, clips, steps, ARMS[arm])
                for i, row in enumerate(got):
                    rows.append({"arm": arm, "shape": shape, "call": n, "last": i == len(got) - 1, **row})
                print(json.dumps({"arm": arm, "shape": shape, "results": [r["result"] for r in got],
                                  "tries": got[-1]["tries"]}), flush=True)
    summary = table(rows)
    (args.out / "relay_turn.json").write_text(json.dumps({"summary": summary, "rows": rows}, indent=1),
                                              encoding="utf-8")
    print(json.dumps(summary, indent=1))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8095)
    parser.add_argument("--arms", nargs="+", default=list(ARMS), choices=list(ARMS))
    shapes = ["yes", "thanks", *FORMS, "question", "idle"]
    parser.add_argument("--shapes", nargs="+", default=shapes, choices=shapes)
    return asyncio.run(main_async(parser.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
