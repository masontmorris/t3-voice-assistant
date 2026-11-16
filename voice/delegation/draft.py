"""Prompt drafts: what a task's frontier agent receives, written while the speaker is still talking.

The hybrid segmenter's completion answer starts a draft. A task's draft inputs are its instruction, its
dependencies and its side-effect flag, all read from the task record. A draft is current only while those inputs
still match the record. The author's output is untrusted. `parse_draft` reads it at the boundary, `check` rejects
a draft that names actions or dependencies the record lacks, and `render` takes the side-effect confirmation and
the dependency placeholders from the record, never from the draft.
"""

import re
from dataclasses import dataclass, field
from enum import StrEnum

from voice.segmentation.cursor import THRESHOLDS
from voice.segmentation.tasks import UNDISPATCHED, Status


class DraftStatus(StrEnum):
    DRAFTING = "drafting"
    CURRENT = "current"
    STALE = "stale"
    REJECTED = "rejected"


@dataclass(frozen=True)
class DraftInput:
    """Everything the author sees for one task. Two inputs that compare equal give interchangeable drafts.

    The instruction compares by its lowercased words, because the final transcript re-cases and re-punctuates the
    partials without changing what was asked.
    """
    task_id: str
    words: tuple[str, ...]
    depends_on: tuple[str, ...]
    side_effect: bool
    instruction: str = field(compare=False)
    context: str = field(compare=False)
    revision: int = field(compare=False)


@dataclass(frozen=True)
class Draft:
    instruction: str
    context: str
    constraints: tuple[str, ...]
    success_criteria: tuple[str, ...]
    open_questions: tuple[str, ...]


@dataclass
class PromptDraft:
    """The newest draft for one task, and the input it was written from."""
    source: DraftInput
    status: DraftStatus
    draft: Draft | None = None
    problems: tuple[str, ...] = ()


@dataclass(frozen=True)
class Dispatch:
    task_id: str
    prompt: str
    used_draft: bool
    reason: str


DRAFT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["instruction", "context", "constraints", "success_criteria", "open_questions"],
    "properties": {
        "instruction": {"type": "string"},
        "context": {"type": "string"},
        "constraints": {"type": "array", "items": {"type": "string"}},
        "success_criteria": {"type": "array", "items": {"type": "string"}},
        "open_questions": {"type": "array", "items": {"type": "string"}},
    },
}
LIST_FIELDS = ("constraints", "success_criteria", "open_questions")
# Words that name an action with an effect outside the assistant, grouped so a draft may say "send" for "email".
ACTION_FAMILIES = {
    "message": ("email", "e-mail", "mail", "send", "text", "message", "reply", "forward", "dm"),
    "purchase": ("buy", "purchase", "order", "pay", "checkout"),
    "booking": ("book", "reserve", "schedule"),
    "removal": ("delete", "remove", "cancel", "unsubscribe", "erase"),
    "publish": ("post", "publish", "share", "tweet", "upload"),
    "call": ("call", "phone", "dial"),
}
PLACEHOLDER = re.compile(r"\{\{\s*(task_\d+)\.result\s*\}\}")
CONFIRM = "Confirm with the user before you take any action that has an effect outside this conversation."
DEAD = {Status.SUPERSEDED, Status.WITHDRAWN, Status.CANCEL_REQUESTED}


def placeholder(task_id):
    return "{{" + task_id + ".result}}"


def source_for(task, context="", revision=0):
    return DraftInput(task.task_id, tuple(re.findall(r"[a-z0-9']+", task.instruction.lower())),
                      tuple(task.depends_on), task.side_effect, task.instruction, context, revision)


def completed(point, tasks, finished=THRESHOLDS.finished):
    """Task ids whose segment the hybrid says is finished at this decision point.

    A segment whose end was in doubt carries a `finished` answer, and it is finished when that answer reaches the
    threshold. A segment without one had a certain end (a hard cut, or the end of the final transcript), so it is
    finished. A malformed or failed answer finishes nothing.
    """
    if not point["applied"] or any(p.startswith("decisions http") for p in point["problems"]):
        return []
    broken = {p.split("_", 1)[0] for p in point["problems"]}
    by_start = {t.segment_start: t for t in tasks.values() if t.segment_start is not None and t.status not in DEAD}
    done = []
    for seg in point["segments"]:
        sid, task = seg["sid"], by_start.get(seg["start"])
        if task is None or sid in broken:
            continue
        answer = point["breaks"].get(sid)
        if answer is None or answer["finished"] >= finished:
            done.append(task.task_id)
    return done


def _words(text):
    return set(re.findall(r"[a-z][a-z'-]*", text.lower()))


def _families(text):
    ws = _words(text)
    return {name for name, stems in ACTION_FAMILIES.items() if any(w.startswith(s) for s in stems for w in ws)}


