"""Task records, their state machine, and the deterministic logic that applies decisions to them.

The decider only classifies segments. Everything that changes a task (binding segments to tasks across revisions,
holding side effects until the final transcript, superseding on correction, linking delegate submissions) happens here.
Dispatch is mocked: a dispatched task is recorded in the dispatch log and nothing runs.
"""

import re
from dataclasses import dataclass, field, replace
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
    HELD = "held"


UNDISPATCHED = {Status.PROPOSED, Status.READY, Status.NEEDS_CLARIFICATION, Status.HELD}
TRANSITIONS = {
    Status.PROPOSED: UNDISPATCHED | {Status.DISPATCHED, Status.SUPERSEDED, Status.WITHDRAWN},
    Status.READY: UNDISPATCHED | {Status.DISPATCHED, Status.SUPERSEDED, Status.WITHDRAWN},
    Status.NEEDS_CLARIFICATION: UNDISPATCHED | {Status.DISPATCHED, Status.SUPERSEDED, Status.WITHDRAWN},
    Status.HELD: UNDISPATCHED | {Status.DISPATCHED, Status.SUPERSEDED, Status.WITHDRAWN},
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
    Status.HELD: "held_for_confirmation",
}
LIVE = {Status.PROPOSED, Status.READY, Status.NEEDS_CLARIFICATION, Status.HELD, Status.DISPATCHED}
MATCH_THRESHOLD = 0.6
STOPWORDS = frozenset("a an the to of for in on at and or but is are be it its me my i you your can could would will "
                      "please what what's whats that this with then also just about do does".split())


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
    return overlap(set(_words(a)), set(_words(b)))


def overlap(wa, wb):
    return len(wa & wb) / min(len(wa), len(wb)) if wa and wb else 0.0


