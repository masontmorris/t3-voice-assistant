"""Record the hosted decider on whole thread requests, so voice/t3/test_whole_requests.py replays real answers.

    VOICE_ENV_FILE=<env file> python -m voice.t3.eval.record_whole_requests [--before] [--samples N]

Each case is one utterance as a live turn hears it: its words arrive as partial transcripts a few words at a time and
then as the final transcript, through the hybrid segmenter with the `--t3 on` tool options, the gate and
`run.close_with_decider`. A case passes when the thread tools of the tasks it dispatched are exactly the ones it wants,
one task each. Every dispatched thread task is a line the voice says, so a fragment that becomes a task of its own is a
fault even when it is the same tool. A thread task under the registry's bar counts as `t3_ask`, since that is what T3
then does with it.

The groups:
- `one`: one request with a cut the segmenter's markers put inside it ("and", a sentence end, a hesitation). One task.
- `two`: two requests in one breath. Two tasks, each its own tool.
- `tell`: an instruction to a thread, in several shapes. `t3_tell`.
- `ask`: a question about a thread. `t3_ask`.
- `stop`: a request to stop a thread's run. `t3_stop`.
- `mixed`: a question and an instruction in one turn.
- `none`: thanks and chat. No thread task.

`SEEN` was looked at while the decider text was written. `HELD` was written before any run and first run on the first
text, whose stop line lowered the stop margin there ("Cancel that one."), so that line went back: it has informed one
change. `FRESH` was written after that and before any run, and never used to change anything. `REVIEW` was written for
the review of the first version, before any run; its stop and two-action cases moved the tell line and the join
question's criteria. `LATE` was written after that, before any run, and `CONTROLS` must reach no thread at all.

Each case runs `--samples` times with fresh calls. `--before` keeps only the routings, under `before`, for the code it
ran on; the default run keeps every answer, by case and sample, so the test replays it. `--replay` routes the kept
answers again with the code as it is now, with no call: for a change that moves what the code does with the answers and
not what the decider is asked. Every thread, title and request here is made up.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from voice.live import t3_tasks
from voice.live.t3_tasks import T3Registry
from voice.prototype import run
from voice.segmentation.contract import TranscriptUpdate
from voice.segmentation.gate import PLAN_A
from voice.segmentation.hybrid import HybridSegmenter
from voice.tools import registry

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "whole_requests.json"
ACTS_ON = getattr(t3_tasks, "acts_on", None)
T3 = registry.T3_TOOLS
LOADER, RETRY, SPRITES = "Speed Up the Map Loader", "Add a Retry Button", "Batch the Sprite Draws"
APPROVAL = {"kind": "approval", "thread": LOADER, "asks_for": "approval to run a command"}
STEP = 3

# (group, utterance, thread last spoken about, whether LOADER waits on an approval, the thread tools it must dispatch)
SEEN = [
    ("one", "Could you check the map loader thread and tell me how that's going?", RETRY, False, ["t3_ask"]),
    ("one", "Okay, go ahead and approve it.", LOADER, True, ["t3_approve"]),
    ("one", "Hey, the retry button thread. What's it up to?", LOADER, False, ["t3_ask"]),
    ("one", "So, uh, I want to get a thread going on, uhm, a small tool, one that turns sprite sheets into atlases, and, "
            "uhm, could you start that and keep it tidy?", None, False, ["t3_start"]),
    ("one", "Take a look at the retry button thread and let me know if it's done.", LOADER, False, ["t3_ask"]),
    ("one", "Go to the retry button thread and tell it to add tests too.", LOADER, False, ["t3_tell"]),
    ("two", "How is the retry button thread going, and tell the map loader thread to add a progress bar.", None, False,
     ["t3_ask", "t3_tell"]),
    ("two", "Check the retry button thread and then start a new thread to clean up the logging.", None, False,
     ["t3_ask", "t3_start"]),
    ("tell", "Tell it to also add tests.", LOADER, False, ["t3_tell"]),
    ("tell", "Please have it write a short summary when it's done.", LOADER, False, ["t3_tell"]),
    ("tell", "Go ahead and have it switch to the new tile API.", LOADER, False, ["t3_tell"]),
    ("tell", "Could you tell the retry button thread to use a longer delay?", LOADER, False, ["t3_tell"]),
    ("tell", "Let it know the config file is off limits.", LOADER, False, ["t3_tell"]),
    ("tell", "Tell it to stop using the old shader.", LOADER, False, ["t3_tell"]),
    ("tell", "Have it stop logging every frame.", LOADER, False, ["t3_tell"]),
    ("tell", "Ask it to check whether the tests still pass.", LOADER, False, ["t3_tell"]),
    ("ask", "How's it going?", LOADER, False, ["t3_ask"]),
    ("ask", "Did it finish the tests?", LOADER, False, ["t3_ask"]),
    ("ask", "What did the retry button thread say?", LOADER, False, ["t3_ask"]),
    ("stop", "Stop it.", LOADER, False, ["t3_stop"]),
    ("stop", "Can you stop the map loader thread?", RETRY, False, ["t3_stop"]),
    ("mixed", "How's it doing? And tell it to add a test for the empty case.", LOADER, False, ["t3_ask", "t3_tell"]),
    ("none", "Thanks.", LOADER, False, []),
]
HELD = [
    ("one", "Can you look in on the sprite batching thread and see where it's at?", LOADER, False, ["t3_ask"]),
    ("one", "Yeah, go on and allow it.", LOADER, True, ["t3_approve"]),
    ("one", "Uhm, so, I was thinking we could, uh, have a thread look into why the save files are so big. Maybe a small "
            "cleanup project. Could you kick that off?", None, False, ["t3_start"]),
    ("one", "The release notes thread. Ask it to add the date at the top.", LOADER, False, ["t3_tell"]),
    ("one", "Check on the map loader thread for me and say if it needs anything.", RETRY, False, ["t3_ask"]),
    ("two", "Stop the map loader thread and ask the retry button thread how it's going.", None, False,
     ["t3_ask", "t3_stop"]),
    ("two", "Tell the retry button thread to add tests, and how's the map loader thread doing?", None, False,
     ["t3_ask", "t3_tell"]),
    ("two", "Is the sprite batching thread done? And start a thread to fix the jump timing.", None, False,
     ["t3_ask", "t3_start"]),
    ("tell", "Please tell it to stop touching the config.", LOADER, False, ["t3_tell"]),
    ("tell", "Have the map loader thread add a loading spinner.", RETRY, False, ["t3_tell"]),
    ("tell", "Go ahead and tell it to run the tests again.", LOADER, False, ["t3_tell"]),
    ("tell", "Can you ask it to keep the old function around?", LOADER, False, ["t3_tell"]),
    ("tell", "Tell the retry button thread to skip the flaky test.", LOADER, False, ["t3_tell"]),
    ("tell", "Make it use the cached tiles instead.", LOADER, False, ["t3_tell"]),
    ("tell", "I'd like it to also update the readme.", LOADER, False, ["t3_tell"]),
    ("tell", "Can you tell it to quit retrying the upload?", LOADER, False, ["t3_tell"]),
    ("tell", "Have the retry button thread stop retrying after three tries.", LOADER, False, ["t3_tell"]),
    ("tell", "Tell it to report how long the build takes.", LOADER, False, ["t3_tell"]),
    ("ask", "Is it still running?", LOADER, False, ["t3_ask"]),
    ("ask", "Has the map loader thread asked anything?", RETRY, False, ["t3_ask"]),
    ("ask", "What's it working on now?", LOADER, False, ["t3_ask"]),
    ("ask", "Did it ever stop touching the config?", LOADER, False, ["t3_ask"]),
    ("stop", "Please halt the map loader thread.", RETRY, False, ["t3_stop"]),
    ("stop", "Cancel that one.", LOADER, False, ["t3_stop"]),
    ("mixed", "Is the map loader thread done yet? Also ask it to add a spinner.", RETRY, False, ["t3_ask", "t3_tell"]),
    ("none", "Cool, thank you.", LOADER, False, []),
]
FRESH = [
    ("one", "Could you peek at the shader cache thread and tell me what it's doing?", LOADER, False, ["t3_ask"]),
    ("one", "Alright, sure, go ahead and approve that.", LOADER, True, ["t3_approve"]),
    ("one", "Hmm, so, there's this thing, uh, the menu music keeps cutting out. Could you get a thread going on that? "
            "Maybe just a quick fix.", None, False, ["t3_start"]),
    ("one", "The sprite batching thread. Tell it to keep the old batches as a fallback.", LOADER, False, ["t3_tell"]),
    ("two", "Tell the map loader thread to add a cache, and stop the retry button thread.", None, False,
     ["t3_stop", "t3_tell"]),
    ("two", "How's the retry button thread doing? And what about the sprite batching thread?", None, False,
     ["t3_ask", "t3_ask"]),
    ("tell", "Ask it to stop squashing the commits.", LOADER, False, ["t3_tell"]),
    ("tell", "Could you have it check the tile sizes too?", LOADER, False, ["t3_tell"]),
    ("tell", "Let the retry button thread know it can drop the old test.", LOADER, False, ["t3_tell"]),
    ("tell", "Go ahead and have it halt the deploy step if the build fails.", LOADER, False, ["t3_tell"]),
    ("ask", "Is it stuck on anything?", LOADER, False, ["t3_ask"]),
    ("ask", "What did it change in the loader?", LOADER, False, ["t3_ask"]),
    ("stop", "Halt it, please.", LOADER, False, ["t3_stop"]),
    ("stop", "Can you interrupt the retry button thread?", LOADER, False, ["t3_stop"]),
    ("mixed", "Did it finish? And have it add a changelog entry.", LOADER, False, ["t3_ask", "t3_tell"]),
    ("none", "Okay, sounds good.", LOADER, False, []),
]
REVIEW = [
    ("two", "Stop the importer thread and tell the tests thread to rerun.", None, False, ["t3_stop", "t3_tell"]),
    ("two", "Start a thread for the sprite atlas, and another one for the save screen.", None, False,
     ["t3_start", "t3_start"]),
    ("two", "Approve it and then tell it to push.", LOADER, True, ["t3_approve", "t3_tell"]),
    ("stop", "Tell it to stop.", LOADER, False, ["t3_stop"]),
    ("stop", "Ask it to stop.", LOADER, False, ["t3_stop"]),
    ("stop", "Can you tell the map loader thread to stop?", RETRY, False, ["t3_stop"]),
    ("tell", "Tell it to stop adding comments everywhere.", LOADER, False, ["t3_tell"]),
    ("tell", "Ask it to stop rebuilding the whole project each time.", LOADER, False, ["t3_tell"]),
]
LATE = [
    ("stop", "Could you ask the sprite batching thread to stop?", LOADER, False, ["t3_stop"]),
    ("stop", "Tell the retry button thread to stop, please.", LOADER, False, ["t3_stop"]),
    ("stop", "Just tell it to stop now.", LOADER, False, ["t3_stop"]),
    ("tell", "Tell it to stop retrying the network calls.", LOADER, False, ["t3_tell"]),
    ("tell", "Ask the release notes thread to stop using bullet points.", LOADER, False, ["t3_tell"]),
    ("tell", "Have it stop printing debug logs.", LOADER, False, ["t3_tell"]),
    ("two", "Stop the sprite batching thread, and ask the release notes thread to add a date.", None, False,
     ["t3_stop", "t3_tell"]),
    ("two", "Start a thread to fix the menu music and another to tidy the save code.", None, False,
     ["t3_start", "t3_start"]),
    ("two", "Deny that request and tell it to try a smaller change.", LOADER, True, ["t3_deny", "t3_tell"]),
    ("one", "Could you tell the shader cache thread, uh, to keep the old cache around?", LOADER, False, ["t3_tell"]),
    ("ask", "Did it stop on its own?", LOADER, False, ["t3_ask"]),
    ("ask", "Is it still stopped?", LOADER, False, ["t3_ask"]),
]
# Turns that must reach no thread at all, from what an unsure action once did: a stray "Yes, that one." with no question
# open read as a message at 0.32 (docs/voice/spoken-acts.md).
CONTROLS = [
    ("none", "Yes, that one.", LOADER, False, []),
    ("none", "Okay, thanks.", LOADER, False, []),
    ("none", "Stop, I was talking to someone else.", LOADER, False, []),
    ("none", "Sure, sounds right to me.", LOADER, False, []),
    ("none", "Hmm, let me think about it.", LOADER, False, []),
]
SETS = ("seen", "held", "fresh", "review", "late", "controls")
CASES = [(*case, name) for name, cases in zip(SETS, (SEEN, HELD, FRESH, REVIEW, LATE, CONTROLS)) for case in cases]


def key(body: dict) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode("utf-8")).hexdigest()


def revisions(utterance: str) -> list[TranscriptUpdate]:
    """The utterance as a live turn's transcript arrives: a partial every `STEP` words, then the final transcript."""
    tokens = utterance.split(" ")
    partials = [" ".join(tokens[:k]) for k in range(STEP, len(tokens), STEP)]
    return [TranscriptUpdate(i + 1, text, False) for i, text in enumerate(partials)] + [
        TranscriptUpdate(len(partials) + 1, utterance, True)]


