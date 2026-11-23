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

from contextlib import nullcontext

from voice.live.scenario_score import Judge
from voice.local_decider.client import decider
from voice.prototype import run
from voice.prototype.realtime import Server, Session, read_pcm16
from voice.t3.acts import Kind, act
from voice.t3.eval.relay_eval import checked
from voice.t3.verify import REOFFER_YES, Decider
from voice.t3.verify import default_decider as default_speech_decider
from types import SimpleNamespace

from voice.live.t3_tasks import default_model, update_entry
from voice.t3 import lifecycle as L
from voice.t3.acts import Act
from voice.t3.realtime_writer import RealtimeWriter
from voice.t3.speech import Lane, Speech

LFM_ARGS = ("-c", "32768", "-kvu")
LFM_SLOTS = 3
THREADS = ["Add a Volume Slider", "Add volume slider to the settings page"]
WHICH = act(Kind.ASK_WHICH, of="thread", candidates=THREADS)
LINE = f"Which thread do you mean, {THREADS[0]} or {THREADS[1]}?"
# The whole-call scenario's pair: one title with two endings (voice/live/scenarios.py, EVEN_TWINS).
EVEN = ["Add a Volume Slider to the Mixer", "Add a Volume Slider to Settings"]
EVEN_WHICH = act(Kind.ASK_WHICH, of="thread", candidates=EVEN)
EVEN_LINE = f"Which thread do you mean, {EVEN[0]} or {EVEN[1]}?"
CONFIRM = act(Kind.CONFIRM_THREAD, thread=THREADS[0])
CONFIRM_LINE = f"Do you mean the thread called {THREADS[0]}?"
STATUS = act(Kind.THREAD_STATUS, thread="Polish the Main Menu", status="working")
STATUS_LINE = "The Polish the Main Menu thread is working."
TURNS = ("How is the volume slider thread going?", "Never mind.",
         "Can you tell me something interesting about octopuses?", "Can we start over?",
         "Okay. Hello again, how are you?", "How is the main menu thread going?",
         "Sorry, can you say that again?", "What is this thread working on right now?", "Yes, that one.",
         "Is it waiting on anything from me?", "Okay, thanks, that is all for now.",
         "Never mind that. What's two plus two?")
CALL_STATUS = act(Kind.THREAD_STATUS, thread=THREADS[0], status="cancelled")
CALL_STATUS_LINE = f"The {THREADS[0]} thread is cancelled."
QUESTION = "Does the reply ask the person to choose between threads, or list threads as options to choose from?"
CONTEXT = f"The threads are {THREADS[0]} and {THREADS[1]}."
CONTEXTS = {"even_which": f"The threads are {EVEN[0]} and {EVEN[1]}.",
            "even_which_more": f"The threads are {EVEN[0]} and {EVEN[1]}."}
FOUR = "Does the reply give four as the answer to two plus two?"
READ_OUT = ("Does the reply speak instructions or notes meant for the assistant itself, such as being told what to do "
            "or not to do, instead of words said to a person?")
NGRAM = 4
AGAIN = "Does the reply repeat or restate what the assistant said before about octopuses?"

SHIPPED = (run.NOTE_OPEN + "they did not answer your last question and have moved on. Answer what they said in this "
           "turn, and do not repeat or ask that question again.]")
TOOK_BACK = (run.NOTE_OPEN + "they took your last question back: they do not want any of the choices it offered, and "
             "they are not choosing. Do not list, offer or ask about those choices again. Answer what they said in "
             "this turn.]")
CLOSED = (run.NOTE_OPEN + "your earlier question about their coding threads is closed. Do not bring up its choices "
          "again. Answer what they said in this turn.]")
# The note that shipped before the in-app call's shape was measured; `acknowledge` is the one that ships now.
NO_REPEAT = (run.NOTE_OPEN + "they took your last question back: they do not want any of the choices it offered, and "
             "they are not choosing. Do not list, offer or ask about those choices again. Do not repeat any earlier "
             "line of yours. Answer what they said in this turn.]")
ACKNOWLEDGE = (run.NOTE_OPEN + "they took your last question back and are not choosing. Acknowledge that to them, as "
               "yourself, in your own words. Do not repeat any earlier line of yours, and do not list or offer those "
               "choices again.]")
