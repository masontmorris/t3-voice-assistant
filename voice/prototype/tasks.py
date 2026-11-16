"""The mock task service: one in-memory home for the segmenter's task records and their mock executions.

The segmenter owns the records and their dedup, including the voice model's delegate calls, which go through
`submit_delegate`. Under the end-of-utterance handoff the voice model's tools go through `call_tool` instead, which
checks each call against the records before the orchestrator acts on it. This service watches the dispatch log,
so delegated and segmented tasks take the same path. A dispatched read-only task that a tool in `tools`
(`voice/tools/registry.py`) accepts runs that real lookup on a worker thread, and finishes when it returns:
completed, or failed with the reason as its summary. Any other read-only task runs on a mock worker that finishes
after `delay_s` with a canned result. A dispatched side-effect task is held and never runs, and so is a task no
decider answer cleared, such as a voice delegate that matched no segment. Nothing here calls a real agent, sends a
message, or touches a device.

Times are seconds on the caller's clock, passed in, so the service is deterministic under test.
"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from enum import StrEnum

from voice.live.facts import facts_in
from voice.prototype.segmenter import Segmenter


class Run(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    HELD = "held_for_confirmation"
    CANCELLED = "cancelled"
    FAILED = "failed"


CANNED = [
    (("weather",), "Mock forecast: sunny, high of 54 degrees."),
    (("rain",), "Mock forecast: 40 percent chance of rain on Saturday."),
    (("sales", "report"), "Mock file search: found Sales_Week45.xlsx."),
    (("flight",), "Mock fare search: the cheapest flight is 289 dollars."),
    (("calendar",), "Mock calendar: two meetings that day, at 10 and at 2."),
    (("drive",), "Mock route: about 35 minutes by car."),
    (("tokyo",), "Mock clock: it is 9:15 in the morning in Tokyo."),
    (("slides",), "Mock file search: found Quarterly_Review.pptx."),
]
DEFAULT_RESULT = "Mock worker finished. No real work was done."
ASK_WHAT_CHANGED = {"status": "needs_clarification",
                    "ask": "The user says the last answer was not what they meant. Nothing new has started. Ask what "
                           "they want changed."}
HUNG_LOOKUP_GRACE_S = 1.0


def canned_result(instruction):
    text = instruction.lower()
    return next((result for words, result in CANNED if all(w in text for w in words)), DEFAULT_RESULT)


def amended(instruction, replacement):
    """`instruction` with its day, date, time, number or place changed to the one `replacement` states, plus the old
    and new values. None when `replacement` states no such value, or `instruction` has none of that kind or the same
    one, since then there is nothing to change and the turn asks instead."""
    new = facts_in(replacement, worded_numbers=True) or [f for f in facts_in(f"in {replacement}") if f.kind == "place"]
    old_facts = facts_in(instruction, worded_numbers=True)
    for fact in new:
        old = next((f for f in old_facts if f.kind == fact.kind), None)
        if old is not None and old.key != fact.key:
            return instruction.replace(old.surface, fact.surface, 1), old.surface, fact.surface
    return None


def needs_hold(record):
    """Side effects are held. So is a task nobody classified: a voice delegate that matched no segment.

    A handoff delegate (source `lfm`) carries the voice model's own side-effect flag, so it runs when that says
    read-only."""
    return record["side_effect"] or not {"orchestrator", "lfm"} & set(record["sources"])


@dataclass
class Execution:
    task_id: str
    state: Run
    dispatched_at: float
    due_at: float | None
    result_summary: str | None = None
    finished_at: float | None = None
    reported: bool = False
    tool: dict | None = None


def snapshot_status(record, execution):
    """The status the voice model hears for one task record, or None for a task that never ran."""
    status = record["status"]
    if status in ("proposed", "ready"):
        return "pending"
    if status == "needs_clarification":
        return "needs_clarification"
    if status == "held":
        return "pending"
    if status in ("superseded", "withdrawn"):
        return None
    return execution.state.value


def not_run_note(record):
    """Why a task has not run, from its history: the gate's weakest answer, or that it was not ready yet."""
    gate = next((h for h in reversed(record["history"]) if h["event"] == "gate"), None)
    if gate is not None and not gate["passed"]:
        return (f"Not started: the service's confidence in its {gate['weakest']} answer was {gate['score']:.2f}, "
                f"below {gate['threshold']:.2f}.")
    return "Not started: when the service last read it, it was not ready to run."


