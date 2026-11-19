"""Rehearse the T3 call through the real live server, with a scripted T3 in place of the fork environment.

    python -m voice.live.rehearse drive --out DIR [--calls 5] [--port 8771]

`drive` starts `serve` as a child, renders each call's turns with the server's own TTS, and sends them with the WAV
client, one call after another on one server. Each call is two chat turns, a long hesitant request that names no
project, "please do", a question about the thread it started, and a yes to the thread being confirmed. The server is
the real one (voice/live/server.py: LFM3, the segmenter and decider, the T3 task service, the call-voice relay, the
checks); only the seam it reads T3 through is scripted (voice/t3/fakes.py), so a launch starts nothing and costs
nothing. Needs the GPU (run it inside the lease), VOICE_ENV_FILE for the decider key, and writes only under --out.
The turns are synthetic speech; the thread picker's cloud model and the general agent are off, so no OpenRouter call
is made.
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
CHAT = (("Hey, how's it going?", "I'm okay, how are you?"), ("Hello there.", "Not bad, just getting set up for the day."),
        ("Hi, can you hear me?", "Great, how has your morning been?"), ("Hey!", "Doing fine, thanks. And you?"),
        ("Good morning.", "Pretty good, a bit tired honestly."))
THREADS = ("Clean up texture export for the game engine", "Convert audio presets for the editor",
           "Find out why the nightly build is slow", "Fix the dropped spreadsheet columns",
           "Remember window sizes on the settings screen")
ASKS = ("How is the texture export thread going?", "How's the audio presets thread going?",
        "What is the nightly build thread up to?", "Is the spreadsheet columns thread done yet?",
        "How is the window sizes thread going?")
REQUESTS = (
    "Uhm, I was wondering if you could start a thread to get work going on, uhm, a little project to make, uhm, "
    "textures export into the game engine cleanly? Could you start that thread like a project manager?",
    "So, uh, I want to get some work going on, uhm, a small project, a tool that converts audio presets into the "
    "format the editor wants, and, uhm, could you start a thread for that and, you know, keep it organized?",
    "Okay, so, uhm, I was thinking, uh, maybe we could have a thread that works out why the nightly build is so "
    "slow, sort of like a detective, a little project, if that makes sense? Can you kick that off for me?",
    "Right, uhm, here's the thing, there's this spreadsheet export that keeps dropping columns, and, uh, I would "
    "love a thread on that, a little investigation project, whenever you get a chance, no rush, but, yeah, please.",
    "Uh, so, quick one, well, not that quick, uhm, I need some work started on the, the settings screen, the part "
    "that remembers window sizes, a small project really, and, uh, could you open a thread for it and sort it out?")
YES = ("Please do.", "Yes, please do.", "Yeah, go ahead.", "Please do.", "Yes please.")


def turns(call):
    """One call's turns as text, in the order the person says them."""
    chat = CHAT[call % len(CHAT)]
    return [*chat, REQUESTS[call % len(REQUESTS)], YES[call % len(YES)], ASKS[call % len(ASKS)], "Yes."]


class ScriptedSeam(F.FakeSeam):
    """The seam a call reads: one sandbox project, a thread per request the calls make, and reads of them."""

    def __init__(self, log_path: Path):
        super().__init__()
        self.log_path = log_path
        for call in range(len(THREADS)):
            self.answer("launch", F.launched(thread=f"t{call + 1}", run=f"r{call + 1}", sequence=93 + call))
        self.listing = T.ThreadList(tuple(
            T.ThreadSummary(f"t{i + 1}", "p1", title, T.ThreadActivity.WORKING, "2026-11-18T10:00:00Z", False)
            for i, title in enumerate(THREADS)), len(THREADS), False, 200)

    def read_thread(self, thread_id):
        self.calls.append(("read", thread_id))
        title = THREADS[int(thread_id[1:]) - 1]
        return F.state(thread=thread_id, title=title, as_of=210)

    def drain(self, timeout=None):
        time.sleep(min(timeout or 0.5, 0.5))
        return iter(())

    def _record(self):
        self.log_path.write_text(json.dumps([list(call) for call in self.calls]), encoding="utf-8")

    def launch(self, *args):
        result = super().launch(*args)
        self._record()
        return result


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
    try:
        if not ready.wait(180) or not any(line.startswith("live server on ") for line in lines):
            raise SystemExit("the live server did not come up, see server.out")
        client = VOICE / "venv-live-client/Scripts/python.exe"
        for call in range(args.calls):
            text = out / f"call{call + 1}.txt"
            text.write_text("\n".join(turns(call)) + "\n", encoding="utf-8")
            clips = out / f"clips{call + 1}"
            subprocess.run([sys.executable, "-m", "voice.live.script", "--turns", str(text), "--out", str(clips),
                            "--server", f"http://127.0.0.1:{args.lfm_port}"], env=env, check=True,
                           stdout=(out / f"clips{call + 1}.out").open("w"), stderr=subprocess.STDOUT)
            wavs = sorted(clips.glob("turn_*.wav"))
            started = time.time()
            subprocess.run([str(client), "-m", "voice.live.client", "--server", f"ws://127.0.0.1:{args.port}",
                            "--secret-file", str(VOICE / "live/secret"), "--log-dir", str(out / f"client{call + 1}"),
                            "--wav", *map(str, wavs)], env=env, check=True, timeout=240,
                           stdout=(out / f"client{call + 1}.out").open("w"), stderr=subprocess.STDOUT)
            print(f"call {call + 1} done in {time.time() - started:.0f} s", flush=True)
    finally:
        stop.touch()
        try:
            child.wait(timeout=30)
        except subprocess.TimeoutExpired:
            child.terminate()
            child.wait(timeout=15)


def summarize(out):
    """One line per turn from the server's records: what was said, the first audio after the person stopped, and any
    reply that was held or cut. Calls split where the ledger's turn count starts over."""
    rows, call = [], 0
    for path in sorted((out / "server").glob("turn_*.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        live = record.get("live") or {}
        number = (live.get("call") or {}).get("turn", 0)
        call += number == 1
        replies = [(r.get("kind"), r.get("delivery"), r.get("text")) for r in record.get("replies", [])]
        held = [r for r in replies if r[1] not in (None, "sent")]
        checks = [e for e in record.get("timeline", []) if e.get("kind") == "fact_check"]
        rows.append({"call": call, "turn": number, "idle": bool(record.get("idle")), "said": record.get("final_transcript"),
                     "replies": replies, "first_audio_after_speech_end_s": live.get("speech_end_to_first_audio_sent_s"),
                     "held": held, "fact_checks": len(checks), "error": record.get("error")})
    launches = out / "seam-calls.json"
    started = [c for c in json.loads(launches.read_text(encoding="utf-8")) if c[0] == "launch"] if launches.exists() else []
    return rows, started


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mode", choices=["drive", "serve", "summary"])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--calls", type=int, default=5)
    parser.add_argument("--port", type=int, default=8771)
    parser.add_argument("--lfm-port", type=int, default=8095)
    parser.add_argument("--max-minutes", type=float, default=13)
    parser.add_argument("--cpu", action="store_true", help="smoke test on the CPU: no GPU layers")
    argv = sys.argv[1:]
    head, rest = (argv[:argv.index("--")], argv[argv.index("--") + 1:]) if "--" in argv else (argv, [])
    args = parser.parse_args(head)
    if args.mode == "summary":
        rows, started = summarize(args.out)
        print(json.dumps({"rows": rows, "threads_started": len(started)}, indent=1))
    else:
        serve(args, rest) if args.mode == "serve" else run_drive(args)


if __name__ == "__main__":
    main()