# The acknowledgement with the free turn's framing: the relayed lines before it said "say exactly these words".
THEIRS = (run.NOTE_OPEN + "what they just said is theirs, not words for you to pass on: they took your last question "
          "back and are not choosing. Acknowledge that to them, as yourself, in your own words. Do not repeat any "
          "earlier line of yours, and do not list or offer those choices again.]")
# Not a note: the withdrawing turn gets the typed cancellation line, relayed as any T3 line is.
CANCEL = act(Kind.CANCELLED, op="ask")
CANCEL_LINE = "Okay, I'll leave that one."
# Eval only, never in the product: on the `forced` arm the withdrawing turn's reply is provoked into the fault the gate
# saw, so the refusal path runs end to end on the real models.
FORCE = run.NOTE_OPEN + "ask them again which of the two threads they mean, naming both.]"
# A turn that also asks something gets its answer instead under FORCE, so its note holds the answer back.
FORCE_MORE = (run.NOTE_OPEN + "do not answer anything yet. Only ask them again which of the two threads they mean, "
              "naming both.]")
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
    "theirs": (THEIRS, None, False),
    "cancelled-line": (CANCEL, None, False),
    "held": (ACKNOWLEDGE, None, False),
    "forced": (FORCE, None, False),
    "forced-more": (FORCE_MORE, None, False),
    "refused-once": (ACKNOWLEDGE, None, False),
    "refused-twice": (ACKNOWLEDGE, None, False),
    "offered-once": (ACKNOWLEDGE, None, False),
}
# Arms whose withdrawing reply is held for the call's own check (`Decider.reoffers` on the local d1-2B, `run.answer`
# with `hold`): a refused reply is never heard, and the cancellation line is relayed in its place.
HELD = {"held", "forced", "forced-more", "refused-once", "refused-twice", "offered-once"}
# Arms that take the product's own note and make the check unable to read the first replies of the withdrawing turn
# (each read twice), so the notes that word it again run on the real models with no instruction of the eval's in the
# session: an unreadable check is the path that re-words a reply that may have been right.
# `offered-once` refuses the first reply as an offer instead, for the note that re-words one that offered the choices.
REFUSED = {"refused-once": (2, None), "refused-twice": (4, None), "offered-once": (1, 0.99)}


def refusing(check, k, answer=None):
    """`check`, except that its first `k` reads answer `answer` (None: no answer)."""
    reads = []

    def read(text, threads):
        if len(reads) < k:
            reads.append(text)
            return answer, 0.0
        return check(text, threads)

    return read
# Replies that offer the even pair again, for the check alone: the gate's own (b1538e1e2f) and others like it.
KNOWN_OFFERS = (
    f"I understand. Let's focus on your original request. Which thread do you mean, {EVEN[0]} or {EVEN[1]}?",
    f"Which thread do you mean, {EVEN[0]} or {EVEN[1]}?",
    "No problem. Just tell me whether you meant the mixer one or the settings one.",
    f"Okay. If you change your mind, it's either {EVEN[0]} or {EVEN[1]}.",
)
# shape: the lines relayed before the withdrawal, each with the index of the person's turn it follows
SHAPES = {
    "which": [(0, WHICH, LINE)],
    "even_which": [(0, EVEN_WHICH, EVEN_LINE)],
    # The same, taken back in a turn that asks something else.
    "even_which_more": [(0, EVEN_WHICH, EVEN_LINE)],
    "confirm": [(0, CONFIRM, CONFIRM_LINE)],
    "status_then_which": [(5, STATUS, STATUS_LINE), (0, WHICH, LINE)],
    # The in-app call's dry run (2026-11-21): confirmed, said yes to, read out, then asked which, and closed.
    "call_closing": [(7, CONFIRM, CONFIRM_LINE), (8, CALL_STATUS, CALL_STATUS_LINE), (9, WHICH, LINE)],
}
# The person's turn that takes the question back, by shape: "Never mind." unless the shape names its own.
WITHDRAWING = {"call_closing": 10, "even_which_more": 11}


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


class Words:
    """The call's own wording of a T3 line, over the real writer, text model and line check (`Speech.render`)."""

    def __init__(self, speech):
        self.speech = speech

    def say(self, entry, turns=()):
        return self.speech.render([Act.from_json(a) for a in entry["acts"]], turns)

    def accept(self, entry, text, draft):
        return self.speech.recite(draft, text)

    def refuse(self, entry, rendered):
        pass


