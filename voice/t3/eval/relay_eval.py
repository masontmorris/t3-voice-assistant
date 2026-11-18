"""Does the call's voice say a checked draft word for word? One row per relay, for each wording of the instructions.

    python -m voice.t3.eval.relay_eval --out DIR [--variants before after] [--scenarios 22] [--port 8097]

Each scenario is a call shaped like the 2026-11-18 one: two short chat turns, then a long hesitant request, as audio in
one realtime session with the call's persona, and then the private note that hands the voice a draft. The reply is
scored with `acts.recites`, the same check the live turn applies before any audio is sent. The context turns are
rendered with the server's own TTS, so no recorded voice is used. Needs the GPU (run it inside the lease) and writes
only under --out.

`before` is the wording that shipped on voice/main dce679c721. `after` is the wording in voice/prototype/run.py.
"""

import argparse
import asyncio
import base64
import io
import json
import time
import wave
from pathlib import Path

from voice.live.script import SHAPES, render, voices
from voice.live.server import LIVE_HOME, load_persona
from voice.prototype.realtime import Server, Session
from voice.prototype.run import FORMAT_LINES, NOTE_PERSONA, T3_NOTE, T3_RELAY, create_item, plain_text, update_session
from voice.t3.acts import Kind, recites
from voice.t3.speech import READER_EXAMPLES, RELAY_EXAMPLES

BEFORE_NOTE = "Private note for you (not said by the person): Draft of what to tell them now: "
BEFORE_RELAY = (
    '\n\nSometimes a private note arrives from a colleague about their '
    'coding threads. A thread is a workspace conversation with a coding agent, separate from this call. '
    'The note holds a draft of what to tell them now. They have not heard it yet. Say the draft to them '
    'now exactly as written, word for word, as "I" to "you". If a note says a reply did not play, '
    'set that draft and reply aside. Do not repeat the news unless a new draft arrives.'
    '\n\nHere are examples of a note and a good reply to it. The threads, projects and numbers in them are only examples.'
    + "".join(f"\n\nNote: Draft of what to tell them now: {line}\nReply: {line}" for line in RELAY_EXAMPLES))

CHAT = (("Hey, how's it going?", "I'm okay, how are you?"), ("Hello there.", "Not bad, just getting set up for the day."),
        ("Hi, can you hear me?", "Great, how has your morning been?"), ("Hey!", "Doing fine, thanks. And you?"),
        ("Good morning.", "Pretty good, a bit tired honestly."))
# Long hesitant requests, by what the person asks for. The act kinds in PLAN are what each one really produces.
REQUESTS = {
    "start": (
        "Uhm, I was wondering if you could start a thread to get work going on, uhm, a little project to make, uhm, "
        "textures export into the game engine cleanly? Could you start that thread like a project manager?",
        "So, uh, I want to get some work going on, uhm, a small tool that converts audio presets into the format the "
        "editor wants, and, uhm, could you start a thread for that and, you know, keep it organized?",
        "Okay, so, uhm, I was thinking, uh, maybe we could have a thread that works out why the nightly build is so "
        "slow, sort of like a detective, if that makes sense? Can you kick that off for me?",
        "Right, uhm, here's the thing, there's this spreadsheet export that keeps dropping columns, and, uh, I would "
        "love a thread on that, a little investigation, whenever you get a chance, no rush, but, yeah, please.",
        "Uh, so, quick one, well, not that quick, uhm, I need some work started on the, the settings screen, the part "
        "that remembers window sizes, and, uh, could you open a thread for it and, you know, sort it out?"),
    "ask": (
        "Uhm, so I was wondering, like, how is that, uh, the thread about the, uhm, the export cleanup thing, how's "
        "that going, do you know?",
        "Okay, so, uh, the thread that was looking at the slow build, uhm, what's it up to, is it done or is it "
        "still, you know, working on it?"),
    "tell": (
        "Uh, can you tell the, uhm, the export cleanup thread to also, uh, add a little test for it, if that's okay, "
        "when you get a chance?",
        "So, uhm, I'd like to pass something on to the settings thread, uh, tell it to keep the window sizes per "
        "monitor, if it can, yeah?"),
    "stop": (
        "Hmm, actually, uhm, the thread that's working on the build speed thing, can you stop that for now, I think "
        "we're, uh, going in the wrong direction?",
        "Uh, okay, so, the one on the spreadsheet columns, uhm, I think you can halt that, it's, uh, not what I "
        "meant at all."),
    "idle": ("",)}
# What the person asked for, and the act kind the voice then has to say. "idle" is news with no request.
PLAN = (("start", Kind.CONFIRM_LAUNCH), ("start", Kind.NO_PROJECT), ("start", Kind.ASK_WHICH),
        ("start", Kind.CONFIRM_LAUNCH), ("start", Kind.LAUNCH_PENDING), ("start", Kind.LAUNCHED),
        ("start", Kind.LAUNCH_UNCONFIRMED), ("start", Kind.REFUSED), ("start", Kind.CANCELLED),
        ("ask", Kind.THREAD_STATUS), ("ask", Kind.NO_THREAD), ("ask", Kind.CONFIRM_THREAD),
        ("tell", Kind.CONFIRM_TELL), ("tell", Kind.TELL_PENDING), ("tell", Kind.HELD_FOLLOWUP),
        ("tell", Kind.SENT_FOLLOWUP), ("stop", Kind.CONFIRM_STOP), ("stop", Kind.STOPPING),
        ("stop", Kind.STOP_UNCONFIRMED), ("idle", Kind.DONE), ("idle", Kind.NEEDS_YOU), ("idle", Kind.REPLIED))