def content_words(text):
    return {w for w in _words(text or "") if w not in STOPWORDS}


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
        self.decided: dict[str, tuple] = {}
        self.gate = None
        self.side_effects_wait_for_final = True

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

    def apply(self, update, segments, decisions, raw=None):
        """Apply one decision point. Segments without a complete decision leave their tasks unchanged.

        `raw` holds the answers before any caller-side hold, such as the hybrid's near-tie wait. Each task keeps the
        last of them with the words it was made on, for `close`."""
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
        for seg in segments:
            task, decision = bound.get(seg.sid), (raw or decisions.segments).get(seg.sid)
            if task is not None and decision is not None:
                self.decided[task.task_id] = (decision, tuple(_words(seg.text)))

    def end(self, update):
        """Record that the utterance ended, so later dispatches log as after the final transcript. Settles nothing."""
        self.revision, self.is_final, self.text = update.revision, True, update.text

    def close(self, update, segments):
        """End the utterance with no decision call. A task whose words have not changed since its last decision is
        settled by that decision, as on a final transcript. A task whose words changed is held. No decider read the
        new words, so code cannot tell whether they made it a side effect, as when "and if any are under three
        hundred" grows into "...book the cheapest one"."""
        self.end(update)
        bound = self.bind(segments)
        for seg in segments:
            task = bound.get(seg.sid)
            if task is None or task.status not in UNDISPATCHED or task.task_id not in self.decided:
                continue
            decision, seen = self.decided[task.task_id]
            if tuple(_words(seg.text)) == seen:
                self._settle(task, replace(seg, open=False), decision)
            else:
                self._move(task, Status.HELD, "changed after its last decision")

    def delegate_from_voice(self, instruction, side_effect, tail, run_linked=False):
        """The voice model's delegate call after `close`, reconciled in code.

        It links to every live task whose content words the delegate covers at 60% and whose side effect it does not
        contradict, so a re-delegation of the whole utterance uses the task ids already listed instead of making one
        combined duplicate. "delete the report" still never links to "find the report", and a read-only claim about a
        side-effect task links and leaves its hold in place, so it never runs one. With `run_linked`, a linked task
        that has not run dispatches, because the voice model asked for it. Content words the delegate brings from the
        unreviewed tail that no linked task covers become one new task for the tail, so the words no decision point
        read are not dropped. A delegate that covers no task becomes one new task, as before."""
        words, tail_words = content_words(instruction), content_words(tail)
        linked = [t for t in self.tasks.values()
                  if t.status in LIVE and not (side_effect and not t.side_effect)
                  and overlap(words, content_words(t.instruction)) >= MATCH_THRESHOLD]
        if not linked:
            task = self._new_task(instruction, None, {"lfm"}, side_effect=side_effect)
            self._move(task, Status.DISPATCHED, "voice model delegated it")
            return {"ok": True, "task_ids": [task.task_id], "duplicate_of": []}
        covered = set().union(*(content_words(t.instruction) for t in linked))
        for task in linked:
            task.sources.add("lfm")
            self._log(task, "linked_voice_delegate", instruction=instruction)
            if run_linked and self._delegate_may_run(task, side_effect):
                self._move(task, Status.DISPATCHED, "voice model delegated it")
        ids = [t.task_id for t in linked]
        if (words & tail_words) - covered:
            new = self._new_task(tail.strip(), None, {"lfm"}, side_effect=side_effect)
            self._move(new, Status.DISPATCHED, "voice model delegated the words no task covered")
            ids.append(new.task_id)
        return {"ok": True, "task_ids": ids, "duplicate_of": [t.task_id for t in linked]}

    def _delegate_may_run(self, task, side_effect):
        """A linked task runs on a delegate only when it has not run, waits on nothing, and the delegate's claim does
        not contradict it. A task the service left for clarification stays there, because a restatement adds no
        answer, and a task waiting on a dependency waits for it."""
        return (task.status in UNDISPATCHED - {Status.NEEDS_CLARIFICATION} and not task.depends_on
                and (not task.side_effect or side_effect))

    def run_from_voice(self, task_id):
        """The voice model's run_task call: a task that has not run dispatches now."""
        task = self.tasks.get(task_id)
        if task is None:
            return {"ok": False, "error": f"no task {task_id}"}
        if task.status not in UNDISPATCHED:
            return {"ok": False, "error": f"{task_id} is already {task.status.value}"}
        self._move(task, Status.DISPATCHED, "voice model ran it")
        return {"ok": True, "task_id": task_id}

    def cancel_from_voice(self, task_id):
        """The voice model's cancel_task call. Only a live task can be cancelled."""
        task = self.tasks.get(task_id)
        if task is None:
            return {"ok": False, "error": f"no task {task_id}"}
        if task.status in UNDISPATCHED:
            self._move(task, Status.WITHDRAWN, "voice model cancelled it")
        elif task.status == Status.DISPATCHED:
            self._move(task, Status.CANCEL_REQUESTED, "voice model cancelled it")
        else:
            return {"ok": False, "error": f"{task_id} is already {task.status.value}"}
        return {"ok": True, "task_id": task_id}

    def amend_from_voice(self, task_id, request, finished=False):
        """The voice model's amend_task call. A new task revises the old one, which is superseded or cancelled.
        A `finished` task keeps its result, and the new task runs beside it. A task the decider withdrew during
        speech can be amended, and so can a superseded one, which amends its newest successor: that is how a
        correction made before the end of the utterance gets linked to the words that replaced it."""
        task = self.tasks.get(task_id)
        if task is None:
            return {"ok": False, "error": f"no task {task_id}"}
        if task.status == Status.SUPERSEDED:
            task_id = self._successor(task_id)
            task = self.tasks[task_id]
        if task.status not in LIVE | {Status.WITHDRAWN}:
            return {"ok": False, "error": f"{task_id} is already {task.status.value}"}
        if task.status in UNDISPATCHED:
            self._move(task, Status.SUPERSEDED, "voice model amended it")
        elif task.status != Status.WITHDRAWN and not finished:
            self._move(task, Status.CANCEL_REQUESTED, "voice model amended it")
        new = self._new_task(request, None, {"lfm"}, revises=task_id, depends_on=list(task.depends_on),
                             side_effect=task.side_effect)
        new.instruction = f"{task.instruction} (corrected: {request})"
        self._move(new, Status.READY if new.depends_on else Status.DISPATCHED, "voice model amended its task")
        return {"ok": True, "task_id": new.task_id, "replaces": task_id}

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
        elif task.side_effect and not self.is_final and self.side_effects_wait_for_final:
            status, reason = Status.READY, "side effect held for the final transcript"
        elif self.gate is not None:
            verdict = self.gate(decision)
            self._log(task, "gate", passed=verdict.passed, score=round(verdict.score, 4), weakest=verdict.weakest,
                      threshold=verdict.threshold)
            status, reason = (Status.DISPATCHED if verdict.passed else Status.HELD), verdict.reason()
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