def parse_draft(content):
    """The author's JSON object as a Draft, or None and the problems. Never raises."""
    if not isinstance(content, dict):
        return None, ("not a JSON object",)
    problems = []
    if not isinstance(content.get("instruction"), str) or not content["instruction"].strip():
        problems.append("instruction missing")
    if not isinstance(content.get("context"), str):
        problems.append("context missing")
    lists = {}
    for name in LIST_FIELDS:
        value = content.get(name)
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            problems.append(f"{name} is not a list of strings")
            continue
        lists[name] = tuple(v.strip() for v in value if v.strip())
    if not problems and not lists["success_criteria"]:
        problems.append("success_criteria empty")
    if problems:
        return None, tuple(problems)
    return Draft(content["instruction"].strip(), content["context"].strip(), **lists), ()


def check(draft, task):
    """Problems that keep a parsed draft from dispatch for this task record. Empty means usable."""
    text = " ".join([draft.instruction, draft.context, *draft.constraints, *draft.success_criteria,
                     *draft.open_questions])
    problems = [f"adds a {name} action" for name in sorted(_families(text) - _families(task.instruction))]
    problems += [f"names {ref}, which is not a dependency" for ref in sorted(set(PLACEHOLDER.findall(text)))
                 if ref not in task.depends_on]
    return tuple(problems)


def _bullets(title, items):
    return [f"{title}:", *(f"- {item}" for item in items)] if items else []


def render(task, draft=None):
    """The prompt the frontier agent receives. Dependencies and the confirmation come from the record."""
    inputs = [f"Use the result of {dep}: {placeholder(dep)}" for dep in task.depends_on]
    confirm = [CONFIRM] if task.side_effect else []
    if draft is None:
        lines = [f"Task: {task.instruction}", *_bullets("Inputs", inputs), *_bullets("Constraints", confirm)]
    else:
        lines = [f"Task: {draft.instruction}", *([f"Context: {draft.context}"] if draft.context else []),
                 *_bullets("Inputs", inputs), *_bullets("Constraints", [*draft.constraints, *confirm]),
                 *_bullets("Done when", draft.success_criteria),
                 *_bullets("Open questions. Ask the user. Do not guess", draft.open_questions)]
    return "\n".join(lines)


class Prewriter:
    """Drafts for one utterance's tasks. At most one draft request per task is in flight, and the newest input wins.

    `observe` and `land` return the inputs to send to the author now. The caller owns the author and the clock.
    """

    def __init__(self, finished=THRESHOLDS.finished):
        self.finished = finished
        self.drafts: dict[str, PromptDraft] = {}
        self.in_flight: dict[str, DraftInput] = {}
        self.waiting: dict[str, DraftInput] = {}
        self.started: set[str] = set()
        self.requests: list[DraftInput] = []

    def observe(self, point, tasks, text):
        """One applied decision point. Starts a draft when a task first completes, and redrafts when its inputs move."""
        self.started |= set(completed(point, tasks, self.finished))
        send = []
        for task_id in sorted(self.started):
            task = tasks[task_id]
            if task.status not in UNDISPATCHED:
                continue
            source = source_for(task, text, point["revision"])
            entry = self.drafts.get(task_id)
            latest = self.waiting.get(task_id) or self.in_flight.get(task_id) or (entry and entry.source)
            if source == latest:
                continue
            if entry is not None and entry.status == DraftStatus.CURRENT:
                entry.status = DraftStatus.STALE
            if task_id in self.in_flight:
                self.waiting[task_id] = source
            else:
                send.append(self._send(source))
        return send

    def _send(self, source):
        self.in_flight[source.task_id] = source
        self.requests.append(source)
        if source.task_id not in self.drafts:
            self.drafts[source.task_id] = PromptDraft(source, DraftStatus.DRAFTING)
        return source

    def land(self, source, content, task):
        """The author's reply for `source`. Returns the waiting input for the same task, if there is one."""
        del self.in_flight[source.task_id]
        draft, problems = parse_draft(content)
        if draft is not None:
            problems = check(draft, task)
        if source != source_for(task):
            status = DraftStatus.STALE
        else:
            status = DraftStatus.REJECTED if problems else DraftStatus.CURRENT
        self.drafts[source.task_id] = PromptDraft(source, status, draft, problems)
        nxt = self.waiting.pop(source.task_id, None)
        return [self._send(nxt)] if nxt is not None and nxt != source else []

    def dispatch(self, task):
        """The prompt to send now: the current draft if its input equals the record, else the raw instruction.

        `land` checked the draft against the same input, so an equal input needs no second check.
        """
        entry = self.drafts.get(task.task_id)
        if entry is None:
            reason = "no draft started"
        elif task.task_id in self.in_flight:
            reason = "still drafting"
        elif entry.status != DraftStatus.CURRENT:
            reason = entry.status.value
        elif entry.source != source_for(task):
            reason = "stale"
        else:
            return Dispatch(task.task_id, render(task, entry.draft), True, "current draft")
        return Dispatch(task.task_id, render(task), False, reason)
