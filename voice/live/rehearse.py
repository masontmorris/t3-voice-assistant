"""Rehearse the T3 call through the real live server, with a scripted T3 in place of the fork environment.

    python -m voice.live.rehearse drive --out DIR [--calls 10] [--port 8771]
    python -m voice.live.rehearse summary --out DIR

The score is threads started, of N calls shaped like the call this lane is fixing. `drive` starts `serve` as a child,
renders each call's turns with the server's own TTS, and sends them with the WAV client (to a null output, so nothing plays), one call
after another on one server. Each call is two chat turns, a long hesitant request, a yes, a question about the thread,
and a yes to the thread being confirmed. The server is the real one (voice/live/server.py: LFM3, the segmenter and
decider, the T3 task service, the call-voice relay, the checks); only the seam it reads T3 through is scripted
(voice/t3/fakes.py), so a launch starts nothing and costs nothing.

Calls 1 to 5 are the visible half, 6 to 10 the first held-out half (A), 11 to 15 a second one (B), written after A had
been looked at for causes, and 16 to 20 a third (C), written after B had been run. Each was written once; fixes are
tuned against the visible half only. Report all of them.

A thread counts as started only when the scripted seam saw a launch whose payload is this call's request (most of its
words are in what the launch carried) in the sandbox project, during this call. The seam knows no thread until a launch
makes it. A line the voice said is judged against the seam's record: "a new coding thread has started" is true only
after such a launch, and a status line is true only when it names the thread this call launched. `summary` reads the
records and says, per call, how far it got: dispatched (the request became a launch act), asked (that question was
spoken), yes read (the answer became a launch act), started (the seam saw this call's launch), status answered, and
where the first break was.

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
        ("Hey you.", "Not too bad, a bit of a slow start."),
        ("Hey, you there?", "Yeah, all good, you?"), ("Good afternoon.", "Afternoon! Busy day so far."),
        ("Hi!", "Doing well, thanks for asking."), ("Hello, hello.", "Not bad at all, how are you?"),
        ("Hey assistant.", "Pretty good, just warming up."),
        ("Morning, are you around?", "Yep, doing alright, you?"), ("Hey hey.", "Pretty decent, can't complain."),
        ("Hello, can you hear me okay?", "Loud and clear, how's it going?"),
        ("Hi again.", "Good thanks, ready when you are."), ("Hey, quick check-in.", "Sure, what's up?"))
THREADS = ("Clean up texture export for the game engine", "Convert audio presets for the editor",
           "Find out why the nightly build is slow", "Fix the dropped spreadsheet columns",
           "Remember window sizes on the settings screen",
           "Reduce noisy logging in the importer", "Rename exported meshes with a script",
           "Find why shader compiles are slow", "Clean up the save slot writer", "Make build notes output markdown",
           "Fix the installer hang at ninety percent", "Tidy the config loader",
           "Find why the dashboard loads slowly", "Fix the typo checker flagging every word",
           "Add dark mode to the settings page",
           "Speed up the level loading screen", "Fix the controller rumble on pause",
           "Clean up the localization export", "Find the memory leak in the particle system",
           "Add a replay mode to the debug menu")
ASKS = ("How is the texture export thread going?", "How's the audio presets thread going?",
        "What is the nightly build thread up to?", "Is the spreadsheet columns thread done yet?",
        "How is the window sizes thread going?",
        "How is the importer logging thread going?", "What is the mesh renaming thread up to?",
        "Is the shader compile thread finished?", "How is the save slot thread going?",
        "How are the build notes going?",
        "How is the installer thread going?", "What's the config loader thread up to?",
        "Is the dashboard thread finished?", "How is the typo checker thread going?",
        "How's the dark mode thread going?",
        "How is the loading screen thread going?", "What's the controller rumble thread up to?",
        "Is the localization thread finished?", "How is the memory leak thread going?",
        "How's the replay mode thread going?")
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
    "generator output markdown, a small project, nothing urgent.",
    "Hi, so, uh, I keep running into this thing where, um, the installer hangs at ninety percent, and I was hoping, "
    "uh, you could start a thread to look into it, like a proper investigation.",
    "Okay, uhm, so we've got this config loader, right, and it's, it's a mess, so, uh, can you spin up a thread to "
    "tidy it, maybe a small project, nothing huge.",
    "Um, right, so, the thing I want, the dashboard is slow to load, and, uhm, I'd like a thread on that, to figure "
    "out where the time goes, if you can.",
    "So, uh, I need a favor, kind of, a thread for fixing the typo checker, the, the one that flags every other "
    "word, could you open that up for me?",
    "Alright, uhm, here's what I'm thinking, a thread to add dark mode to the settings page, a small project, and, "
    "uh, go ahead and kick it off whenever.",
    "Um, okay, so the level loading screen is, like, painfully slow, and I was thinking, uh, could you start a thread "
    "on that, just to see what's going on, nothing fancy?",
    "Right, uhm, there's this bug where the controller keeps rumbling when you pause, and, uh, I'd really like a "
    "thread on that, if you could get that going.",
    "So, uh, I have this, this localization export, and it's kind of a mess, a small project really, and, uhm, would "
    "you open a thread to clean that up for me?",
    "Okay, so, this is a bit of a long one, uhm, we have a memory leak somewhere in the particle system, I think, "
    "and, uh, I need a thread for tracking it down, thorough, but not crazy.",
    "Hey, uh, quick idea, a replay mode, in the debug menu, uhm, so we can scrub through a session, can you start a "
    "thread to build that?")
YES = ("Please do.", "Yes, please do.", "Yeah, go ahead.", "Please do.", "Yes please.",
       "Please do?", "Yeah, go ahead.", "Sure.", "Yes, do it.", "Please do.",
       "Please do.", "Yeah, go ahead.", "Sure.", "Please do?", "Yes, do it.",
       "Please do.", "Yeah, go ahead.", "Sure.", "Please do?", "Yes, do it.")
assert len(CHAT) == len(THREADS) == len(ASKS) == len(REQUESTS) == len(YES)


def turns(call, tail=True):
    """One call's turns as text, in the order the person says them."""
    return [*CHAT[call], REQUESTS[call], YES[call], *((ASKS[call], "Yes.") if tail else ())]