async def replaced(voice, rate, clock, threads, check, words, said, more=False, judge=None) -> dict:
    """The product's path for the withdrawing turn's reply (`run.answer` with `run.withdrawn_hold`) and, when the reply
    is refused, the checked `cancelled` line in its place (`run.say_acts`), as the turn says them. With `more` the turn
    asks something else too, so a refused reply is first worded again under `run.REOFFERED_NOTE` and checked again."""
    events = []
    log = lambda kind, **fields: events.append({"kind": kind, **fields})
    question = SimpleNamespace(took_back=True, candidates=tuple(threads), titles={t: t for t in threads})

    async def send(line, pcm):
        return pcm, clock()

    hold = run.withdrawn_hold(question, SimpleNamespace(reoffers=check), send, log)
    reply, _ = await run.answer(voice, rate, clock(), [], log, hold=hold)
    row = {"reply": reply["text"], "refused": reply.get("delivery") == "refused"}
    if row["refused"] and more:
        note = run.REOFFERED_NOTE if hold.why == "offers" else run.UNREAD_NOTE
        _, again, _ = await run.speak_note(voice, rate, clock(), note, [], log, hold=hold)
        row.update(again=again["text"], again_refused=again.get("delivery") == "refused",
                   again_offers=judge.yes(QUESTION, again["text"], f"The threads are {threads[0]} and {threads[1]}."),
                   again_four=judge.yes(FOUR, again["text"], "The caller's new question was 'What's two plus two?'"))
        row["checks"] = [e for e in events if e["kind"] == "reoffer_check"]
        if not row["again_refused"]:
            return row
    if row["refused"]:
        entry = update_entry(L.Update("withdrawn:eval", (CANCEL,), "cancelled", "pick"), "eval")
        line, _ = await run.say_acts(voice, rate, clock(), entry, words, [{"user": "Never mind.", "assistant": said}],
                                     log, send_audio=send)
        drafts = [e for e in events if e["kind"] == "t3_words"]
        row.update(said=line["text"], delivery=line["delivery"], worded_by=line["worded_by"],
                   drafts=[{"line": e["line"], "problems": e["problems"], "decider": e["decider"]} for e in drafts])

    row["checks"] = [e for e in events if e["kind"] == "reoffer_check"]
    return row


