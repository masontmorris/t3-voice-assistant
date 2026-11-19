"""Says every act kind once through the call's own path: the LFM3 writer drafts it, the form and decider checks pass or
refuse the draft, the call's voice session says it, and the reply must be the draft's words.

    python -m voice.t3.eval.act_kinds --out DIR [--port 8096]

One fresh voice session per kind, with the call's persona and relay instructions and no chat before it. The acts carry
values that are not the reader's examples, so the writer cannot copy an example back. It calls `say_acts`, the function
the live turn calls, so what it reports is what a live turn would do with the same draft. Needs the GPU (run it inside
the lease), VOICE_ENV_FILE for the decider key, and writes only under --out. No OpenRouter call is made.
"""

import argparse
import asyncio
import json
import time
from pathlib import Path

from voice.live.server import LIVE_HOME, load_persona
from voice.live.t3_tasks import T3Tasks, Words, update_entry
from voice.prototype.realtime import Server, Session
from voice.prototype.run import FORMAT_LINES, NOTE_PERSONA, T3_RELAY, say_acts, update_session
from voice.t3 import fakes as F
from voice.t3 import lifecycle as L
from voice.t3.acts import Kind, act
from voice.t3.journal import T3Journal
from voice.t3.realtime_writer import RealtimeWriter
from voice.t3.verify import default_decider

REMARK = ("So, uh, I want to get some work going on, uhm, a small project, a tool that converts audio presets into the "
          "format the editor wants, and, uhm, could you start a thread for that and, you know, keep it organized?")
T = "Trim unused icons from the build"
P = "Voice sandbox"
SLOTS = {
    Kind.CONFIRM_LAUNCH: {"project": P},
    Kind.ASK_WHICH: {"of": "project", "candidates": [P, "Voice server"]},
    Kind.NO_PROJECT: {},
    Kind.LAUNCH_PENDING: {"project": P},
    Kind.LAUNCHED: {"project": P},
    Kind.LAUNCH_UNCONFIRMED: {"project": P},
    Kind.NO_THREAD: {"recent": 14},
    Kind.CONFIRM_THREAD: {"thread": T},
    Kind.CONFIRM_TELL: {"thread": T, "message": "also remove the old splash image"},
    Kind.CONFIRM_STOP: {"thread": T},
    Kind.CANCELLED: {"op": "stop", "thread": T},
    Kind.THREAD_STATUS: {"thread": T, "status": "working", "summary": "It says it removed 31 unused icons."},
    Kind.TELL_PENDING: {"thread": T},
    Kind.SENT_FOLLOWUP: {"thread": T},
    Kind.TELL_UNCONFIRMED: {"thread": T},
    Kind.HELD_FOLLOWUP: {"thread": T},
    Kind.DROPPED_FOLLOWUP: {"thread": T},
    Kind.STOPPING: {"thread": T},
    Kind.NOTHING_RUNNING: {"thread": T},
    Kind.STOP_UNCONFIRMED: {"thread": T},
    Kind.RUN_UPDATE: {"thread": T, "status": "queued"},
    Kind.REPLIED: {"thread": T, "status": "running", "reply": "I found 31 icons nothing uses."},
    Kind.NEEDS_YOU: {"thread": T, "need": "file_change", "prompt": "Apply the change to the icon folder?"},
    Kind.NEED_RESOLVED: {"thread": T, "status": "expired"},
    Kind.DONE: {"thread": T, "status": "completed", "summary": "It says the build is 2 MB smaller."},
    Kind.THREAD_DELETED: {"thread": T},
    Kind.LOST_TRACK: {"thread": T},
    Kind.THREAD_GONE: {"thread": T},
    Kind.WATCH_QUEUED: {"thread": T, "following": 6},
    Kind.UNREACHABLE: {},
    Kind.SEAM_DOWN: {"reason": "connection"},
    Kind.REFUSED: {"op": "start", "code": "refused", "reason": "the project is read only"},
    Kind.LEGACY_UPDATE: {"thread": T, "text": f"{T} is waiting for the build to finish."},
}
assert set(SLOTS) == set(Kind)


