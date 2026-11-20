"""The decider's tool pick over the four T3 tools, weather, and time, and whether each pick reaches its tool.

Each case in `eval/tool_pick.jsonl` is one utterance with the tool a person labeled before any run: 20 T3 requests,
10 weather and time controls, and 15 controls where the right tool is none or the general lookup. `--gate` adds the
live gate eval's weather and time items (`voice/research/segmentation/eval/live_gate.jsonl`), with the call context
each carries. Every case runs as `live_gate_eval.run_item` runs a live turn: the intent rules decide whether the
decider is asked, then the hybrid segmenter asks its questions with the tool options `--t3 on` offers (`T3Registry`).
The pick is the tool answered for the utterance's first segment. `reaches` says whether a task with that tool was
dispatched with a tool answer that clears the registry's bar, which is what routes it to the T3 service.

`--text before` scores the decider text and gate from before seam unit 10: no T3 sentence in the request-versus-chat
question, no T3 pass in the gate, and no parked thread request sent to T3 as a lookup. It reproduces the old request
bodies byte for byte, which the recorded calls check: a recorded answer is reused only when its request equals the
body sent now.

    VOICE_ENV_FILE=<env file with OPENROUTER_API_KEY> python -m voice.t3.tool_pick_eval --text after --gate
    VOICE_ENV_FILE=<env file with LIQUID_API_KEY> python -m voice.t3.tool_pick_eval --text nofragments --decider liquid

`--text nofragments` is `after` without the three lines for a request cut into fragments (`T3_READY`, `T3_DEPENDS` and
`T3_CORRECTS`), to see whether they move any control. `--decider liquid` asks d1.2 instead of the free decider.

Every call goes to the free decider, one at a time, cached under `$VOICE_HOME/evals/t3-tool-pick`, so a rerun scores
the recorded answers and calls nothing. The place and day questions stay off: the free decider refuses their `text`
type with a 400.
"""

from __future__ import annotations

import argparse
import json
import os
from collections import Counter, defaultdict
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

from voice.live.t3_tasks import T3Registry
from voice.local_decider.client import decider as make_decider
from voice.prototype.segmenter import live_calls, use_decider_cache
from voice.research.segmentation import live_gate_eval
from voice.segmentation import contract, hybrid, tasks
from voice.tools import registry

CASES = Path(__file__).resolve().parent / "eval" / "tool_pick.jsonl"
GATE_CASES = live_gate_eval.EVAL
OUT = Path(os.environ.get("VOICE_HOME", Path.home() / "voice")) / "evals" / "t3-tool-pick"
CALLS = {"before": OUT / "calls", "after": OUT / "calls-after", "nofragments": OUT / "calls-nofragments"}
TOOLS = T3Registry(registry.build(None, None, None)).choices()
NOT_ASKED = "not asked"


def picked(trace) -> tuple[str, float | None]:
    """The first segment's tool answer, or NOT_ASKED when the intent rules kept the decider out."""
    if not trace["asked_decider"]:
        return NOT_ASKED, None
    for segment in trace["segments"]:
        tool = segment.get("tool")
        if isinstance(tool, dict) and tool.get("tool"):
            return tool["tool"], tool.get("confidence")
    return "no answer", None


def routed(trace) -> list[str]:
    """The tools of every dispatched task whose tool answer clears the registry's bar."""
    return [t["tool"]["tool"] for t in trace["tasks"] if t["dispatched"] and (t.get("tool") or {}).get("tool")
            and (t["tool"].get("confidence") or 0) >= registry.TOOL_THRESHOLD]


def reaches(trace, tool) -> bool:
    return tool in routed(trace)


@contextmanager
def decider_text(which):
    """`before` puts back the unit 9 text and gate for the length of a run; `after` is the shipped code."""
    if which == "after":
        yield
        return
    if which == "nofragments":
        saved = hybrid.T3_READY, hybrid.T3_DEPENDS, hybrid.T3_CORRECTS
        hybrid.T3_READY = hybrid.T3_DEPENDS = hybrid.T3_CORRECTS = ""
        try:
            yield
        finally:
            hybrid.T3_READY, hybrid.T3_DEPENDS, hybrid.T3_CORRECTS = saved
        return
    # Before unit 10 no gate let a T3 pick through, no parked thread request went to T3 as a lookup, and no tool
    # answer was read as a group.
    saved = hybrid.T3_REQUEST, hybrid.T3_CHITCHAT, live_gate_eval.PLAN_A, tasks.T3_TOOLS, contract.T3_TOOLS
    hybrid.T3_REQUEST = hybrid.T3_CHITCHAT = ""
    live_gate_eval.PLAN_A = replace(saved[2], confirmed_tools=frozenset())
    tasks.T3_TOOLS = contract.T3_TOOLS = frozenset()
    try:
        yield
    finally:
        hybrid.T3_REQUEST, hybrid.T3_CHITCHAT, live_gate_eval.PLAN_A, tasks.T3_TOOLS, contract.T3_TOOLS = saved


class StrictRecording(live_gate_eval.Recording):
    """A recorded answer counts only for the body it answered, so changed text never scores old answers."""

    def call(self, route, body, label=""):
        path = self.calls / f"{self.key}.{self.n + 1}.json"
        if path.exists() and not self.fresh:
            recorded = json.loads(path.read_text(encoding="utf-8"))
            if recorded.get("request") != body:
                raise RuntimeError(f"{path.name} answered a different body; record this text in its own directory")
        return super().call(route, body, label)


