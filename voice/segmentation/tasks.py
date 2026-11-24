"""Task records, their state machine, and the deterministic logic that applies decisions to them.

The decider only classifies segments. Everything that changes a task (binding segments to tasks across revisions,
holding side effects until the final transcript, superseding on correction, linking delegate submissions) happens here.
Dispatch is mocked: a dispatched task is recorded in the dispatch log and nothing runs.
"""

import re
from dataclasses import dataclass, field, replace
from enum import StrEnum

from voice.segmentation.contract import CorrectionMode, Kind, Readiness
from voice.tools.registry import T3_ANSWER, T3_TOOLS, ToolChoice


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
    Status.DISPATCHED: {Status.CANCEL_REQUESTED, Status.SUPERSEDED},
    Status.SUPERSEDED: set(),
    Status.CANCEL_REQUESTED: set(),
}
SNAPSHOT_STATUS = {
    Status.PROPOSED: "pending",
    Status.READY: "pending",
    Status.NEEDS_CLARIFICATION: "needs_clarification",
    Status.DISPATCHED: "running",
    Status.CANCEL_REQUESTED: "cancel_requested",
    Status.HELD: "pending",
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
    tool: ToolChoice | None = None
    waiting: dict | None = None
    history: list[dict] = field(default_factory=list)

    def record(self):
        record = {"task_id": self.task_id, "status": self.status.value, "segment_text": self.segment_text,
                  "instruction": self.instruction, "sources": sorted(self.sources), "depends_on": self.depends_on,
                  "revises": self.revises, "side_effect": self.side_effect, "history": self.history}
        if self.tool is not None:
            record["tool"] = {"tool": self.tool.tool, "confidence": self.tool.confidence,
                              "arguments": self.tool.arguments}
        if self.waiting:
            record["waiting"] = self.waiting
        return record


def _words(text):
    return re.findall(r"[a-z0-9']+", text.lower())


def similarity(a, b):
    """Share of the shorter text's words that appear in the other. Tolerates growth and punctuation changes."""
    return overlap(set(_words(a)), set(_words(b)))


def overlap(wa, wb):
    return len(wa & wb) / min(len(wa), len(wb)) if wa and wb else 0.0


def content_words(text):
    return {w for w in _words(text or "") if w not in STOPWORDS}


def _answers(decision):
    return decision.tool is not None and decision.tool.tool == T3_ANSWER


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
        self.waits = None
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

    def rejoin(self, start):
        """The segment at `start` was only the start of a longer request. A task already sent for it answered the
        fragment, so it is superseded, which stops its run and drops what it would say, and it binds to no segment: the
        whole request is read as a new task. A task not sent yet keeps its binding and takes the longer words."""
        task = next((t for t in self.tasks.values() if t.segment_start == start and t.status == Status.DISPATCHED),
                    None)
        if task is not None:
            self._move(task, Status.SUPERSEDED, "the request it was sent for went on in the next words")
            task.segment_start = None

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

        It duplicates a live task when the two share at least 60% of the shorter one's content words, it brings no
        content word from the unreviewed tail that the task lacks, and it does not claim a side effect the task lacks.
        So "delete the report" never links to "find the report", while a read-only claim about a side-effect task
        links and leaves its hold in place. A duplicate links to that task. With `run_linked`, a linked task that has
        not run dispatches, because the voice model asked for it. Anything else becomes a new task and dispatches;
        the task service holds it if it is a side effect and the service holds side effects."""
        words, tail_words = content_words(instruction), content_words(tail)
        best = None
        for task in self.tasks.values():
            theirs = content_words(task.instruction)
            if task.status not in LIVE or (side_effect and not task.side_effect) or (words & tail_words) - theirs:
                continue
            share = overlap(words, theirs)
            if share >= MATCH_THRESHOLD and (best is None or share > best[0]):
                best = (share, task)
        if best is not None:
            task = best[1]
            task.sources.add("lfm")
            self._log(task, "linked_voice_delegate", instruction=instruction)
            if run_linked and task.status in UNDISPATCHED:
                self._move(task, Status.DISPATCHED, "voice model delegated it")
            return {"ok": True, "task_id": task.task_id, "duplicate_of": task.task_id}
        task = self._new_task(instruction, None, {"lfm"}, side_effect=side_effect)
        self._move(task, Status.DISPATCHED, "voice model delegated it")
        return {"ok": True, "task_id": task.task_id}

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
        A `finished` task keeps its result, and the new task runs beside it."""
        task = self.tasks.get(task_id)
        if task is None:
            return {"ok": False, "error": f"no task {task_id}"}
        if task.status not in LIVE:
            return {"ok": False, "error": f"{task_id} is already {task.status.value}"}
        if task.status in UNDISPATCHED:
            self._move(task, Status.SUPERSEDED, "voice model amended it")
        elif not finished:
            self._move(task, Status.CANCEL_REQUESTED, "voice model amended it")
        new = self._new_task(request, None, {"lfm"}, revises=task_id, depends_on=list(task.depends_on),
                             side_effect=task.side_effect)
        new.instruction = f"{task.instruction} (corrected: {request})"
        self._move(new, Status.READY if new.depends_on else Status.DISPATCHED, "voice model amended its task")
        return {"ok": True, "task_id": new.task_id, "replaces": task_id}

    def take_back_utterance(self, reason):
        """Stop every live task this utterance made, because the whole utterance corrects an earlier turn: a task
        that has not run is withdrawn, and a running one is cancelled."""
        for task in self.tasks.values():
            if task.status in UNDISPATCHED:
                self._move(task, Status.WITHDRAWN, reason)
            elif task.status == Status.DISPATCHED:
                self._move(task, Status.CANCEL_REQUESTED, reason)

    def amend_earlier(self, ref, instruction, side_effect):
        """A task that replaces one an earlier turn ran. `ref` names that task, which this turn does not hold."""
        task = self._new_task(instruction, None, {"orchestrator"}, revises=ref, side_effect=side_effect)
        self._move(task, Status.DISPATCHED, "amends an earlier turn's task")
        return task

    def answer_earlier(self, ref, instruction, side_effect):
        """A task the person has now answered: the request that could not start runs with the value they gave. It
        replaces the waiting task, so a later turn carries one task and the snapshot reports one."""
        task = self._new_task(instruction, None, {"orchestrator"}, revises=ref, side_effect=side_effect)
        self._move(task, Status.DISPATCHED, "the person answered what it lacked")
        return task

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
            task.side_effect = decision.side_effect or _answers(decision) or (
                task.revises is not None and self.tasks[task.revises].side_effect)
            task.tool = decision.tool
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
        task.side_effect = decision.side_effect or _answers(decision) or target.side_effect
        task.tool = decision.tool
        bound[seg.sid] = task

    def _settle(self, task, seg, decision):
        """Pick the status of an undispatched task from the decision and the dispatch policy.

        `waits` is the caller's check for a call that has nothing to look up, such as a forecast with no place. It
        returns what the task would have to ask the person (tool, arguments, missing fields) or None, and a task it
        answers for waits for that answer (`NEEDS_CLARIFICATION`) however ready it reads."""
        task.waiting = self.waits(task) if self.waits is not None else None
        if task.waiting:
            status, reason = Status.NEEDS_CLARIFICATION, f"needs {', '.join(task.waiting['missing'])}"
        elif decision.readiness == Readiness.CLARIFY or (decision.readiness == Readiness.WAIT and self.is_final):
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
            verdict = self.gate(decision, self.is_final)
            self._log(task, "gate", passed=verdict.passed, score=round(verdict.score, 4), weakest=verdict.weakest,
                      threshold=verdict.threshold)
            status, reason = (Status.DISPATCHED if verdict.passed else Status.HELD), verdict.reason()
        else:
            status, reason = Status.DISPATCHED, "ready"
        if (status != Status.DISPATCHED and self.is_final and not seg.open and not task.depends_on
                and task.tool is not None and task.tool.tool in T3_TOOLS):
            # Parked, a thread request is answered by the voice alone, unchecked, so T3 asks back itself. It keeps its
            # act however sure the pick: every act but a lookup is read back and confirmed before anything is sent, so
            # an unsure "tell it to ..." is asked about as that message, not answered with the thread's status.
            status, reason = Status.DISPATCHED, f"{reason}; T3 asks back"
        self._move(task, status, reason)

    def snapshot(self, conversation_id, utterance_id):
        tasks = []
        for t in self.tasks.values():
            if t.status in SNAPSHOT_STATUS:
                entry = {"task_id": t.task_id, "description": t.instruction, "status": SNAPSHOT_STATUS[t.status]}
                if t.depends_on:
                    entry["depends_on"] = t.depends_on
                if t.waiting:
                    entry["needs"] = list(t.waiting["missing"])
                tasks.append(entry)
        unclear = [t.instruction for t in self.tasks.values() if t.status == Status.NEEDS_CLARIFICATION]
        return {"type": "utterance_snapshot", "conversation_id": conversation_id, "utterance_id": utterance_id,
                "final_transcript": self.text, "tasks": tasks, "clarification_needed": unclear or None}

    def task_records(self):
        return [t.record() for t in self.tasks.values()]
