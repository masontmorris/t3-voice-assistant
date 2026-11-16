"""The mock task service: one in-memory home for the segmenter's task records and their mock executions.

The segmenter owns the records and their dedup, including the voice model's delegate calls, which go through
`submit_delegate`. This service watches the dispatch log. A dispatched read-only task runs on a mock worker that
finishes after `delay_s` with a canned result. A dispatched side-effect task is held and never runs, and so is a
task no decider answer cleared, such as a voice delegate that matched no segment. Nothing here calls a real agent,
sends a message, or touches a device.

Times are seconds on the caller's clock, passed in, so the service is deterministic under test.
"""

from dataclasses import dataclass
from enum import StrEnum

from voice.prototype.segmenter import Segmenter


class Run(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    HELD = "held_for_confirmation"
    CANCELLED = "cancelled"


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


def canned_result(instruction):
    text = instruction.lower()
    return next((result for words, result in CANNED if all(w in text for w in words)), DEFAULT_RESULT)


def needs_hold(record):
    """Side effects are held. So is a task the decider never classified, such as an unmatched voice delegate."""
    return record["side_effect"] or "orchestrator" not in record["sources"]


@dataclass
class Execution:
    task_id: str
    state: Run
    dispatched_at: float
    due_at: float | None
    result_summary: str | None = None
    finished_at: float | None = None
    reported: bool = False


def snapshot_status(record, execution):
    """The status the voice model hears for one task record, or None for a task that never ran."""
    status = record["status"]
    if status in ("proposed", "ready"):
        return "pending"
    if status == "needs_clarification":
        return "needs_clarification"
    if status in ("superseded", "withdrawn"):
        return None
    return execution.state.value


def build_snapshot(records, executions, conversation_id, utterance_id, final_transcript):
    """The plan's `utterance_snapshot`, from task records alone. It reports only what the records say."""
    tasks = []
    for record in records:
        execution = executions.get(record["task_id"])
        status = snapshot_status(record, execution)
        if status is None:
            continue
        entry = {"task_id": record["task_id"], "description": record["instruction"], "status": status}
        if record["depends_on"]:
            entry["depends_on"] = record["depends_on"]
        if status == Run.COMPLETED:
            entry["result_summary"] = execution.result_summary
        tasks.append(entry)
    unclear = [r["instruction"] for r in records if r["status"] == "needs_clarification"]
    return {"type": "utterance_snapshot", "conversation_id": conversation_id, "utterance_id": utterance_id,
            "final_transcript": final_transcript, "tasks": tasks, "clarification_needed": unclear or None}


class MockTaskService:
    def __init__(self, segmenter: Segmenter, conversation_id, delay_s=4.0):
        self.segmenter, self.conversation_id, self.delay_s = segmenter, conversation_id, delay_s
        self.executions: dict[str, Execution] = {}
        self._seen = 0

    def submit_delegate(self, instruction):
        """The voice model's delegate tool. The segmenter reconciles it with its own records."""
        return self.segmenter.submit_delegate(instruction)

    def sync(self, now):
        """Start executions for new dispatches and cancel running ones the segmenter asked to cancel."""
        events = []
        records = {r["task_id"]: r for r in self.segmenter.records()}
        for entry in self.segmenter.dispatch_log[self._seen:]:
            task_id = entry["task_id"]
            if needs_hold(records[task_id]):
                execution = Execution(task_id, Run.HELD, now, None)
            else:
                execution = Execution(task_id, Run.RUNNING, now, now + self.delay_s)
            self.executions[task_id] = execution
            events.append({"task_id": task_id, "event": execution.state.value, "instruction": entry["instruction"]})
        self._seen = len(self.segmenter.dispatch_log)
        for task_id, execution in self.executions.items():
            if records[task_id]["status"] == "cancel_requested" and execution.state in (Run.RUNNING, Run.HELD):
                execution.state, execution.finished_at = Run.CANCELLED, now
                events.append({"task_id": task_id, "event": Run.CANCELLED.value})
        return events

    def poll(self, now):
        """Finish every running execution that is due. Returns the task ids that finished."""
        done = []
        records = {r["task_id"]: r for r in self.segmenter.records()}
        for task_id, execution in self.executions.items():
            if execution.state == Run.RUNNING and execution.due_at <= now:
                execution.state, execution.finished_at = Run.COMPLETED, now
                execution.result_summary = canned_result(records[task_id]["instruction"])
                done.append(task_id)
        return done

    def next_due(self):
        due = [e.due_at for e in self.executions.values() if e.state == Run.RUNNING]
        return min(due) if due else None

    def snapshot(self, utterance_id, final_transcript):
        """Build the snapshot. Every completion it carries counts as told, so no later update repeats it."""
        snapshot = build_snapshot(self.segmenter.records(), self.executions, self.conversation_id, utterance_id,
                                  final_transcript)
        for entry in snapshot["tasks"]:
            if entry["status"] == Run.COMPLETED:
                self.executions[entry["task_id"]].reported = True
        return snapshot

    def take_updates(self):
        """A `task_update` for each completion the voice has not been told about yet, each exactly once."""
        records = {r["task_id"]: r for r in self.segmenter.records()}
        updates = []
        for task_id, execution in self.executions.items():
            if execution.state == Run.COMPLETED and not execution.reported:
                execution.reported = True
                updates.append({"type": "task_update", "conversation_id": self.conversation_id, "task_id": task_id,
                                "description": records[task_id]["instruction"], "status": execution.state.value,
                                "result_summary": execution.result_summary})
        return updates
