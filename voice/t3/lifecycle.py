"""The watch lifecycle from docs/voice/seam-design.md, as one table, and Follow, which runs it.

Every way a watch starts or ends is a row in ROWS. Follow looks up the row for each seam answer,
event, watch end, and socket end, and runs that row's action. No caller branches on a refusal
code or an end on its own. Every row ends in one of three ways: a watch from a cursor, a terminal
task status, or a spoken give-up that saves the task as LOST.

Follow is single-threaded. Its caller passes the time into every call, so tests drive it with a
fake clock and a fake seam. What the voice may say comes out as Update values, each with the
sequences that are its evidence.
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Callable

from voice.t3 import types as T
from voice.t3.journal import JournalTask, Kind, Status, T3Journal, Update
from voice.t3.seam import SeamConnectionEnded, SeamRefused, SeamUnavailable

L1 = "I lost track of that thread. It's still in the app."
L2 = "I can't see that thread any more."
LAUNCH_ACK = "I've asked T3 to start it."
SLOT_LINE = "I'm following {n} threads already. I'll pick this one up when one of them finishes."
DELETED_LINE = "That thread was deleted."
UNREACHABLE_LINE = "I can't reach T3 right now."

RETRY_WAITS_S = (1, 2, 4, 8, 16, 30, 30)
MAX_FAILURES = 8
BUDGET_RESET_S = 60.0
ACK_AFTER_S = 3.0
DONE_WINDOW_S = 10.0


class Ends(StrEnum):
    SUBSCRIBE = "subscribe"
    TERMINAL = "terminal"
    GIVE_UP = "give up"
    ANSWER = "the row for its answer"
    SAVED = "the row for the saved state"


class Act(StrEnum):
    WATCH_LAUNCHED = "save the launch sequence as the cursor, then watch"
    READ_LAUNCHED = "journal the link with no cursor, then read first and watch"
    FAIL_REFUSED = "fail with the refusal's spoken line"
    RETRY_LAUNCH = "retry launch with the same request id"
    READ_SENT = "journal the sent link with no cursor, then read first and watch"
    RETRY_SEND = "retry send with the same request id"
    NO_WATCH = "complete from the list or the read, with no watch"
    REISSUE = "call launch or send again with the journaled request id"
    WATCH_FROM_CURSOR = "watch from the saved cursor, or read first with none"
    NOTHING = "nothing"
    QUEUE = "queue for a watch slot"
    RETRY_WATCH = "watch again under the task's budget"
    GIVE_UP_NOW = "give up at once"
    READ_THEN_WATCH = "read first, then watch"
    RETRY_READ = "give up on not_found, retry the read on unavailable"
    DELETED = "fail as deleted"
    DONE = "speak the done status, save it, keep the watch for 10 s"
    RECONNECT_NOW = "reconnect at once"
    RECONNECT_RETRY = "reconnect under the socket's budget"
    GIVE_UP_ALL = "give up for every live task"


@dataclass(frozen=True)
class Row:
    when: str
    act: Act
    ends: Ends
    line: str | dict | None = None


ROWS: dict[str, Row] = {
    "launch-fresh": Row("launch returns replayed: false", Act.WATCH_LAUNCHED, Ends.SUBSCRIBE),
    "launch-replayed": Row("launch returns replayed: true", Act.READ_LAUNCHED, Ends.SUBSCRIBE),
    "launch-refused": Row("launch answers any refusal except unavailable", Act.FAIL_REFUSED, Ends.TERMINAL),
    "launch-unavailable": Row("launch answers unavailable, or the socket closes first", Act.RETRY_LAUNCH,
                              Ends.GIVE_UP,
                              "I asked T3 to start it, but I couldn't confirm that it started. Check the app."),
    "tell": Row("send returns, fresh or replayed", Act.READ_SENT, Ends.SUBSCRIBE),
    "tell-refused": Row("send answers any refusal except unavailable", Act.FAIL_REFUSED, Ends.TERMINAL),
    "tell-unavailable": Row("send answers unavailable, or the socket closes first", Act.RETRY_SEND, Ends.GIVE_UP,
                            "I asked T3 to pass that on, but I couldn't confirm that it arrived. Check the app."),
    "ask": Row("Twin B asks about a thread", Act.NO_WATCH, Ends.TERMINAL, UNREACHABLE_LINE),
    "restart-unstarted": Row("on restart, a request id with no thread", Act.REISSUE, Ends.ANSWER),
    "restart-live": Row("on restart, a started task that has not ended", Act.WATCH_FROM_CURSOR, Ends.SUBSCRIBE),
    "restart-ended": Row("on restart, a terminal or LOST task", Act.NOTHING, Ends.TERMINAL),
    "crash-before-save": Row("crashed before saving a queued line", Act.WATCH_FROM_CURSOR, Ends.SUBSCRIBE),
    "crash-after-save": Row("crashed after saving a queued line or taking it", Act.NOTHING, Ends.SAVED),
    "slot-wait": Row("a task needs a watch while maxWatches are held", Act.QUEUE, Ends.SUBSCRIBE, SLOT_LINE),
    "watch-retry": Row("watch answers unavailable or too_many_watches", Act.RETRY_WATCH, Ends.GIVE_UP, L1),
    "watch-refused": Row("watch answers any other code", Act.GIVE_UP_NOW, Ends.GIVE_UP,
                         {"project_not_allowed": L2, "not_found": L2, "gone": L2, "*": L1}),
    "ended-unavailable": Row("watch_ended unavailable", Act.RETRY_WATCH, Ends.GIVE_UP, L1),
    "ended-resync": Row("watch_ended resync, for a live task", Act.READ_THEN_WATCH, Ends.SUBSCRIBE),
    "read-failed": Row("the read in launch-replayed, tell, or ended-resync fails", Act.RETRY_READ, Ends.GIVE_UP,
                       {"not_found": L2, "*": L1}),
    "ended-not-found": Row("watch_ended not_found or gone", Act.GIVE_UP_NOW, Ends.GIVE_UP, L2),
    "gone-event": Row("a gone event, from a stored thread.deleted", Act.DELETED, Ends.TERMINAL, DELETED_LINE),
    "done-event": Row("a done event for a live task", Act.DONE, Ends.TERMINAL),
    "ended-after-terminal": Row("any end of a watch for a terminal or LOST task", Act.NOTHING, Ends.TERMINAL),
    "close-1013": Row("the socket closes with 1013", Act.RECONNECT_NOW, Ends.SUBSCRIBE),
    "close-other": Row("any other close or drop, or an upgrade that cannot connect", Act.RECONNECT_RETRY,
                       Ends.GIVE_UP,
                       "I've lost my connection to T3, so I've stopped following your threads. "
                       "They're still in the app."),
    "upgrade-401-or-403": Row("an upgrade answers 401 or 403, or the session is revoked", Act.GIVE_UP_ALL,
                              Ends.GIVE_UP,
                              "T3 no longer accepts my access, so I've stopped following your threads. "
                              "They're still in the app."),
    "upgrade-404": Row("an upgrade answers 404", Act.GIVE_UP_ALL, Ends.GIVE_UP,
                       "The voice seam is off, so I've stopped following your threads. They're still in the app."),
}

C = T.RefusalCode
LAUNCH_ROWS: dict[C, str] = {
    C.OPERATION_NOT_ALLOWED: "launch-refused", C.PROJECT_NOT_ALLOWED: "launch-refused",
    C.ABOVE_CEILING: "launch-refused", C.NOT_FOUND: "launch-refused", C.GONE: "launch-refused",
    C.REFUSED: "launch-refused", C.TOO_MANY_WATCHES: "launch-refused", C.NO_DEFAULT_MODEL: "launch-refused",
    C.INVALID_REQUEST: "launch-refused", C.UNAVAILABLE: "launch-unavailable",
}
SEND_ROWS: dict[C, str] = {
    C.OPERATION_NOT_ALLOWED: "tell-refused", C.PROJECT_NOT_ALLOWED: "tell-refused",
    C.ABOVE_CEILING: "tell-refused", C.NOT_FOUND: "tell-refused", C.GONE: "tell-refused",
    C.REFUSED: "tell-refused", C.TOO_MANY_WATCHES: "tell-refused", C.NO_DEFAULT_MODEL: "tell-refused",
    C.INVALID_REQUEST: "tell-refused", C.UNAVAILABLE: "tell-unavailable",
}
WATCH_CALL_ROWS: dict[C, str] = {
    C.OPERATION_NOT_ALLOWED: "watch-refused", C.PROJECT_NOT_ALLOWED: "watch-refused",
    C.ABOVE_CEILING: "watch-refused", C.NOT_FOUND: "watch-refused", C.GONE: "watch-refused",
    C.REFUSED: "watch-refused", C.TOO_MANY_WATCHES: "watch-retry", C.NO_DEFAULT_MODEL: "watch-refused",
    C.INVALID_REQUEST: "watch-refused", C.UNAVAILABLE: "watch-retry",
}
READ_ROWS: dict[C, str] = {code: "read-failed" for code in C}
WATCH_ENDED_ROWS: dict[T.WatchEndReason, str] = {
    T.WatchEndReason.UNAVAILABLE: "ended-unavailable",
    T.WatchEndReason.RESYNC: "ended-resync",
    T.WatchEndReason.NOT_FOUND: "ended-not-found",
    T.WatchEndReason.GONE: "ended-not-found",
}
SOCKET_END_ROWS: dict[str, str] = {
    "close-1013": "close-1013",
    "close-1008": "upgrade-401-or-403",
    "close-1007": "close-other",
    "close-1011": "close-other",
    "close-other": "close-other",
    "unreachable": "close-other",
    "upgrade-401": "upgrade-401-or-403",
    "upgrade-403": "upgrade-401-or-403",
    "upgrade-404": "upgrade-404",
}

DONE_STATUS: dict[T.TerminalStatus, tuple[Status, str]] = {
    T.TerminalStatus.COMPLETED: (Status.COMPLETED, "It finished."),
    T.TerminalStatus.FAILED: (Status.FAILED, "It failed."),
    T.TerminalStatus.INTERRUPTED: (Status.CANCELLED, "It was stopped."),
    T.TerminalStatus.CANCELLED: (Status.CANCELLED, "It was cancelled."),
    T.TerminalStatus.ROLLED_BACK: (Status.CANCELLED, "Its work was rolled back."),
}
RUN_LINES: dict[T.RunStatus, str] = {
    T.RunStatus.RUNNING: "It's working on it.",
    T.RunStatus.QUEUED: "It's queued behind other work.",
}
RESOLVED_LINES: dict[T.ResolvedStatus, str] = {
    T.ResolvedStatus.RESOLVED: "That request was answered.",
    T.ResolvedStatus.EXPIRED: "That request expired.",
    T.ResolvedStatus.CANCELLED: "That request was cancelled.",
}

_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_MARKS = re.compile(r"[*_`]+")
_LINE_START = re.compile(r"^\s*(?:#+|>+|[-*+]|\d+[.)])\s+")
_TABLE = re.compile(r"^\s*\|.*\|\s*$|^\s*[-:| ]{3,}\s*$")
_SPACE_BEFORE = re.compile(r"\s+([.,;:!?])")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def _prose(text: str) -> str:
    """Markdown as spoken prose: code blocks and tables dropped, each heading or list item its own sentence."""
    out, fenced = [], False
    for line in (text or "").splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if fenced or _TABLE.match(line) or not line.strip():
            continue
        line = _MARKS.sub("", _LINK.sub(lambda m: m.group(1), _LINE_START.sub("", line))).strip()
        if line and line[-1] not in ".!?:":
            line += "."
        out.append(line)
    return _SPACE_BEFORE.sub(lambda m: m.group(1), " ".join(" ".join(out).split()))


def speakable(text: str, sentences: int = 2, limit: int = 240) -> str:
    """The first sentences of a recorded text as prose, cut at a word under `limit`."""
    kept = " ".join(_SENTENCE_END.split(_prose(text))[:sentences])
    if len(kept) <= limit:
        return kept
    return kept[:limit].rsplit(" ", 1)[0].rstrip(",;:") + "..."


def row_line(row: Row, code: Any = None) -> str | None:
    if isinstance(row.line, dict):
        key = getattr(code, "value", code)
        return row.line.get(key, row.line["*"])
    return row.line


def refusal_line(refusal: SeamRefused, verb: str = "start it") -> str:
    """`refused` names T3's recorded reason and is never spoken as deleted. Other codes speak the server's text."""
    if refusal.code is C.REFUSED:
        return f"T3 refused to {verb}: {refusal.reason}" if refusal.reason else f"T3 refused to {verb}."
    return refusal.message


def done_line(event: T.DoneEvent, link_replies: dict[str, int]) -> tuple[Status, str]:
    """The status and line for a done. Its text is spoken only when it has one and no reply already said it."""
    status, line = DONE_STATUS[event.status]
    result = event.result
    said = result.message_id is not None and result.message_id in link_replies
    has_record = result.message_id is not None or result.turn_item_id is not None
    text = speakable(result.text, sentences=1 if status is Status.FAILED else 2)
    if text and has_record and not said:
        line = f"{line} It said: {text}"
    return status, line


@dataclass
class Budget:
    failures: int = 0
    due_at: float | None = None
    op: str | None = None


@dataclass
class Follow:
    """Runs ROWS for every task in `journal` over the seam `connect` returns."""

    journal: T3Journal
    connect: Callable[[], Any]
    seam: Any = None
    watching: dict[str, str] = field(default_factory=dict)
    opened_at: dict[str, float] = field(default_factory=dict)
    queue: deque = field(default_factory=deque)
    queued_said: set = field(default_factory=set)
    budgets: dict[str, Budget] = field(default_factory=dict)
    windows: dict[str, float] = field(default_factory=dict)
    begun_at: dict[str, float] = field(default_factory=dict)
    acked: set = field(default_factory=set)
    outbox: list[Update] = field(init=False)
    socket: Budget = field(default_factory=Budget)
    socket_opened_at: float | None = None
    after_1013: bool = False
    dead: str | None = None
    trail: list[tuple[str, str | None]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.outbox = list(self.journal.pending_lines)

    def run(self, name: str, task: JournalTask | None, now: float, **context) -> None:
        row = ROWS[name]
        self.trail.append((name, task.task_id if task is not None else None))
        ACTIONS[row.act](self, name, row, task, now, **context)

    def take(self, task_id: str | None = None) -> list[Update]:
        taken = [u for u in self.outbox if task_id is None or u.task_id == task_id]
        self.journal.take_lines(task_id)
        self.outbox = [u for u in self.outbox if task_id is not None and u.task_id != task_id]
        return taken

    def speak(self, task: JournalTask, line: str, row: str | None = None, sequences=(), *,
              durable: bool = True) -> None:
        sequences = tuple(s for s in sequences if s is not None)
        task.link.evidence.extend(sequences)
        update = Update(task.task_id, line, task.status.value, row, task.link.thread_id, sequences)
        self.outbox.append(update)
        if durable:
            self.journal.pending_lines.append(update)

    def resume(self, now: float) -> None:
        """After a voice-server restart: every journal task takes its restart row."""
        self.ensure(now)
        for task in list(self.journal.tasks.values()):
            if task.status is Status.UNSTARTED:
                self.run("restart-unstarted", task, now)
            elif task.status is Status.LIVE:
                self.run("restart-live", task, now)
            else:
                self.run("restart-ended", task, now)

    def launch(self, task: JournalTask, now: float) -> None:
        self.begun_at.setdefault(task.task_id, now)
        seam = self.ensure(now)
        if seam is None:
            return self.run("launch-unavailable", task, now)
        try:
            result = seam.launch(task.link.request_id, task.link.project_id, task.instruction)
        except SeamRefused as refusal:
            return self.run(LAUNCH_ROWS[refusal.code], task, now, refusal=refusal)
        except (SeamUnavailable, SeamConnectionEnded):
            return self.run("launch-unavailable", task, now)
        self.budgets.pop(task.task_id, None)
        self.run("launch-replayed" if result.replayed else "launch-fresh", task, now, result=result)

    def send(self, task: JournalTask, now: float) -> None:
        seam = self.ensure(now)
        if seam is None:
            return self.run("tell-unavailable", task, now)
        try:
            result = seam.send(task.link.request_id, task.link.thread_id, task.instruction)
        except SeamRefused as refusal:
            return self.run(SEND_ROWS[refusal.code], task, now, refusal=refusal)
        except (SeamUnavailable, SeamConnectionEnded):
            return self.run("tell-unavailable", task, now)
        self.budgets.pop(task.task_id, None)
        self.run("tell", task, now, result=result)

    def stop(self, task: JournalTask, now: float) -> str:
        """Interrupt the task's thread once. A retry reports the first saved result again."""
        stop = self.journal.ensure_stop(task)
        if stop["kind"] is not None:
            return STOP_LINES[stop["kind"]]
        seam = self.ensure(now)
        if seam is None:
            return STOP_UNAVAILABLE
        try:
            result = seam.interrupt(stop["request_id"], task.link.thread_id)
        except SeamRefused as refusal:
            return refusal.message
        except (SeamUnavailable, SeamConnectionEnded):
            return STOP_UNAVAILABLE
        stop["kind"], stop["sequence"] = _field(result, "kind"), _field(result, "sequence")
        self.journal.save()
        return STOP_LINES[stop["kind"]]

    def ensure(self, now: float):
        """The open seam, connecting first when the socket's budget allows it now."""
        if self.seam is not None or self.dead is not None:
            return self.seam
        if self.socket.due_at is not None and now < self.socket.due_at:
            return None
        try:
            seam = self.connect()
        except SeamConnectionEnded as ended:
            self.socket_end(ended.end, now)
            return None
        self.seam, self.socket.due_at, self.socket_opened_at = seam, None, now
        for task in self.journal.live():
            if task.task_id not in self.budgets:
                self.watch(task, now)
        return self.seam

    def tick(self, now: float) -> None:
        """Apply every queued seam item, then run whatever the clock has made due."""
        seam = self.seam
        if seam is not None:
            for item in seam.drain(timeout=0):
                self.apply(item, now)
                if self.seam is not seam:
                    break
        if self.seam is None and self.dead is None and self._wants_socket():
            self.ensure(now)
        for task_id, budget in list(self.budgets.items()):
            if budget.due_at is None or now < budget.due_at:
                continue
            if self.seam is None and self.ensure(now) is None:
                continue
            task = self.journal.tasks[task_id]
            budget.due_at = None
            OPS[budget.op](self, task, now)
        for task_id, began in self.begun_at.items():
            task = self.journal.tasks[task_id]
            if task.status is Status.UNSTARTED and task_id not in self.acked and now >= began + ACK_AFTER_S:
                self.acked.add(task_id)
                self.speak(task, LAUNCH_ACK, "launch-unavailable", durable=False)
        for task_id, until in list(self.windows.items()):
            if now >= until:
                self._unwatch(self.journal.tasks[task_id], now)
        for task_id, opened in list(self.opened_at.items()):
            budget = self.budgets.get(task_id)
            if budget is not None and budget.due_at is None and now - opened >= BUDGET_RESET_S:
                del self.budgets[task_id]
        if self.seam is not None and self.socket_opened_at is not None and now - self.socket_opened_at >= BUDGET_RESET_S:
            self.socket, self.after_1013 = Budget(), False
        self._fill(now)

    def watch(self, task: JournalTask, now: float) -> None:
        if task.link.cursor is None:
            return self.read_first(task, now)
        seam = self.seam
        if seam is None or task.link.thread_id in self.watching:
            return
        if len(self.watching) >= seam.hello.ceiling.max_watches:
            return self.run("slot-wait", task, now)
        try:
            seam.watch(task.link.thread_id, task.link.cursor, task.link.run_id)
        except SeamRefused as refusal:
            return self.run(WATCH_CALL_ROWS[refusal.code], task, now, code=refusal.code)
        except SeamUnavailable:
            return self.run("watch-retry", task, now, code=C.UNAVAILABLE)
        except SeamConnectionEnded:
            return
        self.watching[task.link.thread_id] = task.task_id
        self.opened_at[task.task_id] = now

    def read_first(self, task: JournalTask, now: float) -> None:
        """Speak only what the read shows, save its need, take its sequence as the cursor, then watch."""
        seam = self.seam
        if seam is None:
            return
        try:
            state = seam.read_thread(task.link.thread_id)
        except SeamRefused as refusal:
            return self.run(READ_ROWS[refusal.code], task, now, code=refusal.code)
        except SeamUnavailable:
            return self.run("read-failed", task, now, code=C.UNAVAILABLE)
        except SeamConnectionEnded:
            return
        need, link = state.pending, task.link
        if need is not None and link.requests.get(need.request_id) != "needs_you":
            self.speak(task, need_line(need.kind, need.prompt, task.ended), "read", (state.as_of_sequence,))
            link.requests[need.request_id] = "needs_you"
            link.pending = {"request_id": need.request_id, "kind": need.kind.value, "prompt": need.prompt}
        link.cursor = state.as_of_sequence
        self.journal.save()
        self.watch(task, now)

    def _unwatch(self, task: JournalTask, now: float) -> None:
        self.windows.pop(task.task_id, None)
        thread = task.link.thread_id
        if self.watching.get(thread) == task.task_id:
            del self.watching[thread]
            self.opened_at.pop(task.task_id, None)
            if self.seam is not None:
                try:
                    self.seam.unwatch(thread)
                except (SeamRefused, SeamUnavailable, SeamConnectionEnded):
                    pass
            self.run("ended-after-terminal", task, now)

    def _fill(self, now: float) -> None:
        while self.queue and self.seam is not None and len(self.watching) < self.seam.hello.ceiling.max_watches:
            task = self.journal.tasks[self.queue.popleft()]
            if task.status is Status.LIVE:
                self.watch(task, now)

    def _wants_socket(self) -> bool:
        return any(t.status is Status.LIVE for t in self.journal.tasks.values()) or any(
            b.due_at is not None for b in self.budgets.values())

    def _retry(self, task: JournalTask, op: str, name: str, now: float, code: Any = None) -> None:
        budget = self.budgets.setdefault(task.task_id, Budget())
        budget.failures += 1
        budget.op = op
        if budget.failures >= MAX_FAILURES:
            del self.budgets[task.task_id]
            self.give_up(task, row_line(ROWS[name], code), name)
        else:
            budget.due_at = now + RETRY_WAITS_S[budget.failures - 1]

    def give_up(self, task: JournalTask, line: str | None, name: str) -> None:
        task.status = Status.LOST
        if line:
            self.speak(task, line, name)
        self.journal.save()
        self.budgets.pop(task.task_id, None)
        if self.watching.get(task.link.thread_id) == task.task_id:
            del self.watching[task.link.thread_id]
            self.opened_at.pop(task.task_id, None)
            if self.seam is not None:
                try:
                    self.seam.unwatch(task.link.thread_id)
                except (SeamRefused, SeamUnavailable, SeamConnectionEnded):
                    pass

    def apply(self, item: Any, now: float) -> None:
        if isinstance(item, T.SocketEnd):
            return self.socket_end(item, now)
        task = self._task_for(item.thread_id)
        if task is None:
            return
        if isinstance(item, T.WatchEnded):
            if self.watching.get(item.thread_id) == task.task_id:
                del self.watching[item.thread_id]
                self.opened_at.pop(task.task_id, None)
            if task.ended:
                self.windows.pop(task.task_id, None)
                return self.run("ended-after-terminal", task, now)
            return self.run(WATCH_ENDED_ROWS[item.reason], task, now, code=item.reason)
        if isinstance(item, T.DoneEvent):
            if task.ended:
                return
            return self.run("done-event", task, now, event=item)
        self._cursor_event(task, item, now)

    def _task_for(self, thread_id: str) -> JournalTask | None:
        task_id = self.watching.get(thread_id)
        if task_id is not None:
            return self.journal.tasks[task_id]
        candidates = [t for t in self.journal.by_thread(thread_id) if t.task_id in self.windows]
        return candidates[0] if candidates else None

    def _cursor_event(self, task: JournalTask, event: Any, now: float) -> None:
        link = task.link
        if link.cursor is not None and event.sequence <= link.cursor:
            return
        seq = (event.sequence,)
        if isinstance(event, T.RunEvent):
            pair = [event.run_id, event.status.value]
            if pair not in link.runs:
                link.runs.append(pair)
                line = RUN_LINES.get(event.status)
                if line and not task.ended:
                    self.speak(task, line, "run", seq)
        elif isinstance(event, T.ReplyEvent):
            if event.message_id not in link.replies:
                text = speakable(event.text)
                if text:
                    self.speak(task, f"It said: {text}", "reply", seq)
                link.replies[event.message_id] = event.sequence
        elif isinstance(event, T.NeedsYouEvent):
            if link.requests.get(event.request_id) != "needs_you":
                self.speak(task, need_line(event.kind, event.prompt, task.ended), "needs_you", seq)
                link.requests[event.request_id] = "needs_you"
                link.pending = {"request_id": event.request_id, "kind": event.kind.value, "prompt": event.prompt}
        elif isinstance(event, T.ResolvedEvent):
            before = link.requests.get(event.request_id)
            if before != "resolved":
                if before == "needs_you":
                    self.speak(task, RESOLVED_LINES[event.status], "resolved", seq)
                link.requests[event.request_id] = "resolved"
                if link.pending is not None and link.pending.get("request_id") == event.request_id:
                    link.pending = None
        elif isinstance(event, T.GoneEvent) and not task.ended:
            link.cursor = event.sequence
            return self.run("gone-event", task, now, event=event)
        link.cursor = event.sequence
        self.journal.save()

    def socket_end(self, end: T.SocketEnd, now: float) -> None:
        if self.seam is not None:
            self.seam.close()
        self.seam, self.socket_opened_at = None, None
        for task_id in list(self.windows):
            self.run("ended-after-terminal", self.journal.tasks[task_id], now)
        self.watching.clear()
        self.opened_at.clear()
        self.windows.clear()
        name = SOCKET_END_ROWS[end.label]
        if name == "close-1013" and self.after_1013:
            name = "close-other"
        self.run(name, None, now, end=end)


