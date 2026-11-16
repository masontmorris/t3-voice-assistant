"""Task records, their state machine, and the deterministic logic that applies decisions to them.

The decider only classifies segments. Everything that changes a task (binding segments to tasks across revisions,
holding side effects until the final transcript, superseding on correction, linking delegate submissions) happens here.
Dispatch is mocked: a dispatched task is recorded in the dispatch log and nothing runs.
"""

import re
from dataclasses import dataclass, field
from enum import StrEnum

from voice.segmentation.contract import CorrectionMode, Kind, Readiness


class Status(StrEnum):
    PROPOSED = "proposed"
    READY = "ready"
    NEEDS_CLARIFICATION = "needs_clarification"
    DISPATCHED = "dispatched"
    SUPERSEDED = "superseded"
    WITHDRAWN = "withdrawn"
    CANCEL_REQUESTED = "cancel_requested"


UNDISPATCHED = {Status.PROPOSED, Status.READY, Status.NEEDS_CLARIFICATION}
TRANSITIONS = {
    Status.PROPOSED: UNDISPATCHED | {Status.DISPATCHED, Status.SUPERSEDED, Status.WITHDRAWN},
    Status.READY: UNDISPATCHED | {Status.DISPATCHED, Status.SUPERSEDED, Status.WITHDRAWN},
    Status.NEEDS_CLARIFICATION: UNDISPATCHED | {Status.DISPATCHED, Status.SUPERSEDED, Status.WITHDRAWN},
    Status.WITHDRAWN: UNDISPATCHED,
    Status.DISPATCHED: {Status.CANCEL_REQUESTED},
    Status.SUPERSEDED: set(),
    Status.CANCEL_REQUESTED: set(),
}
SNAPSHOT_STATUS = {
    Status.PROPOSED: "pending",
    Status.READY: "pending",
    Status.NEEDS_CLARIFICATION: "needs_clarification",
    Status.DISPATCHED: "running",
    Status.CANCEL_REQUESTED: "cancel_requested",
}
MATCH_THRESHOLD = 0.6


class IllegalTransition(RuntimeError):
    pass


@dataclass
class Task:
    task_id: str
    segment_text: str
    instruction: str
    segment_start: int | None
    sources: set[str]
    status: Status = Status.PROPOSED
    depends_on: list[str] = field(default_factory=list)
    revises: str | None = None
    side_effect: bool = False
    history: list[dict] = field(default_factory=list)

    def record(self):
        return {"task_id": self.task_id, "status": self.status.value, "segment_text": self.segment_text,
                "instruction": self.instruction, "sources": sorted(self.sources), "depends_on": self.depends_on,
                "revises": self.revises, "side_effect": self.side_effect, "history": self.history}


def _words(text):
    return re.findall(r"[a-z0-9']+", text.lower())


def similarity(a, b):
    """Share of the shorter text's words that appear in the other. Tolerates growth and punctuation changes."""
    wa, wb = set(_words(a)), set(_words(b))
    return len(wa & wb) / min(len(wa), len(wb)) if wa and wb else 0.0