FILLER = frozenset({"uh", "uhm", "um", "so", "okay", "right", "and", "the", "a", "to", "it", "that", "i", "you", "if",
                    "of", "on", "in", "for", "can", "could", "would", "be", "is", "my", "me", "this", "there"})


def words_of(text):
    return {w for w in "".join(c if c.isalnum() or c == "'" else " " for c in (text or "").lower()).split()
            if w not in FILLER}


def overlap(request, payload):
    """The share of `request`'s content words that `payload` carries."""
    want = words_of(request)
    return len(want & words_of(payload)) / len(want) if want else 0.0


class ScriptedSeam(F.FakeSeam):
    """The seam a call reads: one sandbox project, and no thread until a launch makes one. A launch is logged with the
    time it came, its payload, and which call's request that payload is."""

    def __init__(self, log_path: Path):
        super().__init__()
        self.log_path = log_path
        self.launches, self.threads = [], {}

    @property
    def listing(self):
        return T.ThreadList(tuple(T.ThreadSummary(tid, "p1", title, T.ThreadActivity.WORKING, "2026-11-18T10:00:00Z",
                                                  False) for tid, title in self.threads.items()),
                            len(self.threads), False, 200)

    @listing.setter
    def listing(self, _):
        pass

    def read_thread(self, thread_id):
        self.calls.append(("read", thread_id))
        return F.state(thread=thread_id, title=self.threads[thread_id], as_of=210)

    def drain(self, timeout=None):
        time.sleep(min(timeout or 0.5, 0.5))
        return iter(())

    def launch(self, request_id, project_id, instruction):
        self.calls.append(("launch", request_id, project_id, instruction))
        best = max(range(len(REQUESTS)), key=lambda i: overlap(REQUESTS[i], instruction))
        thread = f"t{best + 1}"
        self.threads[thread] = THREADS[best]
        self.launches.append({"at": time.time(), "project": project_id, "instruction": instruction, "call": best,
                              "overlap": round(overlap(REQUESTS[best], instruction), 2), "thread": thread})
        self.log_path.write_text(json.dumps(self.launches), encoding="utf-8")
        return F.launched(thread=thread, run=f"r{best + 1}", sequence=93 + len(self.launches))