VARIANTS = {"before": (BEFORE_RELAY, BEFORE_NOTE, ""), "after": (T3_RELAY, T3_NOTE, "]")}


def render_turns(server, texts):
    """Each text as (PCM16, rate), in the server's own TTS voice."""
    _, voice, _ = render(server, texts[0], voices(server) or [""], SHAPES)
    out = {}
    for text in texts:
        _, _, blob = render(server, text, [voice], SHAPES)
        if blob[:4] == b"RIFF":
            with wave.open(io.BytesIO(blob)) as wav:
                out[text] = wav.readframes(wav.getnframes()), wav.getframerate()
        else:
            out[text] = blob, 24_000
    return out


async def say(session, pcm, rate, reply=True):
    """One person turn as audio, then the voice session's reply to it. The last turn has none: in the call, the
    private note is the next thing the voice session reads, and its reply is the turn's only one."""
    step = rate * 100 // 1000 * 2
    for offset in range(0, len(pcm), step):
        await session.send(type="input_audio_buffer.append", audio=base64.b64encode(pcm[offset:offset + step]).decode())
    await session.send(type="input_audio_buffer.commit")
    if not reply:
        return "", None
    mark = len(session.events)
    await session.send(type="response.create")
    done = await session.wait_for("response.done", "error", after=mark, timeout=60)
    return "".join(e.get("delta", "") for e in session.events[mark:] if e["type"] == "response.output_text.delta"), done


def scenarios(count):
    """The call shapes: two chat turns, then a long request when the person asked for something, and the draft the
    act kind for that request produces. Each request wording is used in turn."""
    found, used = [], {shape: 0 for shape in REQUESTS}
    for index, (shape, kind) in enumerate(PLAN[:count]):
        wordings = REQUESTS[shape]
        request = wordings[used[shape] % len(wordings)]
        used[shape] += 1
        chat = CHAT[index % len(CHAT)]
        found.append({"shape": shape, "kind": kind.value, "context": (*chat, *((request,) if request else ())),
                      "draft": READER_EXAMPLES[kind][1]})
    return found


async def one(url, rendered, name, scenario, persona, relay, note, close):
    context, draft = scenario["context"], scenario["draft"]
    started = time.perf_counter()
    session = await Session.open(url, "eval", time.perf_counter)
    try:
        await session.wait_for("session.created")
        first = rendered[context[0]][1]
        config = {"type": "realtime", "instructions": persona + relay + FORMAT_LINES["c"],
                  "output_modalities": ["text", "audio"], "audio": {"input": {"format": {"type": "audio/pcm", "rate": first}}}}
        await update_session(session, config)
        replies = []
        for position, text in enumerate(context):
            pcm, rate = rendered[text]
            reply, _ = await say(session, pcm, rate, reply=position < len(context) - 1 or scenario["shape"] == "idle")
            replies.append(reply)
        item = await create_item(session, plain_text(f"{note}{draft}{close}"))
        mark = len(session.events)
        sent = time.perf_counter()
        await session.send(type="response.create")
        await session.wait_for("response.done", "error", after=mark, timeout=60)
        events = session.events[mark:]
        text = "".join(e.get("delta", "") for e in events if e["type"] == "response.output_text.delta")
        audio = next((e for e in events if e["type"] == "response.output_audio.delta"), None)
        return {"variant": name, "shape": scenario["shape"], "kind": scenario["kind"], "draft": draft,
                "context": list(context), "context_replies": replies,
                "reply": text, "same_words": recites(text, draft), "item": item["accepted"],
                "first_audio_s": None if audio is None else round(audio["_t"] - sent, 3),
                "seconds": round(time.perf_counter() - started, 2)}
    finally:
        await session.close()


async def main_async(args):
    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    plan = scenarios(args.scenarios)
    persona, persona_label = load_persona(LIVE_HOME / "prompt.txt", NOTE_PERSONA)
    extra = ("-c", "32768", *(("-ngl", "0", "--no-mmproj-offload") if args.cpu else ()))
    with Server(args.port, out / "llama-server.log", extra, 2):
        server = f"http://127.0.0.1:{args.port}"
        texts = sorted({text for scenario in plan for text in scenario["context"]})
        rendered = render_turns(server, texts)
        rows = []
        for name in args.variants:
            relay, note, close = VARIANTS[name]
            for scenario in plan:
                row = await one(f"ws://127.0.0.1:{args.port}/v1/realtime", rendered, name, scenario, persona, relay, note,
                                close)
                rows.append({**row, "persona": persona_label})
                print(name, row["same_words"], repr(row["reply"][:90]), flush=True)
                (out / "rows.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    for name in args.variants:
        mine = [r for r in rows if r["variant"] == name]
        print(f"{name}: {sum(r['same_words'] for r in mine)} of {len(mine)} say the draft's words and no others")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--variants", nargs="+", choices=sorted(VARIANTS), default=["before", "after"])
    parser.add_argument("--scenarios", type=int, default=len(PLAN))
    parser.add_argument("--port", type=int, default=8097)
    parser.add_argument("--cpu", action="store_true", help="smoke test the script on the CPU, no GPU layers")
    asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    main()