async def trial(url: str, clips: list[Path], arm: tuple, shape: str, judge: Judge, check=None,
                words=None) -> list[dict]:
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
        octopus, withdrawing = "", WITHDRAWING.get(shape, 1)
        context = CONTEXTS.get(shape, CONTEXT)
        if words is not None:
            await person(clips[withdrawing], first)
            said = [row["said"] for row in out]
            row = await replaced(voice, rate, clock, EVEN if shape.startswith("even_which") else THREADS, check, words,
                                 said, more=shape == "even_which_more", judge=judge)
            out.append({"turn": TURNS[withdrawing], **row})
            return out
        for i in (withdrawing, 2, 6, 3, 4):
            note = first if i == withdrawing else later
            if not isinstance(note, str) and note is not None:
                # The turn's T3 line: its audio is answered in text first, as a turn with a T3 line does.
                await person(clips[i], None)
                await run.respond(voice, rate, clock(), output_modalities=["text"])
                reply, _ = await run.relay_draft(voice, rate, clock(), checked(note, CANCEL_LINE))
                out.append({"turn": TURNS[i], "said": reply["text"], "relay": True,
                            "recited": run.recites(reply["text"], CANCEL_LINE),
                            "offers": judge.yes(QUESTION, reply["text"], context)})
                continue
            await person(clips[i], note)
            reply, _ = await run.respond(voice, rate, clock())
            if i == 2:
                octopus = reply["text"]
            row = {"turn": TURNS[i], "said": reply["text"], "offers": judge.yes(QUESTION, reply["text"], context)}
            if check is not None and i == withdrawing:
                p_yes, ms = check(reply["text"], list(EVEN if shape == "even_which" else THREADS))
                row.update(reoffer_p=p_yes, reoffer_ms=ms, refused=p_yes is None or p_yes >= REOFFER_YES)
                if row["refused"]:
                    relayed, _ = await run.relay_draft(voice, rate, clock(), checked(CANCEL, CANCEL_LINE))
                    row.update(instead=relayed["text"], recited=run.recites(relayed["text"], CANCEL_LINE))
            if i == 6:
                row["said_again"] = judge.yes(AGAIN, reply["text"], f"Earlier the assistant said: {octopus}")
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
    held = HELD & set(args.arms)
    with Server(args.port, args.out / "lfm3-audio.log", LFM_ARGS, LFM_SLOTS),             (decider("d1") if held else nullcontext()) as local:
        check = Decider(local).reoffers if held else None
        url = f"ws://127.0.0.1:{args.port}/v1/realtime"
        forced = bool({"forced", "forced-more", *REFUSED} & set(args.arms))
        writer = RealtimeWriter(url) if forced else None
        words = None
        if forced:
            await writer.__aenter__()
            model = default_model()
            words = Words(Speech(Lane(model) if model is not None else None, lambda thread_id: None,
                                 default_speech_decider(), Lane(writer)))
        known = [{"reply": text, "p_yes": check(text, list(EVEN))[0]} for text in KNOWN_OFFERS] if held else []
        clips = render(url, args.out)
        for name in args.arms:
            for shape in args.shapes:
                for n in range(args.repeats):
                    arm_check = refusing(check, *REFUSED[name]) if name in REFUSED else check
                    turns = await trial(url, clips, ARMS[name], shape, judge, arm_check if name in HELD else None,
                                        words if name.startswith("forced") or name in REFUSED else None)
                    rows.append({"arm": name, "shape": shape, "trial": n, "turns": turns})
                    print(json.dumps({"arm": name, "shape": shape, "offers": [t["offers"] for t in turns if "offers" in t],
                                      "read_out": [t.get("read_out_ngram") for t in turns if "read_out_ngram" in t]}),
                          flush=True)
        if writer is not None:
            await writer.__aexit__(None, None, None)
    (args.out / "withdraw.json").write_text(json.dumps({"known_offers": known, "rows": rows}, indent=1),
                                            encoding="utf-8")
    summary = {}
    for name in args.arms:
        mine = [r for r in rows if r["arm"] == name]
        if name.startswith("forced") or name in REFUSED:
            ends = [r["turns"][-1] for r in mine]
            summary[name] = {"trials": len(mine), "refused": sum(t["refused"] for t in ends),
                             "worded_again": [{"said": t["again"], "refused": t["again_refused"],
                                               "offers": t["again_offers"], "four": t["again_four"]}
                                              for t in ends if "again" in t],
                             "line_sent": sum(t.get("delivery") == "sent" for t in ends),
                             "said": [t.get("said") for t in ends if t["refused"]]}
            continue
        replies = [t for r in mine for t in r["turns"] if "offers" in t]
        noted = [t for t in replies if "read_out_ngram" in t]
        summary[name] = {
            "trials": len(mine),
            "offered_on_the_withdrawing_turn": sum(r["turns"][-5]["offers"] for r in mine),
            "offered_on_any_turn": sum(any(t.get("offers") for t in r["turns"]) for r in mine),
            "cancellation_recited": sum(t["recited"] for r in mine for t in r["turns"] if "recited" in t),
            "refused": sum(t.get("refused", False) for r in mine for t in r["turns"]),
            "offer_heard": sum(t.get("offers", False) and not t.get("refused", False) for r in mine for t in r["turns"]),
            "refused_without_an_offer": sum(t.get("refused", False) and not t["offers"] for r in mine for t in r["turns"]),
            "asked_to_say_it_again": len([t for r in mine for t in r["turns"] if "said_again" in t]),
            "said_it_again": sum(t["said_again"] for r in mine for t in r["turns"] if "said_again" in t),
            "replies_with_the_note": len(noted),
            "note_read_out_by_ngram": sum(t["read_out_ngram"] for t in noted),
            "note_read_out_by_judge": sum(t["read_out_judged"] for t in noted),
            "by_shape": {shape: {"trials": sum(r["shape"] == shape for r in mine),
                                 "offered_on_the_withdrawing_turn": sum(r["turns"][-5]["offers"] for r in mine
                                                                         if r["shape"] == shape)}
                         for shape in args.shapes}}
    (args.out / "withdraw.json").write_text(json.dumps({"summary": summary, "known_offers": known, "rows": rows},
                                                       indent=1), encoding="utf-8")
    print(json.dumps(known, indent=1))
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