STOP_UNAVAILABLE = "I can't stop it from here yet. You can stop it in the app."
STOP_LINES = {"requested": "I've asked it to stop.", "nothing_running": "It wasn't running anything."}


def _field(value: Any, name: str) -> Any:
    return value.get(name) if isinstance(value, dict) else getattr(value, name)


def need_line(kind: T.PendingKind, prompt: str, ended: bool) -> str:
    if kind is T.PendingKind.IN_APP:
        return "It also needs something from you in the app." if ended else prompt
    return f"It also needs you: {prompt}" if ended else f"It needs you: {prompt}"


# Actions, one per Act. Each takes (follow, row name, row, task, now, **context).


def _watch_launched(f: Follow, name, row, task, now, result):
    link = task.link
    link.thread_id, link.run_id, link.cursor = result.thread_id, result.run_id, result.sequence
    _started(f, name, task, result.sequence)
    f.watch(task, now)


def _read_launched(f: Follow, name, row, task, now, result):
    link = task.link
    link.thread_id, link.run_id, link.cursor = result.thread_id, result.run_id, None
    _started(f, name, task, result.sequence)
    f.read_first(task, now)


def _started(f: Follow, name, task, sequence):
    task.status = Status.LIVE
    if not task.announced:
        if task.kind is Kind.START:
            where = f" in {task.project_name}" if task.project_name else ""
            f.speak(task, f"Started a thread{where}.", name, (sequence,))
        else:
            f.speak(task, "I've passed that on.", name, (sequence,))
        task.announced = True
    f.journal.save()