def build_snapshot(records, executions, conversation_id, utterance_id, final_transcript, unreviewed=None,
                   notes=False, correction=None):
    """The plan's `utterance_snapshot`, from task records alone. It reports only what the records say.

    `unreviewed` is the end of the transcript that no decision point read. With `notes`, each task that has not run
    says why, for the gated turn's tools session. `correction` is what the turn did with a correction of an earlier
    turn, when the utterance was one."""
    tasks = []
    for record in records:
        execution = executions.get(record["task_id"])
        status = snapshot_status(record, execution)
        if status is None:
            continue
        entry = {"task_id": record["task_id"], "description": record["instruction"], "status": status}
        if record["depends_on"]:
            entry["depends_on"] = record["depends_on"]
        if status in (Run.COMPLETED, Run.FAILED):
            entry["result_summary"] = execution.result_summary
        if notes and status in ("pending", Run.HELD) and execution is None:
            entry["note"] = not_run_note(record)
        tasks.append(entry)
    unclear = [r["instruction"] for r in records if r["status"] == "needs_clarification"]
    snapshot = {"type": "utterance_snapshot", "conversation_id": conversation_id, "utterance_id": utterance_id,
                "final_transcript": final_transcript, "tasks": tasks, "clarification_needed": unclear or None}
    if unreviewed is not None:
        snapshot["unreviewed_speech"] = unreviewed
    if correction is not None:
        snapshot["correction"] = correction
    return snapshot


