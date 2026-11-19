"""Rehearse the T3 call through the real live server, with a scripted T3 in place of the fork environment.

    python -m voice.live.rehearse drive --out DIR [--calls 10] [--port 8771]
    python -m voice.live.rehearse summary --out DIR

The score is threads started, of N calls shaped like Mason's. `drive` starts `serve` as a child, renders each call's
turns with the server's own TTS, and sends them with the WAV client (to a null output, so nothing plays), one call
after another on one server. Each call is two chat turns, a long hesitant request, a yes, a question about the thread,
and a yes to the thread being confirmed. The server is the real one (voice/live/server.py: LFM3, the segmenter and
decider, the T3 task service, the call-voice relay, the checks); only the seam it reads T3 through is scripted
(voice/t3/fakes.py), so a launch starts nothing and costs nothing.

Calls 1 to 5 are the visible half and calls 6 to 10 the held-out half. Both were written once, and fixes are tuned
against the visible half only; report both. `summary` reads the records and says, per call, how far it got:
dispatched (the request became a launch act), asked (that question was spoken), yes read (the answer became a launch
act), started (the seam saw a launch), and where the first break was.

Needs the GPU (run it inside the lease), VOICE_ENV_FILE for the decider key, and writes only under --out. The turns
are synthetic speech; the thread picker's cloud model and the general agent are off, so no OpenRouter call is made.
"""

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from voice.live import server
from voice.t3 import fakes as F
from voice.t3 import lifecycle as L
from voice.t3 import types as T
from voice.t3.journal import T3Journal

VOICE = Path(os.environ.get("VOICE_HOME", Path.home() / "voice"))
VISIBLE = 5
CHAT = (("Hey there, how are things?", "Pretty good, how about you?"),
        ("Hello there.", "Not bad, just getting set up for the day."),
        ("Hi, can you hear me?", "Great, how has your morning been?"), ("Hey!", "Doing fine, thanks. And you?"),
        ("Good morning.", "Pretty good, a bit tired honestly."),
        ("Morning!", "Pretty well, thanks, and you?"), ("Hi there.", "Can't complain, how's your day going?"),
        ("Hey, are you there?", "Good, I'm just checking in."), ("Hello again.", "Fine, thanks for asking."),
        ("Hey you.", "Not too bad, a bit of a slow start."))
THREADS = ("Clean up texture export for the game engine", "Convert audio presets for the editor",
           "Find out why the nightly build is slow", "Fix the dropped spreadsheet columns",
           "Remember window sizes on the settings screen",
           "Reduce noisy logging in the importer", "Rename exported meshes with a script",
           "Find why shader compiles are slow", "Clean up the save slot writer", "Make build notes output markdown")
ASKS = ("How is the texture export thread going?", "How's the audio presets thread going?",
        "What is the nightly build thread up to?", "Is the spreadsheet columns thread done yet?",
        "How is the window sizes thread going?",
        "How is the importer logging thread going?", "What is the mesh renaming thread up to?",
        "Is the shader compile thread finished?", "How is the save slot thread going?",
        "How are the build notes going?")
REQUESTS = (
    "Uhm, I was wondering if you could start a thread to get work going on, uhm, a little project to make, uhm, "
    "textures export into the game engine cleanly? Can you kick that off and keep it tidy?",
    "So, uh, I want to get some work going on, uhm, a small project, a tool that converts audio presets into the "
    "format the editor wants, and, uhm, could you start a thread for that and, you know, keep it organized?",
    "Okay, so, uhm, I was thinking, uh, maybe we could have a thread that works out why the nightly build is so "
    "slow, sort of like a detective, a little project, if that makes sense? Can you kick that off for me?",
    "Right, uhm, here's the thing, there's this spreadsheet export that keeps dropping columns, and, uh, I would "
    "love a thread on that, a little investigation project, whenever you get a chance, no rush, but, yeah, please.",
    "Uh, so, quick one, well, not that quick, uhm, I need some work started on the, the settings screen, the part "
    "that remembers window sizes, a small project really, and, uh, could you open a thread for it and sort it out?",
    "Um, so I've been meaning to, uh, get somebody on the, the logging stuff in the importer, because it's, like, "
    "really noisy right now, and, uhm, would you be able to open a thread for that and, you know, drive it?",
    "Okay, so, uh, there's this little side project, uhm, a script that renames the exported meshes, and I'd love, "
    "uh, a thread started on it, if that's not too much trouble.",
    "Right, so, uhm, how do I put this, the shader compile step takes forever, and, uh, I want a thread to dig into "
    "why, sort of a, a mini project, can you start that?",
    "Uh, hey, so, quick thing, well, a few things, uhm, I need the save system cleaned up, the, the part that writes "
    "slots, and could you, uh, spin up a thread for it please?",
    "So, uhm, this might sound odd, but I'd like to, uh, kick off a thread about, uh, making the build notes "
    "generator output markdown, a small project, nothing urgent.")
YES = ("Please do.", "Yes, please do.", "Yeah, go ahead.", "Please do.", "Yes please.",
       "Please do?", "Yeah, go ahead.", "Sure.", "Yes, do it.", "Please do.")