async def one(url, t3, persona, kind):
    update = L.Update(f"conv:{kind.value}", (act(kind, **SLOTS[kind]),), "completed", "act")
    entry = update_entry(update, "conv_rehearsal")
    session = await Session.open(url, "voice", time.perf_counter)
    log = []
    try:
        await session.wait_for("session.created")
        await update_session(session, {"type": "realtime", "instructions": persona + T3_RELAY + FORMAT_LINES["c"],
                                       "output_modalities": ["text", "audio"]})

        async def send(line, pcm):
            return pcm, time.perf_counter()

        started = time.perf_counter()
        reply, _ = await say_acts(session, 24_000, started, entry, Words(t3, lambda _: update), [],
                                  lambda name, **detail: log.append({"kind": name, **detail}), send_audio=send)
        words = next(e for e in log if e["kind"] == "t3_words")
        return {"kind": kind.value, "draft": words["line"], "draft_problems": words["problems"],
                "draft_error": words["error"], "draft_decider": words["decider"], "reply": reply["text"],
                "status": reply["status"], "delivery": reply["delivery"],
                "first_audio_s": reply["first_audio_after_s"], "seconds": round(time.perf_counter() - started, 2)}
    finally:
        await session.close()


async def drafts(t3, kind, count, remark=True):
    """`count` fresh drafts of one act kind, each from a new update, and how many passed the form and decider checks."""
    outcomes = []
    for _ in range(count):
        update = L.Update(f"conv:{kind.value}", (act(kind, **SLOTS[kind]),), "completed", "act")
        entry = update_entry(update, "conv_rehearsal")
        turns = [{"user": REMARK, "assistant": []}] if remark else []
        rendered = await asyncio.to_thread(Words(t3, lambda _: update).say, entry, turns)
        outcomes.append({"line": rendered.raw, "passed": rendered.line is not None,
                         "problems": rendered.problems or ([rendered.error] if rendered.error else [])})
    return outcomes


async def main_async(args):
    args.out.mkdir(parents=True, exist_ok=True)
    persona, label = load_persona(LIVE_HOME / "prompt.txt", NOTE_PERSONA)
    url = f"ws://127.0.0.1:{args.port}/v1/realtime"
    extra = ("-c", "32768", "-kvu", *(("-ngl", "0", "--no-mmproj-offload") if args.cpu else ()))
    rows = []
    with Server(args.port, args.out / "llama-server.log", extra, 3):
        async with RealtimeWriter(url) as writer:
            follow = L.Follow(T3Journal(args.out / "journal.json"), F.Connector(F.FakeSeam()))
            t3 = T3Tasks(follow, None, default_decider(), writer=writer)
            if args.drafts:
                for kind in (Kind(k) for k in args.kinds) if args.kinds else Kind:
                    outcomes = await drafts(t3, kind, args.drafts)
                    rows.append({"kind": kind.value, "outcomes": outcomes})
                    print(kind.value, f"{sum(o['passed'] for o in outcomes)} of {len(outcomes)} drafts passed", flush=True)
                    for o in outcomes:
                        if not o["passed"]:
                            print("   refused:", o["line"], "|", o["problems"][:1], flush=True)
                (args.out / "drafts.json").write_text(json.dumps(rows, indent=1), encoding="utf-8")
                return
            for kind in (Kind(k) for k in args.kinds) if args.kinds else Kind:
                row = {**await one(url, t3, persona, kind), "persona": label}
                rows.append(row)
                ok = "said" if row["delivery"] == "sent" else "held"
                print(ok, row["kind"], "|", row["draft"], "|", row["reply"], flush=True)
                (args.out / "rows.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    sent = sum(r["delivery"] == "sent" for r in rows)
    print(f"{sent} of {len(rows)} act kinds were said, each as its checked draft")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8096)
    parser.add_argument("--kinds", nargs="+", choices=[k.value for k in Kind], help="only these act kinds")
    parser.add_argument("--drafts", type=int, help="only draft each kind this many times, with no relay")
    parser.add_argument("--cpu", action="store_true", help="smoke test on the CPU: no GPU layers")
    asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    main()