class MockTaskService:
    """`holds_side_effects` off is the gated turn: the gate and the voice model decide what runs, so a dispatched side
    effect runs on the mock worker instead of waiting for confirmation. A side effect never reaches a real tool."""

    def __init__(self, segmenter: Segmenter, conversation_id, delay_s=4.0, tools=None, holds_side_effects=True):
        self.segmenter, self.conversation_id, self.delay_s, self.tools = segmenter, conversation_id, delay_s, tools
        self.holds_side_effects = holds_side_effects
        self.executions: dict[str, Execution] = {}
        self.correction = None
        self._seen = 0
        self._calls = {}
        self._pool = ThreadPoolExecutor(max_workers=4) if tools else None

    def _start(self, task_id, instruction, side_effect, now):
        call = self.tools.route(instruction) if self.tools and not side_effect else None
        if call is None:
            return Execution(task_id, Run.RUNNING, now, now + self.delay_s)
        self._calls[task_id] = self._pool.submit(self.tools.run, call)
        return Execution(task_id, Run.RUNNING, now, now + self.tools.timeout_s + HUNG_LOOKUP_GRACE_S,
                         tool={"tool": call.tool, "arguments": call.arguments})

    def submit_delegate(self, instruction):
        """The voice model's delegate tool. The segmenter reconciles it with its own records."""
        return self.segmenter.submit_delegate(instruction)

    def sync(self, now):
        """Start executions for new dispatches and cancel running ones the segmenter asked to cancel."""
        events = []
        records = {r["task_id"]: r for r in self.segmenter.records()}
        for entry in self.segmenter.dispatch_log[self._seen:]:
            task_id = entry["task_id"]
            record = records[task_id]
            if self.holds_side_effects and needs_hold(record):
                execution = Execution(task_id, Run.HELD, now, None)
            else:
                execution = self._start(task_id, entry["instruction"], record["side_effect"], now)
            self.executions[task_id] = execution
            events.append({"task_id": task_id, "event": execution.state.value, "instruction": entry["instruction"]})
        self._seen = len(self.segmenter.dispatch_log)
        for task_id, execution in self.executions.items():
            if records[task_id]["status"] == "cancel_requested" and execution.state in (Run.RUNNING, Run.HELD):
                execution.state, execution.finished_at = Run.CANCELLED, now
                events.append({"task_id": task_id, "event": Run.CANCELLED.value})
        return events

    def poll(self, now):
        """Finish every running execution that is due or whose lookup returned. Returns the task ids that finished."""
        done = []
        records = {r["task_id"]: r for r in self.segmenter.records()}
        for task_id, execution in self.executions.items():
            if execution.state != Run.RUNNING:
                continue
            call = self._calls.get(task_id)
            if call is not None and call.done():
                result = call.result()
                execution.state = Run.COMPLETED if result.outcome == "ok" else Run.FAILED
                execution.result_summary, execution.tool = result.summary, result.record()
            elif call is None and execution.due_at <= now:
                execution.state = Run.COMPLETED
                execution.result_summary = canned_result(records[task_id]["instruction"])
            elif execution.due_at <= now:
                execution.state, execution.tool = Run.FAILED, {**execution.tool, "outcome": "timeout"}
                execution.result_summary = f"The {execution.tool['tool']} lookup did not answer in time."
            else:
                continue
            execution.finished_at = now
            done.append(task_id)
        return done

    def pending_calls(self):
        """The lookups still running, as futures a caller can wait on."""
        return [self._calls[task_id] for task_id, e in self.executions.items()
                if e.state == Run.RUNNING and task_id in self._calls]

    def close(self):
        if self._pool is not None:
            self._pool.shutdown(wait=False, cancel_futures=True)

    def next_due(self):
        due = [e.due_at for e in self.executions.values() if e.state == Run.RUNNING]
        return min(due) if due else None

    def correct(self, replacement, prior_tasks, now):
        """The utterance corrects an earlier turn, so nothing it said becomes a new task. With a `replacement` that
        changes a value of the call's newest task, a task with that value replaces it. Otherwise the voice asks what
        to change. Returns the service events."""
        orch = self.segmenter.orch
        orch.take_back_utterance("the utterance corrects an earlier turn")
        target = prior_tasks[-1] if prior_tasks else None
        change = amended(target["instruction"], replacement) if target and replacement else None
        if change is None:
            self.correction = ASK_WHAT_CHANGED
        else:
            task = orch.amend_earlier(target["ref"], change[0], target["side_effect"])
            self.correction = {"status": "amended", "task_id": task.task_id,
                               "changed": {"from": change[1], "to": change[2]}}
        return self.sync(now)

    def call_tool(self, name, arguments, unreviewed, now, new_requests=True):
        """One handoff tool call from the voice model. A call is a request: the result says what actually happened.
        Without `new_requests`, as for chit-chat, a delegate is refused: only a request starts a new task."""
        orch = self.segmenter.orch
        if name == "delegate" and not new_requests:
            result = {"ok": False, "error": "the utterance asks for nothing new"}
        elif name == "delegate" and isinstance(arguments.get("instruction"), str) and arguments["instruction"].strip():
            result = orch.delegate_from_voice(arguments["instruction"].strip(),
                                              arguments.get("side_effect") is not False, unreviewed,
                                              run_linked=not self.holds_side_effects)
        elif name == "run_task" and isinstance(arguments.get("task_id"), str):
            result = orch.run_from_voice(arguments["task_id"])
        elif name == "cancel_task" and isinstance(arguments.get("task_id"), str):
            execution = self.executions.get(arguments["task_id"])
            if execution is not None and execution.state == Run.COMPLETED:
                result = {"ok": False, "error": f"{arguments['task_id']} already completed"}
            else:
                result = orch.cancel_from_voice(arguments["task_id"])
        elif name == "amend_task" and isinstance(arguments.get("task_id"), str) \
                and isinstance(arguments.get("new_request"), str) and arguments["new_request"].strip():
            execution = self.executions.get(arguments["task_id"])
            result = orch.amend_from_voice(arguments["task_id"], arguments["new_request"].strip(),
                                           finished=execution is not None and execution.state == Run.COMPLETED)
        else:
            result = {"ok": False, "error": f"bad call to {name}"}
        events = self.sync(now)
        return result, events

    def snapshot(self, utterance_id, final_transcript, unreviewed=None, notes=False):
        """Build the snapshot. Every completion it carries counts as told, so no later update repeats it."""
        snapshot = build_snapshot(self.segmenter.records(), self.executions, self.conversation_id, utterance_id,
                                  final_transcript, unreviewed, notes, self.correction)
        for entry in snapshot["tasks"]:
            if entry["status"] in (Run.COMPLETED, Run.FAILED):
                self.executions[entry["task_id"]].reported = True
        return snapshot

    def take_updates(self):
        """A `task_update` for each completion the voice has not been told about yet, each exactly once."""
        records = {r["task_id"]: r for r in self.segmenter.records()}
        updates = []
        for task_id, execution in self.executions.items():
            if execution.state in (Run.COMPLETED, Run.FAILED) and not execution.reported:
                execution.reported = True
                updates.append({"type": "task_update", "conversation_id": self.conversation_id, "task_id": task_id,
                                "description": records[task_id]["instruction"], "status": execution.state.value,
                                "result_summary": execution.result_summary})
        return updates