def _fail_refused(f: Follow, name, row, task, now, refusal):
    task.status = Status.FAILED
    if refusal.thread_id and task.link.thread_id is None:
        task.link.thread_id = refusal.thread_id
    f.budgets.pop(task.task_id, None)
    f.speak(task, refusal_line(refusal, "start it" if task.kind is Kind.START else "pass that on"), name)
    f.journal.save()


def _retry_launch(f: Follow, name, row, task, now, **_):
    f._retry(task, "launch", name, now)


def _retry_send(f: Follow, name, row, task, now, **_):
    f._retry(task, "send", name, now)


def _no_watch(f: Follow, name, row, task, now, **_):
    pass


def _reissue(f: Follow, name, row, task, now, **_):
    f.begun_at.setdefault(task.task_id, now)
    if f.seam is None:
        f.budgets[task.task_id] = Budget(due_at=now, op="launch" if task.kind is Kind.START else "send")
        return
    (f.launch if task.kind is Kind.START else f.send)(task, now)


def _watch_from_cursor(f: Follow, name, row, task, now, **_):
    f.watch(task, now)


def _nothing(f: Follow, name, row, task, now, **_):
    pass


def _queue(f: Follow, name, row, task, now, **_):
    if task.task_id not in f.queue:
        f.queue.append(task.task_id)
    if task.task_id not in f.queued_said:
        f.queued_said.add(task.task_id)
        f.speak(task, SLOT_LINE.format(n=len(f.watching)), name, durable=False)


