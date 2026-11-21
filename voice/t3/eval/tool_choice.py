"""Which tool the decider picks for approve, deny, answer, status, chat, claim-bait and withdraw turns, once per decider. Every utterance is made up.

    VOICE_ENV_FILE=<env file> python -m voice.t3.eval.tool_choice liquid     # the hosted d1.2 the calls use
    python -m voice.t3.eval.tool_choice d1                                    # the local d1-2B, under the GPU lease

Each case runs as the end of a live turn does (`run.close_with_decider`, the `--t3 on` options): with an approval a thread
waits on (the approve and deny tools are then offered, and the kind question says so), with a question it waits on (the
answer tool is then offered, with the question and its options), or with none, and the thread the call last spoke about. A case passes when the tool it must reach, and no other T3 tool, is dispatched over the registry's
bar; a turn that must reach none dispatches nothing. Nothing here was tuned on the hosted decider: the two lines added to
the approve and deny tools for the local one are reported as they ran.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from voice.live.t3_tasks import ANSWER, APPROVE, DENY, T3Registry
from voice.prototype import run
from voice.prototype.segmenter import use_decider_cache
from voice.segmentation.contract import TranscriptUpdate
from voice.segmentation.gate import PLAN_A
from voice.segmentation.hybrid import HybridSegmenter
from voice.tools import registry

WAITING = [{"kind": "approval", "thread": "Tidy the Save Slots", "asks_for": "approval to run a command"}]
WAITING_QUESTION = [{"kind": "question", "thread": "Tidy the Save Slots", "question": "Which storage should it use?",
                     "options": ["Postgres", "SQLite", "Keep the current store"]}]
WAITING_QUESTIONS = [*WAITING_QUESTION, {"kind": "question", "thread": "Fix the Map Markers",
                                          "question": "Which marker colour should it use?", "options": ["Red", "Blue"]}]
THREAD = "Tidy the Save Slots"
APPROVAL, QUESTION, QUESTIONS = "approval", "question", "two questions"
# (utterance, what waits on the person: an approval, a question or nothing, what it must reach)
CASES = [
    ("I approve.", APPROVAL, APPROVE), ("I give it my permission.", APPROVAL, APPROVE),
    ("Go ahead and allow it.", APPROVAL, APPROVE), ("Yes, let it run that.", APPROVAL, APPROVE),
    ("You have my permission.", APPROVAL, APPROVE),
    ("No, don't allow that.", APPROVAL, DENY), ("I deny it.", APPROVAL, DENY), ("Don't let it run that.", APPROVAL, DENY),
    ("I'd rather not give permission.", APPROVAL, DENY),
    ("How is the save slots thread going?", APPROVAL, "t3_ask"), ("What's the save slots thread up to?", APPROVAL, "t3_ask"),
    ("Is it done yet?", APPROVAL, "t3_ask"),
    ("Thanks, that's all.", APPROVAL, "none"), ("Hello there.", APPROVAL, "none"), ("Okay, thanks.", APPROVAL, "none"),
    ("Okay, I already gave it permission in the app a minute ago.", APPROVAL, "none"),
    ("I stopped that thread this morning.", APPROVAL, "none"), ("So it has finished, then.", APPROVAL, "none"),
    ("Never mind.", APPROVAL, "none"), ("Forget it.", APPROVAL, "none"), ("Can we start over?", APPROVAL, "none"),
    ("I approve.", None, "not approve or deny"), ("No, don't allow that.", None, "not approve or deny"),
    ("How is the save slots thread going?", None, "t3_ask"), ("Never mind.", None, "none"),
    ("Can we start over?", None, "none"),
    # A question waits: an answer by name, by place, in other words, or in the speaker's own words reaches the answer tool.
    ("Postgres.", QUESTION, ANSWER), ("Let's go with SQLite.", QUESTION, ANSWER), ("The second one.", QUESTION, ANSWER),
    ("I'd say keep the current store.", QUESTION, ANSWER), ("Go with the first option.", QUESTION, ANSWER),
    ("Use Postgres, please.", QUESTION, ANSWER), ("Option two.", QUESTION, ANSWER),
    ("Whatever it's already using, keep that.", QUESTION, ANSWER), ("Just use a plain JSON file.", QUESTION, ANSWER),
    ("Let's try MySQL instead.", QUESTION, ANSWER),
    ("How is the save slots thread going?", QUESTION, "t3_ask"), ("Is it done yet?", QUESTION, "t3_ask"),
    ("Thanks, that's all.", QUESTION, "none"), ("Hello there.", QUESTION, "none"), ("Never mind.", QUESTION, "none"),
    ("Okay, I already answered that in the app a minute ago.", QUESTION, "none"), ("Can we start over?", QUESTION, "none"),
    ("I approve.", QUESTION, "not approve or deny"), ("No, don't allow that.", QUESTION, "not approve or deny"),
    # Two threads wait on a question: an answer by place or by name still reaches the answer tool.
    ("The second one.", QUESTIONS, ANSWER), ("Go with Postgres.", QUESTIONS, ANSWER), ("Blue.", QUESTIONS, ANSWER),
    # Nothing, or only an approval, waits: an answer-shaped reply has no question to answer.
    ("Postgres.", None, "not answer"), ("Let's go with SQLite.", None, "not answer"),
    ("The second one.", APPROVAL, "not answer"), ("Use Postgres, please.", APPROVAL, "not answer"),
]


def routed(client, utterance: str, waiting: str | None) -> list[str]:
    on_the_person = {APPROVAL: WAITING, QUESTION: WAITING_QUESTION, QUESTIONS: WAITING_QUESTIONS}.get(waiting, [])
    tools = T3Registry(registry.build(None, None, None), lambda: on_the_person)
    choices = tools.choices()
    segmenter = HybridSegmenter(client, "tool-choice", tools=choices,
                                arguments=run.decider_arguments(client, t3_enabled=True))
    segmenter.pending_requests = list(tools.approvals)
    segmenter.pending_questions = list(tools.questions)
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
    if want == "not answer":
        return ANSWER not in got
    return got == [want]


def main() -> int:
    from voice.local_decider.client import decider as make_decider

    name = sys.argv[1] if len(sys.argv) > 1 else "liquid"
    # "answer" runs only the cases that have a question waiting or say an answer with none, for a spend-bounded run.
    only = sys.argv[2:] == ["answer"]
    chosen_cases = [c for c in CASES if c[1] in (QUESTION, QUESTIONS) or c[2] == "not answer"] if only else CASES
    home = Path(os.environ.get("VOICE_HOME", Path.home() / "voice"))
    use_decider_cache(home / "evals" / f"tool-choice-{name}", 120)
    rows = []
    with make_decider(name) as client:
        for utterance, waiting, want in chosen_cases:
            got = routed(client, utterance, waiting)
            rows.append({"utterance": utterance, "waiting": waiting, "want": want, "routed": got,
                         "passes": passes(want, got)})
            print(json.dumps(rows[-1]), flush=True)
    for label, chosen in (("approve", [r for r in rows if r["want"] == APPROVE]),
                          ("deny", [r for r in rows if r["want"] == DENY]),
                          ("answer", [r for r in rows if r["want"] == ANSWER]),
                          ("status question", [r for r in rows if r["want"] == "t3_ask"]),
                          ("chat, claim-bait and withdraw", [r for r in rows if r["want"] == "none"]),
                          ("approve or deny said with nothing to approve",
                           [r for r in rows if r["want"] == "not approve or deny"]),
                          ("an answer said with no question waiting", [r for r in rows if r["want"] == "not answer"])):
        print(f"{name}: {label}: {sum(r['passes'] for r in chosen)} of {len(chosen)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
