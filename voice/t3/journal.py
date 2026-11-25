"""The voice's durable record of the T3 work it started.

One JSON file holds every task the voice launched, sent to, held, or stopped: its request id,
minted and saved before the seam call, the thread it reached, the cursor, the applied request
ids, the reply dedupe, the first interrupt result, and the acts still waiting to be spoken. A
restarted voice server reads it back and follows each live thread from its own cursor. Every write replaces the file whole, so a crash leaves either
the old record or the new one.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path

from voice.t3.acts import Act, Kind as Say, act

CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

_ulid_lock = threading.Lock()
_ulid_last = (0, 0)


def ulid(now_ms: int | None = None) -> str:
    """A 26-character ULID. Ids minted in one process strictly increase, even within a millisecond."""
    global _ulid_last
    ms = int(time.time() * 1000) if now_ms is None else now_ms
    with _ulid_lock:
        last_ms, last_rand = _ulid_last
        if ms <= last_ms:
            ms, rand = last_ms, last_rand + 1
        else:
            rand = int.from_bytes(secrets.token_bytes(10), "big")
        if rand >= 1 << 80:
            ms, rand = ms + 1, 0
        _ulid_last = (ms, rand)
    value = (ms << 80) | rand
    return "".join(CROCKFORD[(value >> shift) & 31] for shift in range(125, -1, -5))


class Status(StrEnum):
    UNSTARTED = "unstarted"
    LIVE = "live"
    HELD = "held"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    LOST = "lost"


ENDED = frozenset({Status.COMPLETED, Status.FAILED, Status.CANCELLED, Status.LOST})


class Kind(StrEnum):
    START = "start"
    TELL = "tell"
    STOP = "stop"
    RESPOND = "respond"
    ANSWER = "answer"
    # The thread a call was opened on: watched for the call, with no write of its own.
    FOLLOW = "follow"


@dataclass
class T3Link:
    request_id: str
    project_id: str | None
    thread_id: str | None = None
    cursor: int | None = None
    evidence: list[int] = field(default_factory=list)
    run_id: str | None = None
    replies: dict[str, int] = field(default_factory=dict)
    requests: dict[str, str] = field(default_factory=dict)
    runs: list[list[str]] = field(default_factory=list)
    pending: dict | None = None
    stop: dict | None = None
    respond: dict | None = None
    answer: dict | None = None


TITLE_MAX_CHARS = 80


def launch_title(instruction: str) -> str:
    """The title the seam gives a thread it starts from `instruction`, until T3 names the thread
    (apps/server/src/voiceSeam/launch.ts, `titleOf`): the first line, cut with an ellipsis."""
    first = (instruction.strip().splitlines() or [""])[0].strip()
    return first if len(first) <= TITLE_MAX_CHARS else first[:TITLE_MAX_CHARS - 1].rstrip() + "…"


@dataclass
class JournalTask:
    task_id: str
    kind: Kind
    instruction: str
    project_name: str | None
    status: Status
    link: T3Link
    announced: bool = False
    title: str | None = None

    @property
    def ended(self) -> bool:
        return self.status in ENDED


def _task_from(raw: dict) -> JournalTask:
    return JournalTask(
        task_id=raw["task_id"],
        kind=Kind(raw["kind"]),
        instruction=raw["instruction"],
        project_name=raw.get("project_name"),
        status=Status(raw["status"]),
        link=T3Link(**raw["link"]),
        announced=raw.get("announced", False),
        title=raw.get("title"),
    )


@dataclass
class Update:
    """What the voice may say, as acts, with the stored or read sequences they come from."""

    task_id: str
    acts: tuple[Act, ...]
    status: str
    row: str | None = None
    thread_id: str | None = None
    sequences: tuple[int, ...] = ()
    delivery_failures: int = 0
    problems: tuple[str, ...] = ()
    tries: int = 0
    refused: tuple[str, ...] = ()
    # The refused wordings the decider has already read again for this line, by `normalise`: each gets one new reading.
    reread: tuple[str, ...] = ()
    n: int = 0
    superseded: bool = False
    project_id: str | None = None
    # For a launch: what the new thread was handed ("brief", "retried brief" or "framed request"), for the turn record.
    handed: str | None = None
    # Wall-clock time a call held this line back as news from before it (`T3Journal.earlier_lines`).
    held_at: float | None = None

    def evidence(self) -> dict:
        evidence = {"thread_id": self.thread_id, "sequences": list(self.sequences)}
        if self.project_id is not None:
            evidence["project_id"] = self.project_id
        if self.handed is not None:
            evidence["handed"] = self.handed
        return evidence

    def to_json(self) -> dict:
        return {**asdict(self), "acts": [a.to_json() for a in self.acts]}

    @classmethod
    def from_json(cls, raw: dict) -> Update:
        return cls(**{**raw, "acts": tuple(Act.from_json(a) for a in raw["acts"]),
                      "sequences": tuple(raw["sequences"]), "problems": tuple(raw.get("problems", ())),
                      "refused": tuple(raw.get("refused", ())), "reread": tuple(raw.get("reread", ()))})


def legacy_update(raw: dict) -> Update:
    """A version 1 pending line, which saved its spoken text, as news the voice words and checks like any other."""
    return Update(raw["task_id"], (act(Say.LEGACY_UPDATE, thread=raw.get("title"), text=raw["line"]),),
                  raw["status"], raw.get("row"), raw.get("thread_id"), tuple(raw.get("sequences", ())),
                  raw.get("delivery_failures", 0))


class T3Journal:
    """Tasks by id, loaded from `path` and saved whole after every change the caller makes."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.tasks: dict[str, JournalTask] = {}
        self.pending_lines: list[Update] = []
        # News from before a call, held back when it opened: never said unprompted, given when the caller asks.
        self.earlier_lines: list[Update] = []
        self.newest: dict[str, int] = {}
        if self.path.exists():
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self.tasks = {t["task_id"]: _task_from(t) for t in raw.get("tasks", [])}
            # Version 1 saved spoken text. It is never replayed as text: it becomes a legacy_update act to word again.
            self.pending_lines = [Update.from_json(u) if "acts" in u else legacy_update(u)
                                  for u in raw.get("pending_lines", [])]
            self.earlier_lines = [Update.from_json(u) for u in raw.get("earlier_lines", [])]
            self.newest = dict(raw.get("newest", {}))

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        body = {"version": 2, "tasks": [asdict(t) for t in self.tasks.values()],
                "pending_lines": [u.to_json() for u in self.pending_lines],
                "earlier_lines": [u.to_json() for u in self.earlier_lines], "newest": self.newest}
        tmp.write_text(json.dumps(body, indent=1) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)

    def stamp(self, update: Update) -> None:
        """Number a thread's update in the order it was made, and record it as that thread's newest."""
        if update.thread_id:
            update.n = max(self.newest.values(), default=0) + 1
            self.newest[update.thread_id] = update.n

    def stale(self, update: Update) -> bool:
        """A newer update about the same thread was made after this one."""
        return bool(update.thread_id and update.n and self.newest.get(update.thread_id, 0) > update.n)

    def supersede(self, newer: Update) -> None:
        """Mark each saved line about `newer`'s thread that has been tried and is still waiting. A superseded line
        stays in the journal as the record of it, and is never spoken."""
        for update in self.pending_lines:
            if (update is not newer and update.thread_id == newer.thread_id and not update.superseded
                    and (update.tries or update.delivery_failures)):
                update.superseded = True

    def replace_lines(self, old: list[Update], new: Update) -> None:
        """`new` stands where the first of `old` stood, when any of them is saved."""
        taken = set(map(id, old))
        at = next((i for i, u in enumerate(self.pending_lines) if id(u) in taken), None)
        if at is not None:
            kept = [u for u in self.pending_lines if id(u) not in taken]
            kept.insert(min(at, len(kept)), new)
            self.pending_lines = kept
            self.save()

    def remove_lines(self, updates: list[Update]) -> None:
        taken = set(map(id, updates))
        previous = self.pending_lines
        remaining = [u for u in previous if id(u) not in taken]
        if len(previous) != len(remaining):
            self.pending_lines = remaining
            try:
                self.save()
            except OSError:
                self.pending_lines = previous
                raise

    def begin(self, kind: Kind, instruction: str, project_id: str | None, project_name: str | None = None,
              thread_id: str | None = None, title: str | None = None) -> JournalTask:
        """Mint the task and its request id and save both before the caller makes the seam call."""
        task = JournalTask(ulid(), Kind(kind), instruction, project_name, Status.UNSTARTED,
                           T3Link(request_id=ulid(), project_id=project_id, thread_id=thread_id), title=title)
        self.tasks[task.task_id] = task
        self.save()
        return task

    def by_thread(self, thread_id: str) -> list[JournalTask]:
        return [t for t in self.tasks.values() if t.link.thread_id == thread_id]

    def live(self) -> list[JournalTask]:
        return [t for t in self.tasks.values() if t.status is Status.LIVE]

    def held(self, thread_id: str | None = None) -> list[JournalTask]:
        """Follow-ups waiting for their thread's run to finish, oldest first."""
        return [t for t in self.tasks.values()
                if t.status is Status.HELD and thread_id in (None, t.link.thread_id)]

    def ensure_respond(self, task: JournalTask, pending_request_id: str, decision: str, need: str | None = None) -> dict:
        """The task's answer record: the request id minted and saved once, before the first call, with the pending
        request and the decision it answers. A retry of the task sends the same ones."""
        if task.link.respond is None:
            task.link.respond = {"request_id": ulid(), "pending_request_id": pending_request_id, "decision": decision,
                                 "need": need, "sequence": None, "answered": False}
            self.save()
        return task.link.respond

    def ensure_answer(self, task: JournalTask, pending_request_id: str, answers: list[dict], spoken: list[str],
                      need: str | None = None) -> dict:
        """The task's answer record: the request id minted and saved once, before the first call, with the pending
        request, the answers it gives (each {questionId, kind, text}) and the words the caller confirmed for them. A
        retry of the task sends the same ones."""
        if task.link.answer is None:
            task.link.answer = {"request_id": ulid(), "pending_request_id": pending_request_id, "answers": answers,
                                "spoken": spoken, "need": need, "sequence": None, "answered": False}
            self.save()
        return task.link.answer

    def ensure_stop(self, task: JournalTask) -> dict:
        """The task's interrupt record. Its request id is minted and saved once, before the first call."""
        if task.link.stop is None:
            task.link.stop = {"request_id": ulid(), "kind": None, "sequence": None}
            self.save()
        return task.link.stop