assert len(CHAT) == len(THREADS) == len(ASKS) == len(REQUESTS) == len(YES)


def turns(call, tail=True):
    """One call's turns as text, in the order the person says them."""
    return [*CHAT[call], REQUESTS[call], YES[call], *((ASKS[call], "Yes.") if tail else ())]


class ScriptedSeam(F.FakeSeam):
    """The seam a call reads: one sandbox project, a thread per request the calls make, and reads of them. Each launch
    is logged with the time it came."""

    def __init__(self, log_path: Path):
        super().__init__()
        self.log_path = log_path
        for call in range(len(THREADS)):
            self.answer("launch", F.launched(thread=f"t{call + 1}", run=f"r{call + 1}", sequence=93 + call))
        self.listing = T.ThreadList(tuple(
            T.ThreadSummary(f"t{i + 1}", "p1", title, T.ThreadActivity.WORKING, "2026-11-18T10:00:00Z", False)
            for i, title in enumerate(THREADS)), len(THREADS), False, 200)
        self.launches = []

    def read_thread(self, thread_id):
        self.calls.append(("read", thread_id))
        title = THREADS[int(thread_id[1:]) - 1]
        return F.state(thread=thread_id, title=title, as_of=210)

    def drain(self, timeout=None):
        time.sleep(min(timeout or 0.5, 0.5))
        return iter(())

    def launch(self, request_id, project_id, instruction):
        result = super().launch(request_id, project_id, instruction)
        self.launches.append({"at": time.time(), "project": project_id, "instruction": instruction})
        self.log_path.write_text(json.dumps(self.launches), encoding="utf-8")
        return result


def serve(args, rest):
    """The live server with its T3 seam and thread picker replaced."""
    seam = ScriptedSeam(args.out / "seam-calls.json")

    def open_t3(cls, origin, token_file, journal_path, model=None, decider=None, *, writer=None):
        return cls(L.Follow(T3Journal(journal_path), F.Connector(seam)), None, decider, writer=writer)

    server.T3Tasks.open = classmethod(open_t3)
    server.default_model = lambda: None
    handler = server.Live.handler

    async def one_call(live, ws):
        """Each call starts with no news, question or recent thread left from the one before it."""
        t3 = live.t3
        with t3.lock:
            t3.follow.outbox = []
            t3.follow.journal.pending_lines.clear()
            t3.follow.journal.save()
            t3.question = t3.unasked = t3.recent = None
            t3.just_held = False
        return await handler(live, ws)

    server.Live.handler = one_call
    if args.cpu:
        real = server.Server
        server.Server = lambda port, log, extra, slots: real(port, log, (*extra, "-ngl", "0", "--no-mmproj-offload"), slots)
    sys.argv = ["voice.live.server", *rest]
    server.main()


def run_drive(args):
    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, VOICE_T3_JOURNAL=str(out / "t3-journal.json"), VOICE_AGENT="off", PYTHONIOENCODING="utf-8")
    stop = out / "STOP"
    command = [sys.executable, "-u", "-m", "voice.live.rehearse", "serve", "--out", str(out),
               *(["--cpu"] if args.cpu else []), "--",
               "--host", "127.0.0.1", "--port", str(args.port), "--t3", "on", "--snapshot", "note",
               "--max-minutes", str(args.max_minutes), "--out", str(out / "server"), "--stop-file", str(stop),
               "--decider-cache", str(out / "decider-cache")]
    child = subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                             encoding="utf-8")
    ready, lines = threading.Event(), []

    def read():
        with (out / "server.out").open("w", encoding="utf-8", buffering=1) as log:
            for line in child.stdout:
                log.write(line)
                lines.append(line)
                if line.startswith("live server on "):
                    ready.set()
        ready.set()

    threading.Thread(target=read, daemon=True).start()
    windows = []
    try:
        if not ready.wait(180) or not any(line.startswith("live server on ") for line in lines):
            raise SystemExit("the live server did not come up, see server.out")
        client = VOICE / "venv-live-client/Scripts/python.exe"
        for call in args.only or range(args.calls):
            text = out / f"call{call + 1}.txt"
            text.write_text("\n".join(turns(call, not args.no_tail)) + "\n", encoding="utf-8")
            clips = out / f"clips{call + 1}"
            subprocess.run([sys.executable, "-m", "voice.live.script", "--turns", str(text), "--out", str(clips),
                            "--server", f"http://127.0.0.1:{args.lfm_port}"], env=env, check=True,
                           stdout=(out / f"clips{call + 1}.out").open("w"), stderr=subprocess.STDOUT)
            wavs = sorted(clips.glob("turn_*.wav"))
            started = time.time()
            subprocess.run([str(client), "-m", "voice.live.client", "--server", f"ws://127.0.0.1:{args.port}",
                            "--secret-file", str(VOICE / "live/secret"), "--log-dir", str(out / f"client{call + 1}"),
                            "--output", "null", "--wav", *map(str, wavs)], env=env, check=True, timeout=240,
                           stdout=(out / f"client{call + 1}.out").open("w"), stderr=subprocess.STDOUT)
            windows.append({"call": call + 1, "start": started, "end": time.time()})
            (out / "windows.json").write_text(json.dumps(windows), encoding="utf-8")
            print(f"call {call + 1} done in {time.time() - started:.0f} s", flush=True)
    finally:
        stop.touch()
        try:
            child.wait(timeout=30)
        except subprocess.TimeoutExpired:
            child.terminate()
            child.wait(timeout=15)


