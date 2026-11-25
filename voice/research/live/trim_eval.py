"""After a barge-in, does the next reply rely on what the caller never heard? The live server with and without
`--trim-unheard`, on synthetic callers who talk over a long reply.

    python -m voice.research.live.trim_eval run --trim on --out DIR/on --bin PATH [--trials 5]
    python -m voice.research.live.trim_eval score DIR/off DIR/on

`run` starts the real live server (`voice.live.rehearse` serve, its in-process T3 seam, the hosted decider) on the
llama-server `--bin`, renders the callers' lines with its own TTS, and holds one call per shape and trial: the caller
asks for a long answer and talks over it `delay` ms after the reply starts, the delay stepping through DELAYS_MS. Both
arms run the same binary, so the flag is the only difference.

Three shapes, each a made-up caller:

- `continue`: asked to go on. Good when the next reply picks up where the caller stopped hearing.
- `unheard`: asked about the end of a list the reply had not reached when it was stopped. Bad when the next reply
  speaks of the unheard part as something it already said.
- `new`: a new, unrelated request. Good when it is answered; bad when the reply refers back to the unheard part.

`score` asks the hosted decider (the scenarios' `Judge`) one stated yes-or-no question per reply, with what the caller
heard and did not hear of the stopped reply as its context. What was heard is the server's own record of the barge-in
(`live.barge_in.heard`), the read-back of the audio that played; the stopped reply's text is the whole reply it
generated. The general `relies_on_unheard` question read 1 of the off arm's 5 "unheard" replies as relying on the list,
though all 5 name "the last bridge on my list"; `treats_list_as_heard` was written for that shape after reading them and
before the on arm ran, and gives 0.85 to 0.92 on those 5 and 0.02 to 0.11 on three made-up good replies. It also reports the barge-in turn's delay from the end of speech to its first audio, and the trim's own
record. Records stay under the --out directories, outside the repo.
"""

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

from voice.live import rehearse as R
from voice.live.scenario_score import Judge

DELAYS_MS = (1500, 2000, 2500, 3000, 3500)
SHAPES = {
    "continue": {"ask": "Can you walk me through how to bake a loaf of bread at home, step by step, in detail?",
                 "over": "Sorry, someone was at the door. Please go on."},
    "unheard": {"ask": "Can you tell me about five famous bridges around the world, one at a time?",
                "over": "Wait, what was the last bridge on your list?"},
    "new": {"ask": "Can you tell me about the history of lighthouses, in some detail?",
            "over": "Actually, never mind that. How many legs does a spider have?"},
}
# Frozen on 2026-11-24, before either arm of the 15-calls-per-shape run: the context and the questions below are the
# ones the first run (5 calls per shape) used, and are not to be changed in the light of that run's results.
CONTEXT = ("An assistant on a phone call was giving a spoken reply when the caller talked over it, and the reply "
           "stopped there. The caller heard only this start of it: \"{heard}\". The caller never heard the rest of it: "
           "\"{unheard}\". The caller then said: \"{over}\". The text is the assistant's next reply.")
QUESTIONS = {
    "relies_on_unheard": ("Does the text treat any of the assistant's earlier reply beyond the part the caller heard "
                          "as already told to them?"),
    "resumes": ("Does the text go on from where the part the caller heard stops, without skipping past what they "
                "never heard?"),
    "answers": "Does the text say how many legs a spider has?",
    "treats_list_as_heard": ("Does the text talk about the assistant's list as if the caller had heard all of it, for "
                             "example by saying which item was last on it?"),
}
ASKED = {"continue": ("resumes", "relies_on_unheard"), "unheard": ("treats_list_as_heard", "relies_on_unheard"),
         "new": ("answers", "relies_on_unheard")}


