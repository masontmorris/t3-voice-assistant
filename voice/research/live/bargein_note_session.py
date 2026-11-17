"""The note after a barge-in measured on the voice session alone, with and without it.

    python -m voice.research.live.bargein_note_session --note off --label control
    python -m voice.research.live.bargein_note_session --note on --label note

The live-server A/B on the same recordings is in `bargein_note_ab.py`. It runs the whole pipeline, so the decider
and the mock task service put their own text in front of the model and swamp the thing being measured. This harness
leaves only the voice session's history, which is what the note is about.

For each of the nine recorded interruptions, one LFM3 session with the shipped note-mode instructions: the synthetic
lighthouse request, its reply, then, with --note on, the shipped note (`interrupted_note`) saying the person
interrupted and heard only the reply's start, then the interruption clip. The heard part is the reply's first six
words: the live run at 1500 ms into the reply quoted one to six words (measured,
`evidence/live/bargein-note/note/score.json`). It is recorded per case, not sent.

The score reports, per case, whether the second reply reuses the first: the longest run of shared words, and whether
it still names a lighthouse. The reply texts stay in the records for a reader. Recordings and weights stay under
$VOICE_HOME; nothing here is copied into the repo.
"""

import argparse
import asyncio
import base64
import json
import time

import numpy as np

from voice.prototype.realtime import MODEL_RATE, VOICE_HOME, Server, Session, read_pcm16
from voice.prototype.run import (FORMAT_LINES, NOTE_PERSONA, create_item, interrupted_note, plain_text,
                                 update_session)
from voice.research.live.bargein_note_ab import INTERRUPTIONS, PROMPT, SHARED_WORDS, longest_run, words

HEARD_WORDS = 6
PORT = 8118
CHUNK_BYTES = 3200
REPLY_TIMEOUT = 300


def at_model_rate(path):
    """The clip's PCM at the session's input rate, as the live client's `read_wav_16k` resamples it."""
    pcm, rate, _ = read_pcm16(path)
    if rate == MODEL_RATE:
        return pcm
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
    positions = np.arange(0, len(samples), rate / MODEL_RATE)
    return np.interp(positions, np.arange(len(samples)), samples).astype("<i2").tobytes()


async def say(session, pcm):
    for offset in range(0, len(pcm), CHUNK_BYTES):
        await session.send(type="input_audio_buffer.append",
                           audio=base64.b64encode(pcm[offset:offset + CHUNK_BYTES]).decode())
    await session.send(type="input_audio_buffer.commit")


async def answer(session):
    """One response, as the text it said."""
    mark = len(session.events)
    await session.send(type="response.create")
    done = await session.wait_for("response.done", "error", after=mark, timeout=REPLY_TIMEOUT)
    if done.get("type") == "error":
        raise RuntimeError(done.get("error"))
    return "".join(event.get("delta", "") for event in session.events[mark:]
                   if event.get("type") == "response.output_text.delta").strip()


async def one_case(url, clock, prompt, interruption, note_on):
    session = await Session.open(url, "voice", clock)
    try:
        await session.wait_for("session.created")
        await update_session(session, {"type": "realtime", "instructions": NOTE_PERSONA + FORMAT_LINES["c"],
                                       "output_modalities": ["text", "audio"]})
        await say(session, prompt)
        pause = await answer(session)
        heard = " ".join(words(pause)[:HEARD_WORDS])
        note = interrupted_note() if note_on and heard else None
        if note is not None:
            await create_item(session, plain_text(note))
        await say(session, interruption)
        reply = await answer(session)
        return {"heard": heard, "note": note, "interrupted_reply": pause, "answer_reply": reply,
                "at_s": round(clock(), 1)}
    finally:
        await session.close()


def score(case):
    shared = longest_run(words(case["interrupted_reply"]), words(case["answer_reply"]))
    lighthouse = any(word.startswith("lighthouse") for word in words(case["answer_reply"]))
    return {**case, "shared_run": shared, "lighthouse": lighthouse,
            "reused_old_answer": shared >= SHARED_WORDS or lighthouse}


async def run(args, out, log):
    prompt = at_model_rate(PROMPT)
    clips = [(at_model_rate(path), path) for path in INTERRUPTIONS]
    started = time.perf_counter()

    def clock():
        return time.perf_counter() - started

    with Server(PORT, out / "llama-server.log", ("-c", "32768", "-kvu"), slots=1) as server:
        note(log, f"llama-server pid {server.proc.pid} healthy after {server.startup_s:.1f} s")
        url = f"ws://127.0.0.1:{PORT}/v1/realtime"
        cases = []
        for pcm, path in clips:
            case = await one_case(url, clock, prompt, pcm, args.note == "on")
            cases.append(score({**case, "clip": str(path.relative_to(VOICE_HOME))}))
            print(json.dumps({key: cases[-1][key] for key in ("clip", "shared_run", "lighthouse")}), flush=True)
    report = {"note": args.note, "cases": cases, "reused_old_answer": sum(c["reused_old_answer"] for c in cases),
              "n": len(cases)}
    (out / "score.json").write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
    return report


def note(path, line):
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--note", choices=["on", "off"], required=True)
    parser.add_argument("--label", required=True)
    args = parser.parse_args()
    out = VOICE_HOME / "evidence" / "live" / "bargein-note-session" / args.label
    out.mkdir(parents=True, exist_ok=True)
    log = out / "run.log"
    note(log, f"# started {time.strftime('%Y-%m-%d %H:%M:%S')} label {args.label}")
    note(log, f"# difference: --barge-in-note {args.note} on the session alone, 9 recorded interruptions after 6 heard "
              "words, shipped note text")
    note(log, f"# expects to learn: whether the reply to an interruption reuses the interrupted answer with the note "
              f"{args.note}")
    print(json.dumps(asyncio.run(run(args, out, log)), indent=1))


if __name__ == "__main__":
    main()