ASKING = {"confirm_launch", "ask_which", "no_project"}
ANSWERED = {"launched", "launch_pending", "launch_unconfirmed", "refused", "cancelled"}


def entries(record):
    """The T3 lines a turn tried to say: the acts' kinds, whether the draft passed, and how the call's reply went."""
    found, current = [], None
    for event in record.get("timeline", []):
        if event["kind"] == "t3_words":
            kinds = [a["kind"] for a in event.get("acts") or ()]
            current = {"kinds": kinds, "draft": event.get("line"), "draft_problems": event.get("problems") or
                       ([event["error"]] if event.get("error") else []), "reply": None, "status": None,
                       "delivery": "unsent"}
            found.append(current)
        elif event["kind"] == "t3_call_reply" and current is not None:
            current["reply"], current["status"] = event.get("text"), event.get("status")
        elif event["kind"] == "t3_delivery" and current is not None:
            current["delivery"] = event.get("delivery")
    return found


def grade(call_rows, started):
    """How far one call got and where it first broke. `call_rows` are its turns in order, with their T3 entries."""
    lines = [e for row in call_rows for e in row["t3"]]
    asking = [e for e in lines if ASKING & set(e["kinds"])]
    asked = [e for e in asking if e["delivery"] == "sent"]
    answered = [e for e in lines if ANSWERED & set(e["kinds"])]
    stages = {"dispatched": bool(asking), "asked": bool(asked), "yes_read": bool(answered), "started": started}
    if started:
        cause = None
    elif not asking:
        cause = "request not dispatched as a launch"
    elif not asked:
        cause = ("draft refused" if all(e["draft_problems"] for e in asking) else "relay reply held")
    elif not answered:
        cause = "yes not read"
    else:
        cause = "launch not performed"
    return {**stages, "first_break": cause}


def summarize(out):
    """One row per turn, one grade per call. Calls split where the ledger's turn count starts over, and the seam's
    launches are matched to a call by the time they came."""
    rows, call = [], 0
    for path in sorted((out / "server").glob("turn_*.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        live = record.get("live") or {}
        number = (live.get("call") or {}).get("turn", 0)
        call += number == 1
        replies = [(r.get("kind"), r.get("delivery"), r.get("text")) for r in record.get("replies", [])]
        rows.append({"call": call, "turn": number, "idle": bool(record.get("idle")), "said": record.get("final_transcript"),
                     "replies": replies, "first_audio_after_speech_end_s": live.get("speech_end_to_first_audio_sent_s"),
                     "held": [r for r in replies if r[1] not in (None, "sent")], "t3": entries(record),
                     "fact_checks": sum(e.get("kind") == "fact_check" for e in record.get("timeline", [])),
                     "error": record.get("error"), "path": path.name})
    launches = out / "seam-calls.json"
    started = json.loads(launches.read_text(encoding="utf-8")) if launches.exists() else []
    windows = json.loads((out / "windows.json").read_text(encoding="utf-8")) if (out / "windows.json").exists() else []
    grades = {}
    for order, window in enumerate(windows, 1):
        mine = [r for r in rows if r["call"] == order]
        hit = any(window["start"] <= s["at"] <= window["end"] + 5 for s in started)
        grades[window["call"]] = grade(mine, hit)
    return rows, grades, started


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mode", choices=["drive", "serve", "summary"])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--calls", type=int, default=10)
    parser.add_argument("--only", type=int, nargs="+", help="call indexes from 0, for a smoke test")
    parser.add_argument("--no-tail", action="store_true", help="leave out the status question and its yes")
    parser.add_argument("--port", type=int, default=8771)
    parser.add_argument("--lfm-port", type=int, default=8095)
    parser.add_argument("--max-minutes", type=float, default=13.5)
    parser.add_argument("--cpu", action="store_true", help="smoke test on the CPU: no GPU layers")
    argv = sys.argv[1:]
    head, rest = (argv[:argv.index("--")], argv[argv.index("--") + 1:]) if "--" in argv else (argv, [])
    args = parser.parse_args(head)
    if args.mode == "summary":
        rows, grades, started = summarize(args.out)
        visible = [g for c, g in grades.items() if c <= VISIBLE]
        held = [g for c, g in grades.items() if c > VISIBLE]
        causes = {}
        for g in grades.values():
            causes[g["first_break"] or "started"] = causes.get(g["first_break"] or "started", 0) + 1
        print(json.dumps({"rows": rows, "grades": grades, "first_break_causes": causes,
                          "started_visible": f"{sum(g['started'] for g in visible)} of {len(visible)}",
                          "started_held_out": f"{sum(g['started'] for g in held)} of {len(held)}"}, indent=1))
    else:
        serve(args, rest) if args.mode == "serve" else run_drive(args)


if __name__ == "__main__":
    main()