def run(args):
    # The caller runs in this process and prints the reply captions, which can hold any character.
    sys.stdout.reconfigure(encoding="utf-8")
    out = args.out
    rest = ["--llama-server", str(args.bin), "--trim-unheard", args.trim, "--lfm-port", str(args.lfm_port)]
    calls = []
    with R.Rig(out, args.port, args.max_minutes, (), rest) as rig:
        lines = out / "lines.txt"
        keys = [(shape, part) for shape in SHAPES for part in ("ask", "over")]
        lines.write_text("\n".join(SHAPES[s][p] for s, p in keys) + "\n", encoding="utf-8")
        clips = dict(zip(keys, rig.render(lines, out / "clips", args.lfm_port)))
        # The same lines in the TTS's other voice: the second-voice reference for `one_voice`.
        subprocess.run([sys.executable, "-m", "voice.live.script", "--turns", str(lines), "--out",
                        str(out / "clips-male"), "--server", f"http://127.0.0.1:{args.lfm_port}", "--voice", "male"],
                       env=rig.env, check=True, stdout=subprocess.DEVNULL)
        server = rig.out / "server"

        def load(n):
            path = server / f"turn_{n:03d}.json"
            for _ in range(100):
                if path.is_file():
                    return json.loads(path.read_text(encoding="utf-8"))
                time.sleep(0.1)
            return None

        for trial in range(args.trials):
            for shape in SHAPES:
                delay = DELAYS_MS[trial % len(DELAYS_MS)]
                seen = []

                def script():
                    heard = yield R.Say("ask", over=("over", delay))
                    seen.extend(heard.turns)
                    if not any((t.get("live") or {}).get("barge_in") for t in heard.turns):
                        # The stopped turn can end the step before the turn that stopped it: let that one finish.
                        seen.extend((yield R.Pause(20)).turns)

                started = time.time()
                try:
                    R.hold_call(rig, script(), {"ask": clips[(shape, "ask")], "over": clips[(shape, "over")]}, load,
                                out / f"client-{shape}-{trial}")
                    error = None
                except Exception as exc:
                    error = f"{type(exc).__name__}: {exc}"
                calls.append({"shape": shape, "trial": trial, "delay_ms": delay, "error": error,
                              "turns": [t.get("clip") for t in seen], "s": round(time.time() - started, 1)})
                print(json.dumps(calls[-1]), flush=True)
                time.sleep(R.CALL_GAP_S)
    (out / "calls.json").write_text(json.dumps(calls, indent=1), encoding="utf-8")


def spoken(record):
    return " ".join(r["text"] for r in record.get("replies", [])
                    if r.get("text") and r.get("delivery", "sent") in ("sent", "partial"))


def case(arm, call):
    """The stopped reply, what was heard of it, and the reply after it, for one call; None when no barge-in came."""
    first = int(call["turns"][0].removeprefix("turn_"))
    paths = [arm / "server" / f"turn_{n:03d}.json" for n in (first, first + 1)]
    if not all(path.is_file() for path in paths):
        return None
    stopped, barge = (json.loads(path.read_text(encoding="utf-8")) for path in paths)
    if not (barge.get("live") or {}).get("barge_in"):
        return None
    said = " ".join(r["text"] for r in stopped.get("replies", []) if r.get("text"))
    info = barge["live"]["barge_in"]
    heard = info.get("heard") or ""
    unheard = said[len(heard):].strip() if said.startswith(heard) else said
    return {"heard": heard, "unheard": unheard, "whole": info.get("whole"), "next": spoken(barge),
            "trim": info.get("trim"), "note": info.get("note"),
            "delay_s": (barge.get("live") or {}).get("speech_end_to_first_audio_sent_s"),
            "reply_wavs": [str(arm / "server" / f"{stopped['clip']}_reply.wav"),
                           str(arm / "server" / f"{barge['clip']}_reply.wav")]}


def score(args):
    judge, table = Judge(), {}
    for arm in args.arms:
        calls = json.loads((arm / "calls.json").read_text(encoding="utf-8"))
        rows = []
        for call in calls:
            found = case(arm, call) if not call["error"] else None
            row = {**call, "case": found}
            if found is not None and not found["whole"] and found["next"]:
                context = CONTEXT.format(heard=found["heard"], unheard=found["unheard"], over=SHAPES[call["shape"]]["over"])
                row["p_yes"] = {q: round(judge.p_yes(QUESTIONS[q], found["next"], context), 3)
                                for q in ASKED[call["shape"]]}
            rows.append(row)
        (arm / "score.json").write_text(json.dumps(rows, indent=1), encoding="utf-8")
        table[arm.name] = rows
    for name, rows in table.items():
        print(f"\n{name}")
        for shape in SHAPES:
            mine = [r for r in rows if r["shape"] == shape]
            scored = [r for r in mine if "p_yes" in r]
            counts = ", ".join(f"{q} {sum(r['p_yes'][q] >= 0.5 for r in scored)}/{len(scored)}" for q in ASKED[shape])
            delays = [r["case"]["delay_s"] for r in mine if r["case"] and r["case"]["delay_s"] is not None]
            print(f"  {shape:9} {counts}; scored {len(scored)} of {len(mine)}; delay s "
                  f"{sorted(round(d, 2) for d in delays)}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="mode", required=True)
    go = sub.add_parser("run")
    go.add_argument("--trim", choices=["on", "off"], required=True)
    go.add_argument("--out", type=Path, required=True)
    go.add_argument("--bin", type=Path, required=True)
    go.add_argument("--trials", type=int, default=5)
    go.add_argument("--port", type=int, default=8772)
    go.add_argument("--lfm-port", type=int, default=8096)
    go.add_argument("--max-minutes", type=float, default=50.0)
    rate = sub.add_parser("score")
    rate.add_argument("arms", type=Path, nargs="+")
    args = parser.parse_args()
    run(args) if args.mode == "run" else score(args)


if __name__ == "__main__":
    main()
