"""Score the call voice's relay of a checked line, with the note wording as the variable. GPU: run inside the lease.

    python -m voice.t3.eval.relay_eval --out DIR --chat WAV --ask WAV [--repeats 6] [--notes old first retry]

Each trial opens a fresh voice session with the live persona, plays a chat turn and its reply, plays a thread
question as audio, and then relays one checked line the way a turn does (`relay_draft`), stopping at the first word
that is not the line's. A trial recites when the reply completed with audio and says the line's words and no others.
The drafts are made up. The report counts recited trials per note wording and lists what the others said.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

from voice.prototype import run
from voice.prototype.realtime import Server, Session, read_pcm16
from voice.t3.acts import Kind, act, recites
from voice.t3.fakes import Agree
from voice.t3.speech import Speech

LFM_ARGS = ("-c", "32768", "-kvu")
LFM_SLOTS = 3
DRAFTS = [
    (act(Kind.CONFIRM_THREAD, thread="Tidy the tide table export"),
     "Do you mean the thread called Tidy the tide table export?"),
    (act(Kind.ASK_WHICH, of="thread", candidates=["Trim the harbour map tiles", "Trim the harbour map labels",
                                                    "Add a harbour map legend"]),
     "Which thread do you mean, Trim the harbour map tiles, Trim the harbour map labels, or Add a harbour map legend?"),
    (act(Kind.THREAD_STATUS, thread="Move the kiln log to its own page", status="completed",
         summary="It reports that the kiln log now has a page."),
     "The Move the kiln log to its own page thread is finished and it reports that the kiln log now has a page."),
    (act(Kind.CONFIRM_LAUNCH, project="Moss Notes"), "Should I start a new coding thread in Moss Notes?"),
    (act(Kind.CONFIRM_STOP, thread="Sort the seed catalogue"), "Should I stop Sort the seed catalogue?"),
    (act(Kind.CONFIRM_TELL, thread="Tidy the tide table export", message="keep the date column first"),
     "Should I send 'keep the date column first' to Tidy the tide table export as a follow-up?"),
]


# The line the 2026-11-20 call refused twice: its writer said "which one they mean", the person's side in the third
# person, and the voice stopped at its first word off the line both times. The same line said to "you", and the example's.
SAME_TITLE = [
    (act(Kind.SAME_TITLE, thread="Trim Harbour Tiles", op="stop"),
     "More than one thread is called Trim Harbour Tiles, so I can't tell which one they mean and did nothing. You can "
     "stop it in the app."),
    (act(Kind.SAME_TITLE, thread="Trim Harbour Tiles", op="stop"),
     "More than one thread is called Trim Harbour Tiles, so I can't tell which one you mean and did nothing. You can "
     "stop it in the app."),
    (act(Kind.SAME_TITLE, thread="Trim Harbour Tiles", op="stop"),
     "More than one thread is called Trim Harbour Tiles, so I can't tell which to stop. You can stop it in the app."),
]


# Other shapes of that line, the writer copying the shape of its example: one that opens on what was done, one that opens
# on "I can't tell", and one that says the two threads share the name.
SAME_TITLE_FORMS = [
    (act(Kind.SAME_TITLE, thread="Trim Harbour Tiles", op="stop"),
     "I didn't stop anything, because more than one thread is called Trim Harbour Tiles. You can stop it in the app."),
    (act(Kind.SAME_TITLE, thread="Trim Harbour Tiles", op="stop"),
     "I can't tell which Trim Harbour Tiles thread to stop, because more than one has that name. You can stop it in "
     "the app."),
    (act(Kind.SAME_TITLE, thread="Trim Harbour Tiles", op="stop"),
     "Two threads share the name Trim Harbour Tiles, so I didn't stop either one. You can stop it in the app."),
]


def checked(acts, line):
    """The line as a RenderedLine, minted by the same `Speech.accept` the call uses, with a decider that agrees: this
    eval scores the voice's saying of a line, not the line."""
    rendered = Speech(None, lambda thread_id: None, Agree()).accept((acts,), line, "eval")
    assert rendered.line is not None, rendered.problems
    return rendered.line


def notes() -> dict:
    """The note wordings under test, by name. `old` closed the note with an instruction after the unquoted words, as
    voice/prototype/run.py did until 2026-11-20; `first` and `retry` are what it sends now."""
    product = run.relay_note
    return {
        "old": lambda text: f"{run.T3_NOTE}{text} (Reply with only these words.)]",
        "first": product,
        "retry": lambda text: product(text, 1),
    }


async def trial(url: str, chat: Path, ask: Path, acts, line: str, relay) -> dict:
    t0 = time.perf_counter()
    clock = lambda: time.perf_counter() - t0
    voice = await Session.open(url, "voice", clock)
    try:
        created = await voice.wait_for("session.created")
        out_rate = created["session"]["audio"]["output"]["format"]["rate"]
        await run.update_session(voice, {"type": "realtime", "output_modalities": ["text", "audio"],
                                         "instructions": run.PERSONA})
        for clip, answered in ((chat, True), (ask, False)):
            pcm, rate, _ = read_pcm16(clip)
            await voice.stream(pcm, rate)
            await voice.send(type="input_audio_buffer.commit")
            if answered:
                await run.respond(voice, out_rate, clock())
        run.relay_note, kept = (lambda text, attempt=0: relay(text)), run.relay_note
        try:
            reply, pcm = await run.relay_draft(voice, out_rate, clock(), checked(acts, line))
        finally:
            run.relay_note = kept
        ok = reply["status"] == "completed" and bool(pcm) and recites(reply["text"], line)
        return {"line": line, "recited": ok, "status": reply["status"], "said": reply["text"], "audio_s": reply["speech_s"]}
    finally:
        await voice.close()


async def main_async(args) -> int:
    args.out.mkdir(parents=True, exist_ok=True)
    wordings = notes()
    chosen = {name: wordings[name] for name in args.notes}
    drafts = {"same_title": SAME_TITLE, "same_title_forms": SAME_TITLE_FORMS}.get(args.set, DRAFTS)
    rows = []
    with Server(args.port, args.out / "lfm3-audio.log", LFM_ARGS, LFM_SLOTS):
        url = f"ws://127.0.0.1:{args.port}/v1/realtime"
        for name, relay in chosen.items():
            for _ in range(args.repeats):
                for acts, line in drafts:
                    row = {"note": name, **await trial(url, args.chat, args.ask, acts, line, relay)}
                    rows.append(row)
                    print(json.dumps({k: row[k] for k in ("note", "recited", "status")} | {"said": row["said"][-70:]}),
                          flush=True)
    summary = {name: f"{sum(r['recited'] for r in rows if r['note'] == name)} of {sum(r['note'] == name for r in rows)}"
               for name in chosen}
    (args.out / "relay.json").write_text(json.dumps({"summary": summary, "rows": rows}, indent=1), encoding="utf-8")
    print(json.dumps(summary, indent=1))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--chat", type=Path, required=True, help="a WAV of a chat turn")
    parser.add_argument("--ask", type=Path, required=True, help="a WAV of a thread question")
    parser.add_argument("--repeats", type=int, default=6)
    parser.add_argument("--port", type=int, default=8095)
    parser.add_argument("--notes", nargs="+", default=["old", "first", "retry"])
    parser.add_argument("--set", choices=["lines", "same_title", "same_title_forms"], default="lines", help="which drafts to relay")
    return asyncio.run(main_async(parser.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