def decide(client, utterance: str, recent: str | None, waiting: bool) -> list[str]:
    """One live turn of `utterance`. Returns the tool of every thread task it dispatched or will once the task it
    waits on is done, sorted, an unsure one as `t3_ask`, and every other tool it dispatched over the registry's bar."""
    tools = T3Registry(registry.build(None, None, None), lambda: [APPROVAL] if waiting else [])
    segmenter = run.segmenter_for("hybrid", client, "whole", tools)
    segmenter.conversation_mode = True
    segmenter.orch.gate, segmenter.orch.side_effects_wait_for_final = PLAN_A, True
    segmenter.prior_resolved_tasks = run.with_thread((), recent)
    for update in revisions(utterance):
        if update.is_final:
            run.close_with_decider(segmenter, update)
        else:
            segmenter.update(update)
    dispatched = {d["task_id"] for d in segmenter.dispatch_log}
    # A task that waits on another runs when that one finishes, and a thread task finishes as it is answered.
    waiting = [r for r in segmenter.records() if r["status"] == "ready" and r["depends_on"]]
    while ready := [r for r in waiting if set(r["depends_on"]) <= dispatched and r["task_id"] not in dispatched]:
        dispatched |= {r["task_id"] for r in ready}
    routed = []
    for record in segmenter.records():
        tool = record.get("tool") or {}
        # A superseded task was sent for a fragment of a longer request, and nothing it would say is said.
        if record["task_id"] not in dispatched or record["status"] == "superseded" or not tool.get("tool"):
            continue
        sure = (tool.get("confidence") or 0) >= registry.TOOL_THRESHOLD
        if tool["tool"] in T3:
            # What the task service then does with the pick: its own act, or a lookup. Code before `acts_on` turned an
            # unsure act into a lookup in the record itself, so the margin alone decides there.
            if ACTS_ON is not None:
                sure = ACTS_ON(registry.ToolChoice(tool["tool"], tool.get("confidence") or 0), record["side_effect"],
                               registry.TOOL_THRESHOLD)
            routed.append(tool["tool"] if sure else "t3_ask")
        elif sure and tool["tool"] != registry.NONE:
            routed.append(tool["tool"])
    return sorted(routed)