class Orchestrator:
    def __init__(self):
        self.tasks: dict[str, Task] = {}
        self.dispatch_log: list[dict] = []
        self.events: list[dict] = []
        self.revision = 0
        self.is_final = False
        self.text = ""
        self.pending_delegates: dict[str, str] = {}
        self._delegate_count = 0

    def _new_task(self, text, start, sources, **extra):
        task = Task(f"task_{len(self.tasks) + 1}", text, text, start, set(sources), **extra)
        self.tasks[task.task_id] = task
        self._log(task, "created", sources=sorted(sources), segment_text=text)
        return task

    def _log(self, task, event, **detail):
        entry = {"revision": self.revision, "is_final": self.is_final, "event": event, **detail}
        task.history.append(entry)
        self.events.append({"task_id": task.task_id, **entry})

    def _move(self, task, status, reason):
        if status == task.status:
            return
        if status not in TRANSITIONS[task.status]:
            raise IllegalTransition(f"{task.task_id}: {task.status} -> {status}")
        self._log(task, "status", old=task.status.value, new=status.value, reason=reason)
        task.status = status
        if status == Status.DISPATCHED:
            self.dispatch_log.append({"task_id": task.task_id, "revision": self.revision, "is_final": self.is_final,
                                      "instruction": task.instruction, "sources": sorted(task.sources)})

    def _successor(self, task_id):
        seen = set()
        while task_id not in seen:
            seen.add(task_id)
            nxt = next((t.task_id for t in self.tasks.values() if t.revises == task_id), None)
            if nxt is None:
                break
            task_id = nxt
        return task_id

    def bind(self, segments):
        """Map each segment to the task it belonged to at earlier revisions. Unbound undispatched tasks withdraw."""
        unclaimed = [t for t in self.tasks.values() if t.segment_start is not None]
        bound = {}
        for seg in segments:
            task = next((t for t in unclaimed if t.segment_start == seg.start), None)
            if task is None:
                scored = [(similarity(t.segment_text, seg.text), -abs(t.segment_start - seg.start), t) for t in unclaimed]
                scored = [s for s in scored if s[0] >= MATCH_THRESHOLD]
                task = max(scored, key=lambda s: s[:2])[2] if scored else None
            if task is not None:
                unclaimed.remove(task)
                task.segment_start = seg.start
                if task.segment_text != seg.text:
                    self._log(task, "segment_text", old=task.segment_text, new=seg.text)
                    task.segment_text = seg.text
                    if task.status in UNDISPATCHED or task.status == Status.WITHDRAWN:
                        task.instruction = self._instruction_for(task, seg.text)
                bound[seg.sid] = task
        for task in unclaimed:
            if task.status in UNDISPATCHED and "orchestrator" in task.sources:
                self._move(task, Status.WITHDRAWN, "segment no longer in transcript")
        return bound

    def _instruction_for(self, task, text):
        if task.revises is None:
            return text
        return f"{self.tasks[task.revises].instruction} (corrected: {text})"

    def apply(self, update, segments, decisions):
        """Apply one decision point. Segments without a complete decision leave their tasks unchanged."""
        self.revision, self.is_final, self.text = update.revision, update.is_final, update.text
        bound = self.bind(segments)
        by_sid = {s.sid: s for s in segments}
        for did, sid in decisions.delegates.items():
            self._apply_delegate(self.pending_delegates.pop(did), by_sid.get(sid), bound)
        for seg in segments:
            decision = decisions.segments.get(seg.sid)
            if decision is not None:
                self._apply_segment(seg, decision, bound)
        for seg in segments:
            task = bound.get(seg.sid)
            decision = decisions.segments.get(seg.sid)
            if task is not None and decision is not None and task.status in UNDISPATCHED:
                self._settle(task, seg, decision)

    def submit_delegate(self, instruction):
        """The voice model's delegate tool. Reconciled against the transcript at the next decision point."""
        self._delegate_count += 1
        did = f"d{self._delegate_count}"
        self.pending_delegates[did] = instruction
        return did

    def _apply_delegate(self, instruction, seg, bound):
        task = bound.get(seg.sid) if seg is not None else None
        if task is not None and task.status not in {Status.SUPERSEDED, Status.CANCEL_REQUESTED}:
            task.sources.add("voice_delegate")
            self._log(task, "linked_delegate", instruction=instruction)
            if task.status == Status.WITHDRAWN:
                self._move(task, Status.PROPOSED, "voice delegate asked for it")
            if task.status != Status.DISPATCHED:
                self._move(task, Status.DISPATCHED, "voice delegate submitted it")
            return
        if seg is not None:
            task = self._new_task(seg.text, seg.start, {"voice_delegate"})
            bound[seg.sid] = task
        else:
            task = self._new_task(instruction, None, {"voice_delegate"})
        self._move(task, Status.DISPATCHED, "voice delegate submitted it")

    def _apply_segment(self, seg, decision, bound):
        task = bound.get(seg.sid)
        kind = decision.kind
        if kind == Kind.CORRECTION and decision.corrects is not None:
            target = bound.get(decision.corrects.target)
            if target is not None and target is not task:
                self._correct(seg, decision, target, task, bound)
                return
            if decision.corrects.mode == CorrectionMode.CANCEL:
                return
            kind = Kind.REQUEST
        if kind == Kind.REQUEST:
            if task is None:
                task = self._new_task(seg.text, seg.start, {"orchestrator"})
                bound[seg.sid] = task
            elif "orchestrator" not in task.sources:
                task.sources.add("orchestrator")
                self._log(task, "linked_orchestrator")
            if task.status == Status.WITHDRAWN:
                self._move(task, Status.PROPOSED, "reclassified as request")
            task.side_effect = decision.side_effect or (task.revises is not None
                                                        and self.tasks[task.revises].side_effect)
            task.depends_on = self._dependencies(decision, bound, task)
        elif task is not None and task.status in UNDISPATCHED:
            self._move(task, Status.WITHDRAWN, f"reclassified as {kind.value}")

    def _dependencies(self, decision, bound, task):
        dep = bound.get(decision.depends_on) if decision.depends_on else None
        if dep is None or dep is task:
            return []
        dep_id = self._successor(dep.task_id)
        return [] if self.tasks[dep_id].status in {Status.WITHDRAWN, Status.CANCEL_REQUESTED} else [dep_id]

    def _correct(self, seg, decision, target, task, bound):
        if target.status in {Status.SUPERSEDED, Status.CANCEL_REQUESTED} and task is not None:
            return
        replace = decision.corrects.mode == CorrectionMode.REPLACE
        if target.status == Status.DISPATCHED:
            self._move(target, Status.CANCEL_REQUESTED, f"corrected by segment \"{seg.text}\"")
        elif target.status in UNDISPATCHED:
            self._move(target, Status.SUPERSEDED if replace else Status.WITHDRAWN,
                       f"corrected by segment \"{seg.text}\"")
        if not replace or task is not None:
            return
        task = self._new_task(seg.text, seg.start, {"orchestrator"}, revises=target.task_id,
                              depends_on=list(target.depends_on))
        task.instruction = self._instruction_for(task, seg.text)
        task.side_effect = decision.side_effect or target.side_effect
        bound[seg.sid] = task

    def _settle(self, task, seg, decision):
        """Pick the status of an undispatched task from the decision and the dispatch policy."""
        if decision.readiness == Readiness.CLARIFY or (decision.readiness == Readiness.WAIT and self.is_final):
            status, reason = Status.NEEDS_CLARIFICATION, f"readiness {decision.readiness.value}"
        elif decision.readiness == Readiness.WAIT:
            status, reason = Status.PROPOSED, "readiness wait"
        elif task.depends_on:
            status, reason = Status.READY, "waiting on dependency"
        elif seg.open:
            status, reason = Status.PROPOSED, "segment still being spoken"
        elif task.side_effect and not self.is_final:
            status, reason = Status.READY, "side effect held for the final transcript"
        else:
            status, reason = Status.DISPATCHED, "ready"
        self._move(task, status, reason)

    def snapshot(self, conversation_id, utterance_id):
        tasks = []
        for t in self.tasks.values():
            if t.status in SNAPSHOT_STATUS:
                entry = {"task_id": t.task_id, "description": t.instruction, "status": SNAPSHOT_STATUS[t.status]}
                if t.depends_on:
                    entry["depends_on"] = t.depends_on
                tasks.append(entry)
        unclear = [t.instruction for t in self.tasks.values() if t.status == Status.NEEDS_CLARIFICATION]
        return {"type": "utterance_snapshot", "conversation_id": conversation_id, "utterance_id": utterance_id,
                "final_transcript": self.text, "tasks": tasks, "clarification_needed": unclear or None}

    def task_records(self):
        return [t.record() for t in self.tasks.values()]
