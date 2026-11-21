"""Score a line writer on made-up acts, through the checks the live call applies. GPU: run inside the lease.

    python -m voice.t3.eval.line_eval --writer local|realtime|hosted|realtime+local --out DIR [--repeats 1]

Each case in voice/t3/eval/line_cases.py is worded the way a turn words it: up to MAX_DRAFTS drafts, the second
carrying what the first was refused for, each checked by the slot check and by Liquid's d1.2. The report counts, per
group, the cases whose first draft passed both checks and the cases where some draft within the turn's drafts did.
The JSON also records checks of fixed command descriptions under `need_checks`.

`local` is LFM3-1.7B on its own llama-server, `realtime` the LFM3-Audio text-only session the gate words its
lines with, `hosted` the cloud text model named by VOICE_PAID_TEXT_MODEL, and `realtime+local` the realtime writer with
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
from voice.t3 import fakes as F
from voice.t3 import types as T
from voice.t3.eval.line_cases import ACCOUNT_CASES, cases, need_lines, turns_for
from voice.t3.speech import (Lane, LocalText, RequestGate, Speech, candidate_names, named, reference, without_request)
from voice.t3.acts import Act, Kind
from voice.t3.speech import Launch
from voice.t3.verify import Decider

LFM_ARGS = ("-c", "32768", "-kvu")
LFM_SLOTS = 3


def decider(out: Path) -> Decider:
    from voice.liquid_decider import client as liquid
    from voice.segmentation.client import Pacer

    liquid.use_cache(out / "liquid")
    return Decider(liquid.LiquidClient(pacer=Pacer(first_s=0.0, cap_s=0.0), timeout_s=10.0, max_attempts=3))


STORED = {}


def prepared(speech: Speech, acts, scene) -> tuple:
    """The acts as a turn holds them when it words them: a pending request's stored text replaced by its gated
    description, and a launched thread named as T3 titles it now (or, in a which-thread question, each offered
    thread by its title or by what it was started for)."""
    stored = acts[0].slots.get("prompt", acts[0].slots.get("pending"))
    if stored is not None:
        STORED["state"] = F.state(thread="t", title=acts[0].slots.get("thread", "t"),
                                  activity=T.ThreadActivity.WAITING_ON_YOU, pending=F.need(prompt=stored))
        acts, _ = speech.fill(acts, "t")
    if scene is None:
        return acts
    ref = reference(scene.thread, scene.launched, scene.titles)
    me = next((l for l in scene.launched if l.thread_id == scene.thread), None)
    account = ref is not None and ref.unnamed and ref.shared and not scene.on_call
    acts = named(acts, ref, speech.account(me) if account else None, scene.on_call)
    if acts[0].kind is Kind.ASK_WHICH and scene.offered:
        acts = (Act(acts[0].kind, {**acts[0].slots, "candidates": candidate_names(
            scene.offered, scene.titles, scene.launched, speech.account)}),)
    return acts


def word(speech: Speech, acts, turns, scene=None) -> dict:
    """One case as a turn words it. Returns what each draft was and whether any passed."""
    from voice.live.t3_tasks import refuse

    acts = prepared(speech, acts, scene)
    update = SimpleNamespace(problems=(), tries=0, refused=())
    drafts = []
    for _ in range(MAX_DRAFTS):
        started = time.perf_counter()
        if update.tries and any("request" in a.slots for a in acts):
            acts, update.problems, update.refused = without_request(acts), (), ()
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
    for group, acts, scene in [c for c in cases() if not groups or c[0] in groups] * repeats:
        row = {"group": group, **word(speech, acts, turns_for(acts), scene)}
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


def check_need_lines(speech: Speech, legacy: bool = False) -> list[dict]:
    rows = []
    for shape, mode, said, line in need_lines(legacy):
        checked = speech.accept((said,), line, "eval")
        rows.append({"shape": shape, "mode": mode, "act": said.to_json(), "line": line, "passed": checked.line is not None,
                     "problems": checked.problems, "error": checked.error,
                     "invents": checked.verdict.answers.get("invents") if checked.verdict else None})
    return rows


def check_accounts(speech: Speech) -> list[dict]:
    """The gate on accounts of what a thread was started for: the faithful account and two others for each request, and
    the one the writer makes (empty when the gate did not pass it)."""
    rows = []
    for request, faithful, wrong in ACCOUNT_CASES:
        made = speech.account(Launch("t", request, "Pebble Garden"))
        for shape, account in (("faithful", faithful), *(("invented", w) for w in wrong)):
            doubt = speech.gate.doubts_account(request, account)
            rows.append({"shape": shape, "account": account, "doubt": doubt, "passed": doubt is not None and doubt < 0.10})
        rows.append({"shape": "written", "account": made})
    return rows


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
        gate = None
        if lane is not None and isinstance(lane.client, LocalText):
            from voice.live.t3_tasks import SUMMARY_GATE_PORT, summary_gate_server
            stack.enter_context(summary_gate_server(args.out / "summary-gate.log"))
            gate = RequestGate(f"http://127.0.0.1:{SUMMARY_GATE_PORT}")
            lane.client.gate_url = f"http://127.0.0.1:{SUMMARY_GATE_PORT}"
        speech = Speech(lane, lambda thread_id: STORED.get("state"), check, Lane(writer) if writer else None, gate=gate)
        rows = await asyncio.to_thread(run_cases, speech, args.repeats, args.groups)
        accounts = None
        if gate is not None and (not args.groups or "held_out_shapes" in args.groups):
            accounts = await asyncio.to_thread(check_accounts, speech)
    result = {"writer": args.writer, "report": report(rows), "rows": rows, **({"account_checks": accounts} if accounts else {})}
    if not args.groups or "needs_with_command" in args.groups:
        result["need_checks"] = await asyncio.to_thread(check_need_lines, speech, args.legacy)
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
    parser.add_argument("--legacy", action="store_true", help="check the need lines as the act was before the request "
                        "slot (for a run on older code)")
    parser.add_argument("--local-port", type=int, default=8099)
    parser.add_argument("--lfm-port", type=int, default=8095)
    args = parser.parse_args(argv)
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
