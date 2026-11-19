"""Record the hosted decider's reading of follow-ups about a thread, so voice/t3/test_followups.py replays real answers.

    VOICE_ENV_FILE=<env file> python -m voice.t3.eval.record_followups

Each case is an utterance, the thread the call last spoke about when there is one, an earlier lookup when there is one,
and where the utterance must go: a T3 tool (`t3_ask`, `t3_tell`, `t3_stop`), a lookup tool (`weather`, `time`), or
nowhere (`none`). Every case runs as the end of a live turn does (`run.close_with_decider` with the `--t3 on` tool
options and the gate), and passes when its tool, and no other T3 tool, is dispatched over the registry's bar. `before`
runs it with no thread in view and with the intent rules alone deciding whether the decider is asked, as the code did
before follow-ups; `after` is the shipped code. Every thread, place and request here is made up.

`SEEN` was looked at while the decider text was written. `HELD` was written before any run and has never been used to
change anything: report both. The fixture keeps each recorded answer under a hash of the request it answered, so a
changed request text finds no answer and the test says to record again.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from voice.live.t3_tasks import T3Registry
from voice.prototype import run
from voice.segmentation.contract import Intent, TranscriptUpdate
from voice.segmentation.gate import PLAN_A
from voice.segmentation.hybrid import HybridSegmenter
from voice.segmentation.intent import utterance_intent
from voice.tools import registry

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "followup_decisions.json"
TOOLS = T3Registry(registry.build(None, None, None)).choices()
T3 = sorted(registry.T3_TOOLS)

RETRY, LOADER = "Add a Retry Button", "Speed Up the Map Loader"
DENVER = {"ref": "turn_002:task_1", "instruction": "What's the weather in Denver tomorrow?", "side_effect": False,
          "call": {"tool": "weather", "arguments": {"place": "Denver", "when": "tomorrow"}}}
SEATTLE = {"ref": "turn_004:task_1", "instruction": "How's the weather in Seattle today?", "side_effect": False,
           "call": {"tool": "weather", "arguments": {"place": "Seattle", "when": "today"}}}
# (utterance, the thread last spoken about, an earlier lookup, where it must go)
SEEN = [
    ("Give me more detail on that.", RETRY, None, "t3_ask"),
    ("Is it done yet?", RETRY, None, "t3_ask"),
    ("How's that one going?", RETRY, None, "t3_ask"),
    ("What did it say?", RETRY, None, "t3_ask"),
    ("And how is that thread doing now?", RETRY, None, "t3_ask"),
    ("I wanted an update on the thread from before.", RETRY, None, "t3_ask"),
    ("Tell it to also add tests.", RETRY, None, "t3_tell"),
    ("Stop it.", RETRY, None, "t3_stop"),
    ("Thanks.", RETRY, None, "none"),
    ("Okay, great.", RETRY, None, "none"),
    ("How are you doing today?", RETRY, None, "none"),
    ("What's the weather in Denver tomorrow?", RETRY, None, "weather"),
    ("What about Boston?", RETRY, DENVER, "weather"),
    ("What time is it in Tokyo?", RETRY, None, "time"),
]
HELD = [
    ("Any update on that?", LOADER, None, "t3_ask"),
    ("Did it finish?", LOADER, None, "t3_ask"),
    ("What's it working on right now?", LOADER, None, "t3_ask"),
    ("How long has it been running?", LOADER, None, "t3_ask"),
    ("Ask it to log the load times too.", LOADER, None, "t3_tell"),
    ("Please tell it to stop touching the config.", LOADER, None, "t3_tell"),
    ("Cancel that one.", LOADER, None, "t3_stop"),
    ("Cool, thank you.", LOADER, None, "none"),
    ("Never mind.", LOADER, None, "none"),
    ("Good morning.", LOADER, None, "none"),
    ("What's the weather like in Austin?", LOADER, None, "weather"),
    ("What about Chicago?", LOADER, SEATTLE, "weather"),
]
CASES = [(*case, False) for case in SEEN] + [(*case, True) for case in HELD]


def key(body: dict) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode("utf-8")).hexdigest()


def decide(client, utterance, recent, earlier, mode):
    """One end of turn as the live server runs it, in `mode`. Returns the tools of the tasks it dispatched over the
    registry's bar."""
    segmenter = HybridSegmenter(client, f"followup:{mode}", tools=TOOLS,
                                arguments=run.decider_arguments(client, t3_enabled=True))
    segmenter.conversation_mode = True
    segmenter.orch.gate, segmenter.orch.side_effects_wait_for_final = PLAN_A, True
    prior = (earlier,) if earlier else ()
    segmenter.prior_resolved_tasks = prior
    update = TranscriptUpdate(1, utterance, True)
    if mode == "after":
        segmenter.prior_resolved_tasks = run.with_thread(prior, recent)
        run.close_with_decider(segmenter, update, prior)
    else:
        if utterance_intent(utterance).intent == Intent.REQUEST:
            segmenter.update(update)
        segmenter.close(update, settle=False)
    dispatched = {d["task_id"] for d in segmenter.dispatch_log}
    return sorted({(r.get("tool") or {}).get("tool") for r in segmenter.records()
                   if r["task_id"] in dispatched and (r.get("tool") or {}).get("tool")
                   and (r["tool"].get("confidence") or 0) >= registry.TOOL_THRESHOLD})


def passes(want: str, routed: list[str]) -> bool:
    t3 = [tool for tool in routed if tool in T3]
    if want in T3:
        return t3 == [want]
    if want == "none":
        return not routed
    return want in routed and not t3


class Recording:
    """The decider, with each answer kept under the hash of its request."""

    def __init__(self, client, answers):
        self.client, self.answers = client, answers

    def call(self, route, body, label=""):
        record = self.client.call(route, body, label=label)
        if record["status"] == 200:
            self.answers[key(body)] = record["response"]
        return record

    def __getattr__(self, name):
        return getattr(self.client, name)


def main() -> int:
    import os

    from voice.local_decider.client import decider as make_decider
    from voice.prototype.segmenter import use_decider_cache

    use_decider_cache(Path(os.environ.get("VOICE_HOME", Path.home() / "voice")) / "evals" / "t3-followups", 300)
    answers, rows = {}, []
    with make_decider("liquid") as client:
        recording = Recording(client, answers)
        for utterance, recent, earlier, want, held in CASES:
            for mode in ("before", "after"):
                routed = decide(recording, utterance, recent, earlier, mode)
                rows.append({"utterance": utterance, "recent": recent, "earlier": earlier, "want": want, "held": held,
                             "mode": mode, "routed": routed, "passes": passes(want, routed)})
                print(json.dumps(rows[-1]), flush=True)
    FIXTURE.write_bytes((json.dumps({"rows": rows, "answers": answers}, indent=1) + "\n").encode("utf-8"))
    for held in (False, True):
        for mode in ("before", "after"):
            chosen = [r for r in rows if r["held"] == held and r["mode"] == mode]
            thread = [r for r in chosen if r["want"] in T3]
            other = [r for r in chosen if r["want"] not in T3]
            print(f"{'held' if held else 'seen'} {mode}: thread follow-ups right {sum(r['passes'] for r in thread)} "
                  f"of {len(thread)}, other turns right {sum(r['passes'] for r in other)} of {len(other)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
