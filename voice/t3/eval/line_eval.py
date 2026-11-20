"""Score a line writer on made-up acts, through the checks the live call applies. GPU: run inside the lease.

    python -m voice.t3.eval.line_eval --writer local|realtime|hosted|realtime+local --out DIR [--repeats 1]

Each case in voice/t3/eval/line_cases.py is worded the way a turn words it: up to MAX_DRAFTS drafts, the second
carrying what the first was refused for, each checked by the slot check and by Liquid's d1.2. The report counts, per
group, the cases whose first draft passed both checks and the cases where some draft within the turn's drafts did.

`local` is LFM3-1.7B on its own llama-server, `realtime` the LFM3-Audio text-only session the live call words its
lines with, `hosted` the cloud text model named by VOICE_PAID_TEXT_MODEL, and `realtime+local` the live writer with
the local model behind it. The Liquid ledger and cache for the run stay under --out. Nothing from a call is read.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections import defaultdict
from contextlib import AsyncExitStack
from pathlib import Path
from types import SimpleNamespace

from voice.prototype.run import MAX_DRAFTS
from voice.t3.eval.line_cases import cases, turns_for
from voice.t3.speech import Lane, LocalText, Speech
from voice.t3.verify import Decider

LFM_ARGS = ("-c", "32768", "-kvu")
LFM_SLOTS = 3


def decider(out: Path) -> Decider:
    from voice.liquid_decider import client as liquid
    from voice.segmentation.client import Pacer

    liquid.use_cache(out / "liquid")
    return Decider(liquid.LiquidClient(pacer=Pacer(first_s=0.0, cap_s=0.0), timeout_s=10.0, max_attempts=3))


def word(speech: Speech, acts, turns) -> dict:
    """One case as a turn words it. Returns what each draft was and whether any passed."""
    from voice.live.t3_tasks import refuse

    update = SimpleNamespace(problems=(), tries=0, refused=())
    drafts = []
    for _ in range(MAX_DRAFTS):
        started = time.perf_counter()
        rendered = speech.render(acts, turns, update.problems, update.refused, update.tries)
        refuse(update, rendered)
        drafts.append({"raw": rendered.raw, "passed": rendered.line is not None, "problems": rendered.problems,
                       "worded_by": rendered.line.worded_by if rendered.line is not None else None,
                       "error": rendered.error, "s": round(time.perf_counter() - started, 2)})
        if rendered.line is not None:
            break
    return {"acts": [a.to_json() for a in acts], "drafts": drafts, "first": drafts[0]["passed"],
            "any": any(d["passed"] for d in drafts)}


def run_cases(speech: Speech, repeats: int, groups: list[str]) -> list[dict]:
    rows = []
    for group, acts in [c for c in cases() if not groups or c[0] in groups] * repeats:
        row = {"group": group, **word(speech, acts, turns_for(acts))}
        rows.append(row)
        print(json.dumps({"group": group, "kind": acts[0].kind.value, "first": row["first"], "any": row["any"],
                          "line": next((d["raw"] for d in reversed(row["drafts"])), None)})[:260], flush=True)
    return rows


def report(rows: list[dict]) -> dict:
    by = defaultdict(lambda: [0, 0, 0])
    for row in rows:
        for key in (row["group"], "all"):
            by[key][0] += 1
            by[key][1] += row["first"]
            by[key][2] += row["any"]
    return {key: {"cases": n, "first_draft_passed": first, "passed_within_drafts": any_}
            for key, (n, first, any_) in by.items()}


async def main_async(args) -> int:
    from voice.local_decider.client import LlamaServer
    from voice.live.t3_tasks import LOCAL_TEXT_FLAGS, LOCAL_TEXT_MODEL
    from voice.prototype.realtime import Server
    from voice.t3.pick.model import PAID_MODEL, LiveClient
    from voice.t3.realtime_writer import RealtimeWriter

    args.out.mkdir(parents=True, exist_ok=True)
    check = decider(args.out)
    kinds = args.writer.split("+")
    lane = writer = None
    async with AsyncExitStack() as stack:
        if "local" in kinds:
            stack.enter_context(LlamaServer(["-m", str(LOCAL_TEXT_MODEL), *LOCAL_TEXT_FLAGS], args.local_port,
                                                   args.out / "text-server.log"))
            lane = Lane(LocalText(f"http://127.0.0.1:{args.local_port}"))
        if "hosted" in kinds:
            lane = Lane(LiveClient(model=PAID_MODEL, cache_dir=args.out / "text-cache", timeout_s=30.0, max_attempts=3))
        if "realtime" in kinds:
            stack.enter_context(Server(args.lfm_port, args.out / "lfm3-audio.log", LFM_ARGS, LFM_SLOTS))
            writer = RealtimeWriter(f"ws://127.0.0.1:{args.lfm_port}/v1/realtime")
            await stack.enter_async_context(writer)
        speech = Speech(lane, lambda thread_id: None, check, Lane(writer) if writer else None)
        rows = await asyncio.to_thread(run_cases, speech, args.repeats, args.groups)
    result = {"writer": args.writer, "report": report(rows), "rows": rows}
    (args.out / f"lines-{args.writer}.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
    print(json.dumps({"writer": args.writer, **result["report"]}, indent=1))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--writer", required=True, choices=["local", "realtime", "hosted", "realtime+local",
                                                            "realtime+hosted"])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--groups", nargs="*", default=[], help="only these case groups")
    parser.add_argument("--local-port", type=int, default=8099)
    parser.add_argument("--lfm-port", type=int, default=8095)
    args = parser.parse_args(argv)
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
