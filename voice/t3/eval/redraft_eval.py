"""What a retry of a refused line does. Three measures, each a mode. The two with a writer run inside the GPU lease.

    python -m voice.t3.eval.redraft_eval repeat   --out DIR [--groups G ...]
    python -m voice.t3.eval.redraft_eval turns    --out DIR [--groups G ...] [--trials 3]
    python -m voice.t3.eval.redraft_eval resample --out DIR [--samples 3]        (no GPU)

`repeat`: every case in voice/t3/eval/line_cases.py is worded once by the realtime writer, then asked for again the way a
turn asks after a refusal, with the first wording named as refused and the check's own reason beside it. The refusal is
forced, so every case gets a second request, and the count is of second drafts that say the refused wording again. The
writer decodes greedily, so a repeat is the same request answered the same way.

`turns`: each case is worded as a turn words it, up to two drafts, through the slot check and the hosted d1.2, for a
number of trials. Every check read is a real call, and a trial's decider keeps the first reading of a line and serves
it again unless asked for a fresh one, as the hosted client's cache does within a process. Each trial whose first draft
the decider refused (not the slot check) is then retried twice from that same reading: once as the code before this
change did, where the kept refusal serves the same words again, and once as the code now does.

`resample`: the recorded line-check cases (voice/t3/fixtures/verify_lines.json) read again, fresh, a number of times,
with the share that pass among the lines that should pass and among those that should not. It shows what a second
reading can rescue and what it could let through. Nothing from a call is read in any mode.
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

from voice.t3.acts import Act, normalise
from voice.t3.eval.line_cases import cases, turns_for
from voice.t3.eval.line_eval import LFM_ARGS, LFM_SLOTS, STORED, decider, prepared
from voice.t3.realtime_writer import RealtimeWriter
from voice.t3.speech import Lane, Speech
from voice.t3.verify import Decider, Verdict

REASON = "the line says something the facts do not support. Say only what the act means and add nothing."
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "verify_lines.json"


class Refuser:
    """A check that refuses every line it is asked about, for the reason a real refusal gives."""

    def verify(self, acts, line, *, fresh=False) -> Verdict:
        return Verdict(False, {}, ("a1_claims",), problems=(REASON,))


class Keeps:
    """A decider client over `inner` that keeps the first reading it gave for a request and serves it again unless
    asked `fresh`, as the hosted client's cache does within a process. Every reading it makes is a real call."""

    def __init__(self, inner):
        self.inner, self.kept = inner, {}

    def call(self, route, body, label="", fresh=False):
        key = json.dumps(body, sort_keys=True)
        if not fresh and key in self.kept:
            return self.kept[key]
        record = self.inner.call(route, body, label=label, fresh=True)
        if not fresh:
            self.kept[key] = record
        return record


class Before(Speech):
    """`Speech` as it was: a refused wording that comes back is not read again."""

    def accept(self, acts, text, worded_by, raw=None, *, known_names=frozenset(), again=False):
        return super().accept(acts, text, worded_by, raw, known_names=known_names, again=False)


def chosen(groups: list[str]):
    """The cases of the groups asked for, or of every group but the held-request one, which has its own measure."""
    return [c for c in cases() if (c[0] in groups if groups else c[0] != "another_request")]


def repeat(speech: Speech, groups: list[str]) -> dict:
    rows = []
    for group, acts, scene in chosen(groups):
        turns = turns_for(acts)
        acts = prepared(speech, acts, scene)
        first = speech.render(acts, turns, (), (), 0)
        second = speech.render(acts, turns, first.problems, [first.raw] if first.raw else [], 1)
        rows.append({"group": group, "acts": [a.to_json() for a in acts], "first": first.raw, "second": second.raw,
                     "same": bool(first.raw) and normalise(first.raw) == normalise(second.raw or "")})
        print(json.dumps({"group": group, "same": rows[-1]["same"], "second": second.raw})[:300], flush=True)
    by = defaultdict(lambda: [0, 0])
    for row in rows:
        for key in (row["group"], "all"):
            by[key][0] += 1
            by[key][1] += row["same"]
    return {"report": {key: {"cases": n, "second_draft_repeats_first": same} for key, (n, same) in by.items()},
            "rows": rows}