def passes(want: list[str], routed: list[str]) -> bool:
    return sorted(want) == routed


class Recorded:
    """The decider, answering each request from one kept sample of one case, and only a request it kept."""

    def __init__(self, answers: dict, row: dict, sample: int):
        self.answers = answers.get(f"{row['set']}:{row['utterance']}", {}).get(str(sample), {})

    def call(self, route, body, label=""):
        answer = self.answers.get(key(body))
        if answer is None:
            raise KeyError("no recorded answer for this request: re-run voice.t3.eval.record_whole_requests")
        return {"status": 200, "response": answer, "timing": {"ms": 0.0, "started_at": ""}}


def replayed(answers: dict, row: dict) -> list[list[str]]:
    return [decide(Recorded(answers, row, n), row["utterance"], row["recent"], row["waiting"])
            for n in range(len(row["routed"]))]


class Sampling:
    """The decider with every call fresh, each answer kept under its case, the sample number and the hash of its
    request. Two cases can send the same partial, so a sample's answers are the case's own."""

    def __init__(self, client, answers: dict, case: str, sample: int):
        self.client, self.answers, self.sample = client, answers.setdefault(case, {}), str(sample)

    def call(self, route, body, label=""):
        record = self.client.call(route, body, label=label, fresh=True, cache_record=False)
        if record["status"] == 200:
            self.answers.setdefault(self.sample, {})[key(body)] = record["response"]
        return record

    def __getattr__(self, name):
        return getattr(self.client, name)