def _retry_watch(f: Follow, name, row, task, now, code=None, **_):
    f._retry(task, "watch", name, now, code)


def _give_up_now(f: Follow, name, row, task, now, code=None, **_):
    f.give_up(task, row_line(row, code), name)


def _read_then_watch(f: Follow, name, row, task, now, **_):
    f.read_first(task, now)


def _retry_read(f: Follow, name, row, task, now, code=None, **_):
    if code is C.UNAVAILABLE:
        f._retry(task, "read", name, now, code)
    else:
        f.give_up(task, row_line(row, code), name)


def _deleted(f: Follow, name, row, task, now, event):
    task.status = Status.FAILED
    f.speak(task, row.line, name, (event.sequence,))
    f.journal.save()


def _done(f: Follow, name, row, task, now, event):
    status, line = done_line(event, task.link.replies)
    task.status = status
    f.speak(task, line, name, (event.sequence, event.as_of_sequence))
    f.journal.save()
    f.windows[task.task_id] = now + DONE_WINDOW_S


def _reconnect_now(f: Follow, name, row, task, now, **_):
    f.after_1013 = True
    f.socket.due_at = now
    if f._wants_socket():
        f.ensure(now)


def _reconnect_retry(f: Follow, name, row, task, now, **_):
    f.socket.failures += 1
    if f.socket.failures >= MAX_FAILURES:
        f.socket = Budget()
        _give_up_all(f, name, row, None, now)
    else:
        f.socket.due_at = now + RETRY_WAITS_S[f.socket.failures - 1]


