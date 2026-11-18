"""The decider's tool pick over the four T3 tools, weather, and time, through the end of turn live mode runs.

Each case in `eval/tool_pick.jsonl` is one utterance with the tool a person labeled before any run. It runs as
`live_gate_eval.run_item` runs a live turn: the intent rules decide whether the decider is asked, then the hybrid
segmenter asks its questions with the tool question's options as `--t3 on` offers them (`T3Registry`). The pick is
the tool the decider answered for the utterance's first segment. `reaches` says whether that pick would route:
the gate dispatched the task and its tool answer clears the registry's bar. The free decider refuses the place and
day questions (a 400 on their `text` type), so they are off unless `--arguments` asks for them.

    VOICE_ENV_FILE=<env file with OPENROUTER_API_KEY> python -m voice.t3.tool_pick_eval

Every call goes to the free decider, one at a time, cached under `$VOICE_HOME/evals/t3-tool-pick`, so a rerun
scores the recorded answers and calls nothing.
"""

from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from pathlib import Path

from voice.live.t3_tasks import T3Registry
from voice.local_decider.client import decider as make_decider
from voice.prototype.segmenter import live_calls, use_decider_cache
from voice.research.segmentation.live_gate_eval import run_item
from voice.tools import registry

CASES = Path(__file__).resolve().parent / "eval" / "tool_pick.jsonl"
OUT = Path(os.environ.get("VOICE_HOME", Path.home() / "voice")) / "evals" / "t3-tool-pick"
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


def reaches(trace, tool) -> bool:
    return any(t["dispatched"] and (t.get("tool") or {}).get("tool") == tool
               and (t["tool"].get("confidence") or 0) >= registry.TOOL_THRESHOLD for t in trace["tasks"])


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


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--budget", type=int, default=150, help="stop when the ledger holds this many attempts")
    parser.add_argument("--only", help="one case id, for a setup check")
    parser.add_argument("--arguments", action="store_true", help="also ask the place and day questions")
    args = parser.parse_args(argv)
    cases = [json.loads(line) for line in CASES.read_text(encoding="utf-8").splitlines() if line.strip()]
    if args.only:
        cases = [c for c in cases if c["id"] == args.only]
    use_decider_cache(OUT / "ledger", args.budget)
    before = live_calls()
    rows = []
    with make_decider("cloud") as client:
        for case in cases:
            item = {**case, "split": "t3", "category": case["tool"], "expect": case["tool"], "context": {}}
            trace, _ = run_item(client, item, 1, False, calls=OUT / "calls", tools=TOOLS,
                                arguments=args.arguments)
            pick, confidence = picked(trace)
            rows.append({"id": case["id"], "label": case["tool"], "pick": pick, "confidence": confidence,
                         "reaches": reaches(trace, pick), "intent": trace["intent"], "utterance": case["utterance"]})
            print(json.dumps(rows[-1]), flush=True)
    (OUT / "results.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    right = sum(r["pick"] == r["label"] for r in rows)
    print(f"\n{right} of {len(rows)} picks match the label; {live_calls() - before} ledger attempts this run\n")
    print(table(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
