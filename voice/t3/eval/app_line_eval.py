"""Score two lines the writer words from its examples: a run that ended stopped (`done` with a cancelled, interrupted or
rolled-back status) and a request that has to go to the app with no checked description (`needs_the_app` with only
`need`). GPU: run inside the lease.

    python -m voice.t3.eval.app_line_eval --out DIR [--repeats 4] [--arms before after]

The writer is the LFM3-Audio text-only session the call words its lines with, and the line check is the hosted d1.2,
as in voice/t3/eval/line_eval.py, on the made-up acts of its `done_stopped` and `needs_the_app_need` groups. `before`
shows the writer the examples it had before 2026-11-22 (the `done` example that says "finished and reports", and only
the `needs_the_app` example that has a `request`); `after` the shipped ones. Each case's first draft that passed is judged
by the hosted d1.2: whether every sentence is grammatical, and for an app line whether it says what kind of thing is to
be approved.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from contextlib import AsyncExitStack
from pathlib import Path

from voice.live.scenario_score import Judge
from voice.t3 import speech as SP
from voice.t3.acts import Kind
from voice.t3.eval.line_cases import cases, turns_for
from voice.t3.eval.line_eval import LFM_ARGS, LFM_SLOTS, STORED, decider, word

GROUPS = ("done_stopped", "needs_the_app_need")
BEFORE_APP = ({"thread": "Example Tag Camera Clips", "decision": "approve", "need": "permission",
               "request": "It wants to use a tool that lists your calendar events."},
              "Example Tag Camera Clips wants to use a tool that lists your calendar events, and that has to be "
              "approved in the app.")
# The variants added on 2026-11-22, which `before` does not show.
NEW_VARIANTS = ((Kind.DONE, "stopped"), (Kind.NEEDS_THE_APP, "file_change"), (Kind.NEEDS_THE_APP, "permission"))
GRAMMATICAL = "Is every sentence of the text grammatical English, with no word missing?"
SAYS_KIND = ("Does the text say what kind of thing the thread wants approved, such as running a command or changing "
             "files, and not only that something needs approval?")


def use(arm: str, shipped: dict) -> None:
    """Show the writer the examples of `arm`."""
    SP.READER_EXAMPLES[Kind.NEEDS_THE_APP] = BEFORE_APP if arm == "before" else shipped["app"]
    for key, example in shipped["variants"].items():
        if arm == "before":
            SP.READER_VARIANTS.pop(key, None)
        else:
            SP.READER_VARIANTS[key] = example


async def main_async(args) -> int:
    from voice.prototype.realtime import Server
    from voice.t3.realtime_writer import RealtimeWriter

    args.out.mkdir(parents=True, exist_ok=True)
    shipped = {"app": SP.READER_EXAMPLES[Kind.NEEDS_THE_APP],
               "variants": {key: SP.READER_VARIANTS[key] for key in NEW_VARIANTS}}
    judge, rows = Judge(), []
    async with AsyncExitStack() as stack:
        stack.enter_context(Server(args.lfm_port, args.out / "lfm3-audio.log", LFM_ARGS, LFM_SLOTS))
        writer = RealtimeWriter(f"ws://127.0.0.1:{args.lfm_port}/v1/realtime")
        await stack.enter_async_context(writer)
        speech = SP.Speech(None, lambda thread_id: STORED.get("state"), decider(args.out), SP.Lane(writer))
        for arm in args.arms:
            use(arm, shipped)
            for n in range(args.repeats):
                for group, acts, scene in [c for c in cases() if c[0] in GROUPS]:
                    row = await asyncio.to_thread(word, speech, acts, turns_for(acts), scene)
                    line = next((d["raw"] for d in row["drafts"] if d["passed"]), None)
                    judged = {}
                    if line:
                        judged["grammatical"] = judge.yes(GRAMMATICAL, line, "A voice assistant said this to a person.")
                        if group == "needs_the_app_need":
                            judged["says_kind"] = judge.yes(SAYS_KIND, line, f"The thread waits on: {acts[0].slots['need']}")
                    rows.append({"arm": arm, "trial": n, "group": group, "line": line, "any": row["any"], **judged})
                    print(json.dumps(rows[-1])[:300], flush=True)
    summary = {}
    for arm in args.arms:
        for group in GROUPS:
            mine = [r for r in rows if r["arm"] == arm and r["group"] == group]
            said = [r for r in mine if r["line"]]
            summary[f"{arm} {group}"] = {"passed checks": f"{len(said)} of {len(mine)}",
                                         "grammatical": f"{sum(r['grammatical'] for r in said)} of {len(said)}",
                                         **({"says_kind": f"{sum(r['says_kind'] for r in said)} of {len(said)}"}
                                            if group == "needs_the_app_need" else {})}
    (args.out / "app-lines.json").write_text(json.dumps({"summary": summary, "rows": rows}, indent=1),
                                             encoding="utf-8")
    print(json.dumps(summary, indent=1))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=4)
    parser.add_argument("--arms", nargs="+", default=["before", "after"], choices=["before", "after"])
    parser.add_argument("--lfm-port", type=int, default=8095)
    return asyncio.run(main_async(parser.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
