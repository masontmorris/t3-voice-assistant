"""The truncate patch's rules, checked on the real patched llama-server, and its latency measured.

    python -m voice.research.live.trim_protocol --out DIR --bin PATH [--trials 20]

Each case runs on a fresh realtime session with the shipped note-mode instructions and synthetic speech rendered by
the server's own TTS (the lines of `trim_options`). A case passes on the events the session sends and on the server's
own log lines (`realtime: checkpoint`, `rewound`, `heard text in`), never on what a reply says:

- `off`: a session that did not ask for checkpoints saves none and refuses a truncate.
- `text`: a `text` that is not the start of what the response said, or longer than it, is refused and nothing is
  rewound; the right prefix then is taken.
- `stale`: after a note goes in, a truncate that expects the response to be last is refused and nothing is rewound.
- `caller_turn`: after the caller's next audio is committed, a truncate of the response before it is refused.
- `same_turn`: an answer, a note and an update in one caller turn: truncating the answer takes the note and the
  update with it, and says so.
- `late`: the truncate and the caller's next audio and response are sent at once, without waiting: the truncate's
  answer comes before the audio is committed, and the next checkpoint sits past the rewound point, so the new audio
  went in after the rewind and none of it was taken.
- latency: the time from sending a truncate to its answer, on an idle server and while another session generates a
  long reply, `--trials` each. A truncate on a session whose own reply is still generating is refused by rule (`a
  response is in progress`), so the live server sends it only once the stopped turn has finished.

Records go to DIR/protocol.json; nothing here goes in the repo.
"""

import argparse
import asyncio
import json
import re
import statistics
import time
from pathlib import Path

from voice.prototype.realtime import Server, Session
from voice.prototype.run import FORMAT_LINES, NOTE_PERSONA, last_item, plain_text, update_session
from voice.research.live.trim_options import LINES, render, say

PORT = 8122
SHORT = 120  # tokens and frames in a response a case only needs to exist


async def opened(url, clock, checkpoints=8):
    session = await Session.open(url, "voice", clock)
    await session.wait_for("session.created")
    await update_session(session, {"type": "realtime", "instructions": NOTE_PERSONA + FORMAT_LINES["c"],
                                   "output_modalities": ["text", "audio"], "response_checkpoints": checkpoints})
    return session


async def respond(session, max_tokens=SHORT):
    mark = len(session.events)
    await session.send(type="response.create", response={"max_output_tokens": max_tokens})
    done = await session.wait_for("response.done", "error", after=mark, timeout=300)
    events = session.events[mark:]
    item = next((e["item"]["id"] for e in events if e["type"] == "response.output_item.done"), None)
    text = "".join(e.get("delta", "") for e in events if e["type"] == "response.output_text.delta")
    return item, text, done


async def truncate(session, item, text, expect=None):
    mark = len(session.events)
    started = time.perf_counter()
    await session.send(type="conversation.item.truncate", item_id=item, content_index=0, text=text,
                       expect_last_item_id=last_item(session) if expect is None else expect)
    answer = await session.wait_for("conversation.item.truncated", "error", after=mark, timeout=60)
    return answer, round((time.perf_counter() - started) * 1000, 2)


async def note(session, text):
    mark = len(session.events)
    await session.send(type="conversation.item.create", item=plain_text(text))
    return (await session.wait_for("conversation.item.added", "error", after=mark))["item"]["id"]


def log_lines(log, start):
    return log.read_text(encoding="utf-8", errors="replace")[start:]


def code(answer):
    return answer.get("error", {}).get("type") if answer["type"] == "error" else "truncated"