def summary(rows: list[dict]) -> list[str]:
    lines = []
    for name in SETS:
        for group in ("one", "two", "tell", "ask", "stop", "mixed", "none"):
            chosen = [r for r in rows if r["set"] == name and r["group"] == group]
            if chosen:
                right = sum(s == sorted(r["want"]) for r in chosen for s in r["routed"])
                lines.append(f"{name} {group}: {right} of {sum(len(r['routed']) for r in chosen)}")
    return lines


def main() -> int:
    from voice.liquid_decider.client import LiquidClient

    parser = argparse.ArgumentParser()
    parser.add_argument("--before", action="store_true", help="keep the routings only, under `before`")
    parser.add_argument("--samples", type=int, default=3)
    parser.add_argument("--missing", action="store_true", help="run only the cases with no row yet, and keep the rest")
    parser.add_argument("--replay", action="store_true", help="route the kept answers again with no call")
    args = parser.parse_args()
    stored = json.loads(FIXTURE.read_text(encoding="utf-8")) if FIXTURE.exists() else {}
    if args.replay:
        stored["rows"] = [{**row, "routed": replayed(stored["answers"], row)} for row in stored["rows"]]
        FIXTURE.write_bytes((json.dumps(stored, separators=(",", ":")) + "\n").encode("utf-8"))
        print("\n".join(summary(stored["rows"])))
        return 0
    section = "before" if args.before else "rows"
    kept = {(r["utterance"], r["set"]): r for r in stored.get(section, [])} if args.missing else {}
    answers, rows = dict(stored.get("answers", {})) if args.missing and not args.before else {}, []
    client = LiquidClient(max_attempts=3)
    for group, utterance, recent, waiting, want, name in CASES:
        if (utterance, name) in kept:
            rows.append(kept[utterance, name])
            continue
        routed = [decide(Sampling(client, answers, f"{name}:{utterance}", n), utterance, recent, waiting)
                  for n in range(args.samples)]
        rows.append({"group": group, "utterance": utterance, "recent": recent, "waiting": waiting, "want": want,
                     "set": name, "routed": routed})
        print(json.dumps(rows[-1]), flush=True)
    if args.before:
        stored["before"] = rows
    else:
        stored.update(rows=rows, answers=answers)
    FIXTURE.write_bytes((json.dumps(stored, separators=(",", ":")) + "\n").encode("utf-8"))
    print("\n".join(summary(rows)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