def _give_up_all(f: Follow, name, row, task, now, **_):
    if row.act is Act.GIVE_UP_ALL:
        f.dead = name
    open_tasks = [t for t in f.journal.tasks.values() if not t.ended]
    for i, open_task in enumerate(open_tasks):
        f.give_up(open_task, row.line if i == 0 else None, name)
    f.queue.clear()


ACTIONS: dict[Act, Callable] = {
    Act.WATCH_LAUNCHED: _watch_launched,
    Act.READ_LAUNCHED: _read_launched,
    Act.FAIL_REFUSED: _fail_refused,
    Act.RETRY_LAUNCH: _retry_launch,
    Act.READ_SENT: _read_launched,
    Act.RETRY_SEND: _retry_send,
    Act.NO_WATCH: _no_watch,
    Act.REISSUE: _reissue,
    Act.WATCH_FROM_CURSOR: _watch_from_cursor,
    Act.NOTHING: _nothing,
    Act.QUEUE: _queue,
    Act.RETRY_WATCH: _retry_watch,
    Act.GIVE_UP_NOW: _give_up_now,
    Act.READ_THEN_WATCH: _read_then_watch,
    Act.RETRY_READ: _retry_read,
    Act.DELETED: _deleted,
    Act.DONE: _done,
    Act.RECONNECT_NOW: _reconnect_now,
    Act.RECONNECT_RETRY: _reconnect_retry,
    Act.GIVE_UP_ALL: _give_up_all,
}

OPS: dict[str, Callable[[Follow, JournalTask, float], None]] = {
    "launch": Follow.launch,
    "send": Follow.send,
    "watch": Follow.watch,
    "read": Follow.read_first,
}