def trial(after: Speech, before: Speech, acts, turns, scene) -> dict:
    """One case in one trial: the first draft, and, when the decider refused it, the second draft under each policy.
    Both policies start from the same reading of the first draft, so the pair differs only in the second."""
    acts = prepared(after, acts, scene)
    first = after.render(acts, turns, (), (), 0)
    if first.line is not None:
        return {"first_passed": True}
    row = {"first_passed": False, "by_decider": first.verdict is not None, "first": first.raw}
    if first.verdict is None:
        return row
    for name, speech in (("before", before), ("after", after)):
        second = speech.render(acts, turns, first.problems, [first.raw], 1)
        row[name] = {"passed": second.line is not None, "same": normalise(second.raw or "") == normalise(first.raw)}
    return row


def turns_mode(writer: Lane, checker: Decider, groups: list[str], trials: int) -> dict:
    rows = []
    for t in range(trials):
        reads = Decider(Keeps(checker.client))
        after = Speech(None, lambda thread_id: STORED.get("state"), reads, writer)
        before = Before(None, lambda thread_id: STORED.get("state"), reads, writer)
        for group, acts, scene in chosen(groups):
            rows.append({"trial": t, "group": group, **trial(after, before, acts, turns_for(acts), scene)})
            print(json.dumps({k: v for k, v in rows[-1].items() if k != "first"})[:300], flush=True)
    refused = [r for r in rows if r.get("by_decider")]
    return {"trials": trials, "draws": len(rows), "first_passed": sum(r["first_passed"] for r in rows),
            "first_refused_by_slot_check": sum(1 for r in rows if not r["first_passed"] and not r.get("by_decider")),
            "first_refused_by_decider": len(refused),
            "second_draft_same_words": sum(r["after"]["same"] for r in refused),
            "passed_on_the_retry_before": sum(r["before"]["passed"] for r in refused),
            "passed_on_the_retry_after": sum(r["after"]["passed"] for r in refused), "rows": rows}


def resample(client, samples: int) -> dict:
    """Each recorded line read `samples` more times, fresh."""
    reads = Decider(client)
    by = {True: [0, 0, 0], False: [0, 0, 0]}
    rows = []
    for row in json.loads(FIXTURE.read_text(encoding="utf-8")):
        acts = [Act.from_json(a) for a in row["acts"]]
        passed = [reads.verify(acts, row["line"], fresh=True).passed for _ in range(samples)]
        rows.append({"case": row["case"], "line": row["line"], "should_pass": row["should_pass"], "passed": passed})
        stat = by[row["should_pass"]]
        stat[0] += len(passed)
        stat[1] += sum(passed)
        stat[2] += 0 < sum(passed) < len(passed)
    return {"samples_per_line": samples,
            "lines_that_should_pass": {"readings": by[True][0], "passed": by[True][1], "lines_that_flip": by[True][2]},
            "lines_that_should_not": {"readings": by[False][0], "passed": by[False][1], "lines_that_flip": by[False][2]},
            "rows": rows}


async def main_async(args) -> int:
    from voice.prototype.realtime import Server

    args.out.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    if args.mode == "resample":
        result = await asyncio.to_thread(resample, decider(args.out).client, args.samples)
    else:
        async with AsyncExitStack() as stack:
            stack.enter_context(Server(args.lfm_port, args.out / "lfm3-audio.log", LFM_ARGS, LFM_SLOTS))
            writer = RealtimeWriter(f"ws://127.0.0.1:{args.lfm_port}/v1/realtime")
            await stack.enter_async_context(writer)
            if args.mode == "repeat":
                speech = Speech(None, lambda thread_id: STORED.get("state"), Refuser(), Lane(writer))
                result = await asyncio.to_thread(repeat, speech, args.groups)
            else:
                result = await asyncio.to_thread(turns_mode, Lane(writer), decider(args.out), args.groups, args.trials)
    result["seconds"] = round(time.perf_counter() - started)
    (args.out / f"{args.mode}.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k != "rows"}, indent=1))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mode", choices=["repeat", "turns", "resample"])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--groups", nargs="*", default=[], help="only these case groups")
    parser.add_argument("--trials", type=int, default=3)
    parser.add_argument("--samples", type=int, default=3)
    parser.add_argument("--lfm-port", type=int, default=8095)
    args = parser.parse_args(argv)
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
