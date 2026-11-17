"""With and without the note a barge-in puts in the voice session, on the recorded interruptions of the gate-1 run.

    python -m voice.research.live.bargein_note_ab --note off --label control
    python -m voice.research.live.bargein_note_ab --note on --label note

One live server and one client call per run. The call replays the gate-1 `int` sequence: the synthetic lighthouse
prompt, then an interruption clip started 1.5 s into the reply, nine times. The nine clips are five of Mason's own
turns from the 2026-11-16 call and four synthetic commands; the prompt and the synthetic clips were rendered by
`voice/live/script.py` (TTS) for `$VOICE_HOME/evidence/live/bargein/run_e2e.py` and its gate-1 run. All of that audio
stays under $VOICE_HOME and is never copied into the repo or sent anywhere.

Each run's difference from the other is the value of --barge-in-note and nothing else.

The score reads the server's turn records: for every turn whose overlap was a barge-in, it takes the reply the person
talked over and the reply that answered them, and reports whether the second reuses the first. Two code signals: the
longest run of words the two replies share (four or more is a repetition or a continuation, since a different answer
shares at most a topic word), and whether the answer still names a lighthouse. Both are proxies that undercount a
paraphrased continuation; the reply texts stay in the records for a reader.

Needs the GPU (llama-server) and the two virtualenvs, given by VOICE_SERVER_PYTHON and VOICE_CLIENT_PYTHON.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from voice.prototype.realtime import VOICE_HOME

CASES = VOICE_HOME / "evidence" / "live" / "bargein"
PROMPT = CASES / "synth" / "turn_001.wav"
CALL1 = VOICE_HOME / "evidence" / "live" / "20261116-152653"
INTERRUPTIONS = [
    CALL1 / "turn_004_input.wav",
    CALL1 / "turn_011_input.wav",
    CALL1 / "turn_016_input.wav",
    CALL1 / "turn_021_input.wav",
    CALL1 / "turn_029_input.wav",
    CASES / "synth" / "turn_010.wav",
    CASES / "synth" / "turn_011.wav",
    CASES / "synth" / "turn_012.wav",
    CASES / "synth" / "turn_013.wav",
]
BARGE_IN_MS = 1500
SHARED_WORDS = 4
WORD = re.compile(r"[a-z0-9']+")


def words(text):
    return WORD.findall((text or "").lower())


def longest_run(a, b):
    """The longest run of words the two texts share, in order."""
    best, previous = 0, [0] * (len(b) + 1)
    for left in a:
        current = [0]
        for index, right in enumerate(b, 1):
            current.append(previous[index - 1] + 1 if left == right else 0)
            best = max(best, current[-1])
        previous = current
    return best


def note(path, line):
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def wait_until(log, needle, timeout=240):
    deadline = time.time() + timeout
    while time.time() < deadline:
        text = log.read_text(encoding="utf-8", errors="replace")
        if needle in text:
            return
        if "Traceback" in text:
            raise SystemExit(f"the live server failed:\n{text[-3000:]}")
        time.sleep(0.5)
    raise SystemExit(f"timed out waiting for {needle!r}")


def run_server(args, out, log):
    server = [args.server_python, "-m", "voice.live.server", "--host", "127.0.0.1", "--port", str(args.port),
              "--lfm-port", str(args.lfm_port), "--barge-in-note", args.note, "--max-minutes", "12",
              "--stop-file", str(out / "STOP"), "--out", str(out / "server"), "--mock-tools", "--delay", "1"]
    note(log, "$ " + " ".join(server))
    with open(out / "server.out", "w", encoding="utf-8") as handle:
        process = subprocess.Popen(server, stdout=handle, stderr=subprocess.STDOUT)
    try:
        wait_until(out / "server.out", "live server on")
        clips = [clip for interruption in INTERRUPTIONS for clip in (PROMPT, interruption)]
        client = [args.client_python, "-m", "voice.live.client", "--server", f"ws://127.0.0.1:{args.port}",
                  "--secret-file", str(VOICE_HOME / "live" / "secret"), "--wait-for-server", "60",
                  "--barge-in-ms", str(BARGE_IN_MS), "--wav", *map(str, clips),
                  "--log-dir", str(out / "client")]
        note(log, "$ " + " ".join(client))
        with open(out / "client.out", "w", encoding="utf-8") as handle:
            code = subprocess.run(client, stdout=handle, stderr=subprocess.STDOUT, timeout=1800).returncode
        note(log, f"client exit {code}")
    finally:
        (out / "STOP").write_text("", encoding="utf-8")
        try:
            process.wait(timeout=120)
        except subprocess.TimeoutExpired:
            process.terminate()
            process.wait(timeout=30)
        note(log, f"server exit {process.returncode}")


def score(out, write=True):
    server = out / "server"
    records = [(path, json.loads(path.read_text(encoding="utf-8")))
               for path in sorted(server.glob("turn_*.json"))]
    by_name = {path.stem: record for path, record in records}
    cases = []
    for path, record in records:
        overlap = (record.get("live") or {}).get("overlap") or {}
        if overlap.get("verdict") != "barge_in":
            continue
        over = by_name.get(f"turn_{overlap.get('turn'):03d}")
        if over is None:
            continue
        interrupted = " ".join(reply.get("text") or "" for reply in over.get("replies", []))
        answer = " ".join(reply.get("text") or "" for reply in record.get("replies", []))
        shared = longest_run(words(interrupted), words(answer))
        lighthouse = any(word.startswith("lighthouse") for word in words(answer))
        cases.append({
            "case": path.stem,
            "interrupted_turn": overlap.get("turn"),
            "answer_call_turn": (record.get("live") or {}).get("call", {}).get("turn"),
            "heard_note": (record.get("live") or {}).get("barge_in_note"),
            "interrupted_reply": interrupted, "answer_reply": answer,
            "shared_run": shared, "lighthouse": lighthouse,
            "reused_old_answer": shared >= SHARED_WORDS or lighthouse,
            "record": str(path.relative_to(VOICE_HOME)),
        })
    report = {"note": out.name, "cases": cases,
              "reused_old_answer": sum(case["reused_old_answer"] for case in cases), "n": len(cases),
              "with_note": sum(1 for case in cases if case["heard_note"])}
    if write:
        (out / "score.json").write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--note", choices=["on", "off"], required=True)
    parser.add_argument("--label", help="records directory under $VOICE_HOME/evidence/live/bargein-note")
    parser.add_argument("--port", type=int, default=8797)
    parser.add_argument("--lfm-port", type=int, default=8117)
    parser.add_argument("--server-python", default=os.environ.get("VOICE_SERVER_PYTHON", sys.executable))
    parser.add_argument("--client-python", default=os.environ.get("VOICE_CLIENT_PYTHON", sys.executable))
    parser.add_argument("--score-only", type=Path, help="score an existing records directory and run nothing")
    args = parser.parse_args()
    if args.score_only is not None:
        print(json.dumps(score(args.score_only, write=False), indent=1))
        return
    missing = [str(path) for path in (PROMPT, *INTERRUPTIONS) if not path.is_file()]
    if missing:
        raise SystemExit("missing clips:\n" + "\n".join(missing))
    out = VOICE_HOME / "evidence" / "live" / "bargein-note" / args.label
    out.mkdir(parents=True, exist_ok=True)
    (out / "STOP").unlink(missing_ok=True)
    log = out / "run.log"
    note(log, f"# started {time.strftime('%Y-%m-%d %H:%M:%S')} label {args.label}")
    note(log, f"# difference: --barge-in-note {args.note} over the gate-1 int sequence (9 interruptions, 1500 ms in), "
              "LFM3 local, mock tools")
    note(log, "# expects to learn: whether the reply to an interruption carries on the interrupted answer with "
              f"the note {args.note}")
    note(log, json.dumps({"cases": [str(path) for path in INTERRUPTIONS], "prompt": str(PROMPT)}))
    run_server(args, out, log)
    note(log, f"finished {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(json.dumps(score(out), indent=1))


if __name__ == "__main__":
    main()