def cases(gate: bool) -> list[dict]:
    rows = [{**json.loads(line), "set": "tool_pick", "context": {}}
            for line in CASES.read_text(encoding="utf-8").splitlines() if line.strip()]
    if gate:
        rows += [{**item, "id": f"g_{item['id']}", "set": "gate"}
                 for item in map(json.loads, GATE_CASES.read_text(encoding="utf-8").splitlines())
                 if item.get("tool") in ("weather", "time")]
    return rows


def run_case(client, case, sample, calls, paid=False) -> dict:
    item = {"split": "t3", "category": case["tool"], "expect": case.get("expect", case["tool"]), **case}
    recording = StrictRecording(client, f"{item['id']}.r{sample}", calls=calls, paid=paid)
    trace, _ = live_gate_eval.run_item(client, item, sample, False, tools=TOOLS, recording=recording)
    pick, confidence = picked(trace)
    return {"id": case["id"], "set": case["set"], "sample": sample, "label": case["tool"], "pick": pick,
            "confidence": confidence, "routed": routed(trace), "reaches": reaches(trace, case["tool"]),
            "intent": trace["intent"], "utterance": case["utterance"]}


def table(rows) -> str:
    labels = list(dict.fromkeys(r["label"] for r in rows))
    columns = labels + sorted({r["pick"] for r in rows} - set(labels))
    counts = Counter((r["label"], r["pick"]) for r in rows)
    lines = ["| label \\ pick | " + " | ".join(columns) + " | right |",
             "| --- | " + " | ".join("---" for _ in columns) + " | --- |"]
    for label in labels:
        n = sum(counts[(label, c)] for c in columns)
        cells = [str(counts[(label, c)] or "") for c in columns]
        lines.append(f"| {label} | " + " | ".join(cells) + f" | {counts[(label, label)]} of {n} |")
    return "\n".join(lines)


def majority(rows, passed) -> dict[str, bool]:
    """Per case, whether more than half of its samples pass. A case fails when half or more of them fail."""
    by_case = defaultdict(list)
    for row in rows:
        by_case[row["id"]].append(passed(row))
    return {case: sum(results) * 2 > len(results) for case, results in by_case.items()}


def summary(rows) -> str:
    t3 = [r for r in rows if r["set"] == "tool_pick" and r["label"] in registry.T3_TOOLS]
    chat = [r for r in rows if r["set"] == "tool_pick" and r["label"] in (registry.NONE, registry.GENERAL)]
    gate = [r for r in rows if r["set"] == "gate"]
    lookups = [r for r in rows if r["set"] == "tool_pick" and r["label"] in ("weather", "time")]
    false_t3 = [r for r in chat if registry.T3_TOOLS & set(r["routed"])]
    reached = majority(t3, lambda r: r["reaches"])
    lines = [f"T3 requests that reach their tool: {sum(reached.values())} of {len(reached)} cases "
             f"({sum(r['reaches'] for r in t3)} of {len(t3)} samples)",
             f"false T3 actions on the {len(majority(chat, bool))} chitchat and lookup controls: {len(false_t3)} "
             f"samples {sorted({r['id'] for r in false_t3})}",
             f"weather and time controls that reach their tool: {sum(majority(lookups, lambda r: r['reaches']).values())}"
             f" of {len(majority(lookups, bool))} cases"]
    if gate:
        lines.append("gate cases, tools routed per sample: " + "; ".join(
            f"{case} {[r['routed'] for r in gate if r['id'] == case]}" for case in dict.fromkeys(r["id"] for r in gate)))
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--text", choices=sorted(CALLS), default="after",
                        help="the decider text and gate to score: before or after seam unit 10 (default after)")
    parser.add_argument("--decider", choices=["cloud", "liquid"], default="cloud",
                        help="cloud is the free OpenRouter decider the recorded runs used; liquid is d1.2, whose answers "
                             "are recorded beside them under a -liquid suffix")
    parser.add_argument("--samples", type=int, default=3, help="samples per case (default 3)")
    parser.add_argument("--gate", action="store_true", help="add the live gate eval's weather and time items")
    parser.add_argument("--only", help="comma-separated case ids")
    parser.add_argument("--budget", type=int, default=600, help="stop when the ledger holds this many attempts")
    args = parser.parse_args(argv)
    chosen = cases(args.gate)
    if args.only:
        wanted = set(args.only.split(","))
        chosen = [c for c in chosen if c["id"] in wanted]
    use_decider_cache(OUT / "ledger", args.budget)
    before = live_calls()
    rows = []
    calls = CALLS[args.text].with_name(CALLS[args.text].name + ("" if args.decider == "cloud" else "-liquid"))
    with decider_text(args.text), make_decider(args.decider) as client:
        for case in chosen:
            for sample in range(1, args.samples + 1):
                rows.append(run_case(client, case, sample, calls, paid=args.decider == "liquid"))
                print(json.dumps(rows[-1]), flush=True)
    name = f"results-{args.text}-{args.decider}.jsonl" if not args.only else f"results-{args.text}-{args.decider}-only.jsonl"
    (OUT / name).write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    print(f"\n{live_calls() - before} ledger attempts this run, {args.samples} samples per case\n")
    print(summary(rows) + "\n")
    print(table([r for r in rows if r["set"] == "tool_pick"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
