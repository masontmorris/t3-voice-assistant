"""Which tool the decider picks for approve, deny, status, chat, claim-bait and withdraw turns, once per decider. Every utterance is made up.

    VOICE_ENV_FILE=<env file> python -m voice.t3.eval.tool_choice liquid     # the hosted d1.2 the calls use
    python -m voice.t3.eval.tool_choice d1                                    # the local d1-2B, under the GPU lease

Each case runs as the end of a live turn does (`run.close_with_decider`, the `--t3 on` options): with an approval a thread
waits on (the approve and deny tools are then offered, and the kind question says so) or with none, and the thread the
call last spoke about. A case passes when the tool it must reach, and no other T3 tool, is dispatched over the registry's
bar; a turn that must reach none dispatches nothing. Nothing here was tuned on the hosted decider: the two lines added to
the approve and deny tools for the local one are reported as they ran.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from voice.live.t3_tasks import APPROVE, DENY, T3Registry
from voice.prototype import run
from voice.prototype.segmenter import use_decider_cache
from voice.segmentation.contract import TranscriptUpdate
from voice.segmentation.gate import PLAN_A
from voice.segmentation.hybrid import HybridSegmenter
from voice.tools import registry

WAITING = [{"thread": "Tidy the Save Slots", "asks_for": "approval to run a command"}]
THREAD = "Tidy the Save Slots"
# (utterance, an approval waits, what it must reach)
CASES = [
    ("I approve.", True, APPROVE), ("I give it my permission.", True, APPROVE),
    ("Go ahead and allow it.", True, APPROVE), ("Yes, let it run that.", True, APPROVE),
    ("You have my permission.", True, APPROVE),
    ("No, don't allow that.", True, DENY), ("I deny it.", True, DENY), ("Don't let it run that.", True, DENY),
    ("I'd rather not give permission.", True, DENY),
    ("How is the save slots thread going?", True, "t3_ask"), ("What's the save slots thread up to?", True, "t3_ask"),
    ("Is it done yet?", True, "t3_ask"),
    ("Thanks, that's all.", True, "none"), ("Hello there.", True, "none"), ("Okay, thanks.", True, "none"),
    ("Okay, I already gave it permission in the app a minute ago.", True, "none"),
    ("I stopped that thread this morning.", True, "none"), ("So it has finished, then.", True, "none"),
    ("Never mind.", True, "none"), ("Forget it.", True, "none"), ("Can we start over?", True, "none"),
    ("I approve.", False, "not approve or deny"), ("No, don't allow that.", False, "not approve or deny"),
    ("How is the save slots thread going?", False, "t3_ask"), ("Never mind.", False, "none"),
    ("Can we start over?", False, "none"),
]


def routed(client, utterance: str, waiting: bool) -> list[str]:
    tools = T3Registry(registry.build(None, None, None), lambda: WAITING if waiting else [])
    choices = tools.choices()
    segmenter = HybridSegmenter(client, "tool-choice", tools=choices,
                                arguments=run.decider_arguments(client, t3_enabled=True))
    segmenter.pending_requests = list(tools.approvals)
    segmenter.conversation_mode = True
    segmenter.orch.gate, segmenter.orch.side_effects_wait_for_final = PLAN_A, True
    segmenter.prior_resolved_tasks = run.with_thread((), THREAD)
    run.close_with_decider(segmenter, TranscriptUpdate(1, utterance, True))
    dispatched = {d["task_id"] for d in segmenter.dispatch_log}
    return sorted({(r.get("tool") or {}).get("tool") for r in segmenter.records()
                   if r["task_id"] in dispatched and (r.get("tool") or {}).get("tool")
                   and (r["tool"].get("confidence") or 0) >= registry.TOOL_THRESHOLD})


def passes(want: str, got: list[str]) -> bool:
    if want == "none":
        return not got
    if want == "not approve or deny":
        return not {APPROVE, DENY} & set(got)
    return got == [want]


def main() -> int:
    from voice.local_decider.client import decider as make_decider

    name = sys.argv[1] if len(sys.argv) > 1 else "liquid"
    home = Path(os.environ.get("VOICE_HOME", Path.home() / "voice"))
    use_decider_cache(home / "evals" / f"tool-choice-{name}", 120)
    rows = []
    with make_decider(name) as client:
        for utterance, waiting, want in CASES:
            got = routed(client, utterance, waiting)
            rows.append({"utterance": utterance, "approval_waiting": waiting, "want": want, "routed": got,
                         "passes": passes(want, got)})
            print(json.dumps(rows[-1]), flush=True)
    for label, chosen in (("approve", [r for r in rows if r["want"] == APPROVE]),
                          ("deny", [r for r in rows if r["want"] == DENY]),
                          ("status question", [r for r in rows if r["want"] == "t3_ask"]),
                          ("chat, claim-bait and withdraw", [r for r in rows if r["want"] == "none"]),
                          ("approve or deny said with nothing waiting",
                           [r for r in rows if r["want"] == "not approve or deny"])):
        print(f"{name}: {label}: {sum(r['passes'] for r in chosen)} of {len(chosen)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