async def run(args, clips, url, clock, log):
    found = {}
    mark = lambda: len(log.read_text(encoding="utf-8", errors="replace"))

    # off
    start = mark()
    session = await opened(url, clock, checkpoints=0)
    await say(session, clips[0])
    item, text, _ = await respond(session)
    answer, _ = await truncate(session, item, "")
    found["off"] = {"answer": code(answer), "checkpoint_lines": log_lines(log, start).count("realtime: checkpoint"),
                    "pass": code(answer) == "item_truncate_off" and "realtime: checkpoint" not in log_lines(log, start)}
    await session.close()

    # text
    session = await opened(url, clock)
    await say(session, clips[0])
    item, text, _ = await respond(session)
    said = text.lstrip()
    start = mark()
    wrong, _ = await truncate(session, item, "Something it never said")
    longer, _ = await truncate(session, item, said + " and more")
    rewound_before = "rewound" in log_lines(log, start)
    right, _ = await truncate(session, item, " ".join(said.split()[:4]))
    found["text"] = {"wrong": code(wrong), "longer": code(longer), "rewound_on_refusals": rewound_before,
                     "right": code(right),
                     "pass": code(wrong) == code(longer) == "item_truncate_invalid_text" and not rewound_before
                     and code(right) == "truncated"}
    await session.close()

    # stale
    session = await opened(url, clock)
    await say(session, clips[0])
    item, text, _ = await respond(session)
    start = mark()
    await note(session, "[Note for you, not said by the person: a made-up note that arrives after the reply.]")
    stale, _ = await truncate(session, item, "", expect=item)
    found["stale"] = {"answer": code(stale), "rewound": "rewound" in log_lines(log, start),
                      "pass": code(stale) == "item_truncate_stale" and "rewound" not in log_lines(log, start)}
    await session.close()

    # caller_turn
    session = await opened(url, clock)
    await say(session, clips[0])
    item, text, _ = await respond(session)
    await say(session, clips[1])
    committed = len(session.events)
    await session.send(type="input_audio_buffer.commit")
    await session.wait_for("conversation.item.added", after=committed)
    start = mark()
    spoken, _ = await truncate(session, item, "")
    found["caller_turn"] = {"answer": code(spoken), "rewound": "rewound" in log_lines(log, start),
                            "pass": code(spoken) == "item_truncate_caller_turn" and "rewound" not in log_lines(log, start)}
    await session.close()

    # same_turn
    session = await opened(url, clock)
    await say(session, clips[0])
    answer_item, answer_text, _ = await respond(session)
    note_item = await note(session, "[Note for you, not said by the person: the made-up lookup finished. Tell them.]")
    update_item, _, _ = await respond(session)
    kept = " ".join(answer_text.split()[:5])
    same, _ = await truncate(session, answer_item, kept)
    await say(session, clips[3])
    after_item, after_text, after_done = await respond(session)
    found["same_turn"] = {"answer": code(same), "removed": same.get("removed_item_ids"),
                          "expected_removed": [note_item, update_item],
                          "next_reply_status": (after_done.get("response") or {}).get("status"),
                          "pass": code(same) == "truncated" and same.get("removed_item_ids") == [note_item, update_item]
                          and (after_done.get("response") or {}).get("status") == "completed"}
    await session.close()

    # late: nothing waits for the truncate's answer before the caller's next audio and response go out
    session = await opened(url, clock)
    await say(session, clips[0])
    item, text, _ = await respond(session, max_tokens=400)
    start = mark()
    sent_at = len(session.events)
    await session.send(type="conversation.item.truncate", item_id=item, content_index=0, text="",
                       expect_last_item_id=last_item(session))
    await say(session, clips[3])
    await session.send(type="response.create", response={"max_output_tokens": SHORT})
    await session.wait_for("response.done", "error", after=sent_at, timeout=300)
    order = [e["type"] for e in session.events[sent_at:]
             if e["type"] in ("conversation.item.truncated", "error", "input_audio_buffer.committed")]
    lines = log_lines(log, start)
    rewound = [int(m.group(1)) for m in re.finditer(r"rewound checkpoint \d+ from n_past = \d+ to (\d+)", lines)]
    after = [int(m.group(1)) for m in re.finditer(r"checkpoint \d+ at n_past = (\d+)", lines)]
    found["late"] = {"order": order, "rewound_to": rewound, "next_checkpoint_at": after,
                     "pass": order[:2] == ["conversation.item.truncated", "input_audio_buffer.committed"]
                     and len(rewound) == 1 and len(after) == 1 and after[0] > rewound[0]}
    await session.close()

    # latency
    found["latency_ms"] = {"idle": await timings(url, clock, clips, args.trials, None),
                           "beside_a_generating_session": await timings(url, clock, clips, args.trials, True)}
    return found


async def timings(url, clock, clips, trials, loaded):
    """Truncate round trips on one session, each on a fresh response; with `loaded`, while a second session generates
    long replies one after another."""
    stop, load = asyncio.Event(), None
    if loaded:
        other = await opened(url, clock, checkpoints=0)

        async def generate():
            while not stop.is_set():
                await say(other, clips[2])
                await respond(other, max_tokens=2000)

        load = asyncio.create_task(generate())
        await asyncio.sleep(1.0)
    session = await opened(url, clock)
    times, answers = [], []
    for _ in range(trials):
        await say(session, clips[0])
        item, text, _ = await respond(session)
        answer, ms = await truncate(session, item, "")
        times.append(ms)
        answers.append(code(answer))
    await session.close()
    if load is not None:
        stop.set()
        await load
        await other.close()
    return {"n": len(times), "median": statistics.median(times), "max": max(times), "min": min(times),
            "answers": sorted(set(answers))}


def checkpoint_sizes(log):
    sizes = [float(m.group(1)) for m in re.finditer(r"realtime: checkpoint \d+ at n_past = \d+, ([\d.]+) MiB",
                                                     log.read_text(encoding="utf-8", errors="replace"))]
    kept = [float(m.group(1)) for m in re.finditer(r"kept \(([\d.]+) MiB\)", log.read_text(encoding="utf-8",
                                                                                          errors="replace"))]
    return {"n": len(sizes), "mib_min": min(sizes), "mib_max": max(sizes), "kept_mib_max": max(kept)}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--bin", type=Path, required=True)
    parser.add_argument("--trials", type=int, default=20)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()

    def clock():
        return time.perf_counter() - started

    log = args.out / "llama-server.log"
    with Server(PORT, log, ("-c", "32768", "-kvu"), slots=3, binary=args.bin):
        clips = render(args.out / "clips", PORT)
        found = asyncio.run(run(args, clips, f"ws://127.0.0.1:{PORT}/v1/realtime", clock, log))
    found["checkpoints"] = checkpoint_sizes(log)
    (args.out / "protocol.json").write_text(json.dumps(found, indent=1), encoding="utf-8")
    print(json.dumps(found, indent=1))


if __name__ == "__main__":
    main()