def serve(args, rest):
    """The live server with its T3 seam and thread picker replaced."""
    seam = ScriptedSeam(args.out / "seam-calls.json")

    def open_t3(cls, origin, token_file, journal_path, model=None, decider=None, *, writer=None):
        return cls(L.Follow(T3Journal(journal_path), F.Connector(seam)), None, decider, writer=writer)

    server.T3Tasks.open = classmethod(open_t3)
    server.default_model = lambda: None
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


MATCH = 0.75  # How much of the request a launch's payload must carry to be this call's request.


def grade(call_rows, call, launches):
    """How far one call got and where it first broke. `call_rows` are its turns in order, with their T3 entries, `call`
    its index from 0, and `launches` the seam's launches during it. A thread is started only by a launch that carries
    this call's request, and each line the voice said is judged against that record."""
    lines = [e for row in call_rows for e in row["t3"]]
    mine = [x for x in launches if x["call"] == call and x["overlap"] >= MATCH and x["project"] == "p1"]
    started, title = bool(mine), THREADS[call]
    said = [e for e in lines if e["delivery"] == "sent"]
    claims = []
    for e in said:
        kinds, text = set(e["kinds"]), (e["reply"] or "").lower()
        if "launched" in kinds:
            claims.append({"line": e["reply"], "true": started})
        elif {"thread_status", "confirm_thread"} & kinds:
            claims.append({"line": e["reply"], "true": started and title.lower() in text})
    asking = [e for e in lines if ASKING & set(e["kinds"])]
    asked = [e for e in asking if e["delivery"] == "sent"]
    answered = [e for e in lines if ANSWERED & set(e["kinds"])]
    status = any("thread_status" in e["kinds"] and title.lower() in (e["reply"] or "").lower() for e in said) and started
    stages = {"dispatched": bool(asking), "asked": bool(asked), "yes_read": bool(answered), "started": started,
              "launch_overlap": [x["overlap"] for x in launches if x["call"] == call],
              "status_answered": status, "false_claims": [c["line"] for c in claims if not c["true"]]}
    if started:
        cause = None
    elif not asking:
        cause = "request not dispatched as a launch"
    elif not asked:
        cause = ("draft refused" if all(e["draft_problems"] for e in asking) else "relay reply held")
    elif not answered:
        cause = "yes not read"
    elif launches:
        cause = "launch payload is not the request"
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
        during = [x for x in started if window["start"] <= x["at"] <= window["end"] + 5]
        grades[window["call"]] = grade(mine, window["call"] - 1, during)
    return rows, grades, started


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mode", choices=["drive", "serve", "summary"])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--calls", type=int, default=15)
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
        halves = {"visible": [g for c, g in grades.items() if c <= VISIBLE],
                  "held_out_a": [g for c, g in grades.items() if VISIBLE < c <= 2 * VISIBLE],
                  "held_out_b": [g for c, g in grades.items() if 2 * VISIBLE < c <= 3 * VISIBLE],
                  "held_out_c": [g for c, g in grades.items() if c > 3 * VISIBLE]}
        causes = {}
        for g in grades.values():
            causes[g["first_break"] or "started"] = causes.get(g["first_break"] or "started", 0) + 1
        score = {name: {"started": f"{sum(g['started'] for g in gs)} of {len(gs)}",
                        "status_answered": f"{sum(g['status_answered'] for g in gs)} of {len(gs)}",
                        "false_claims": sum(len(g["false_claims"]) for g in gs)} for name, gs in halves.items() if gs}
        print(json.dumps({"score": score, "first_break_causes": causes, "grades": grades, "rows": rows}, indent=1))
    else:
        serve(args, rest) if args.mode == "serve" else run_drive(args)


if __name__ == "__main__":
    main()
