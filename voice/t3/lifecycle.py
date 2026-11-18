"""The watch lifecycle from docs/voice/seam-design.md, as one table, and Follow, which runs it.

Every way a watch starts or ends is a row in ROWS. Follow looks up the row for each seam answer,
event, watch end, and socket end, and runs that row's action. No caller branches on a refusal
code or an end on its own. Every row ends in one of three ways: a watch from a cursor, a terminal
task status, or a spoken give-up that saves the task as LOST.

Follow is single-threaded. Its caller passes the time into every call, so tests drive it with a
fake clock and a fake seam. What the voice may say comes out as Update values: typed acts
(voice/t3/acts.py), never sentences, each with the sequences that are its evidence.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Callable

from voice.t3 import types as T
from voice.t3.acts import RULES, Act, act
from voice.t3.acts import Kind as Say
from voice.t3.journal import ENDED, JournalTask, Kind, Status, T3Journal, Update, ulid
from voice.t3.seam import SeamConnectionEnded, SeamRefused, SeamUnavailable

LOST = act(Say.LOST_TRACK)
GONE = act(Say.THREAD_GONE)

RETRY_WAITS_S = (1, 2, 4, 8, 16, 30, 30)
MAX_FAILURES = 8
BUDGET_RESET_S = 60.0
ACK_AFTER_S = 3.0
DONE_WINDOW_S = 10.0
TERMINAL_RUN_STATUSES = frozenset(T.RunStatus(status.value) for status in T.TerminalStatus)


class Ends(StrEnum):
    SUBSCRIBE = "subscribe"
    TERMINAL = "terminal"
    GIVE_UP = "give up"
    ANSWER = "the row for its answer"
    SAVED = "the row for the saved state"


class Do(StrEnum):
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
    HOLD = "hold the follow-up under a fresh request id, then watch its thread for done"
    RELEASE = "send each follow-up held for the thread, oldest first, with its saved request id"
    DROP = "cancel the held follow-up and hand its watch on"


@dataclass(frozen=True)
class Row:
    when: str
    do: Do
    ends: Ends
    says: Act | dict | None = None


ROWS: dict[str, Row] = {
    "launch-fresh": Row("launch returns replayed: false", Do.WATCH_LAUNCHED, Ends.SUBSCRIBE),
    "launch-replayed": Row("launch returns replayed: true", Do.READ_LAUNCHED, Ends.SUBSCRIBE),
    "launch-refused": Row("launch answers any refusal except unavailable", Do.FAIL_REFUSED, Ends.TERMINAL),
    "launch-unavailable": Row("launch answers unavailable, or the socket closes first", Do.RETRY_LAUNCH,
                              Ends.GIVE_UP, act(Say.LAUNCH_UNCONFIRMED)),
    "tell": Row("send returns, fresh or replayed", Do.READ_SENT, Ends.SUBSCRIBE),
    "tell-refused": Row("send answers any refusal except unavailable", Do.FAIL_REFUSED, Ends.TERMINAL),
    "tell-unavailable": Row("send answers unavailable, or the socket closes first", Do.RETRY_SEND, Ends.GIVE_UP,
                            act(Say.TELL_UNCONFIRMED)),
    "tell-busy": Row("send answers thread_busy", Do.HOLD, Ends.SUBSCRIBE, act(Say.HELD_FOLLOWUP)),
    "held-release": Row("a done on a thread that holds follow-ups", Do.RELEASE, Ends.ANSWER),
    "held-dropped": Row("Mason drops a held follow-up, or stops its thread", Do.DROP, Ends.TERMINAL,
                        act(Say.DROPPED_FOLLOWUP)),
    "ask": Row("Twin B asks about a thread", Do.NO_WATCH, Ends.TERMINAL, act(Say.UNREACHABLE)),
    "restart-unstarted": Row("on restart, a request id with no thread", Do.REISSUE, Ends.ANSWER),
    "restart-live": Row("on restart, a started task that has not ended", Do.WATCH_FROM_CURSOR, Ends.SUBSCRIBE),
    "restart-held": Row("on restart, a held follow-up", Do.WATCH_FROM_CURSOR, Ends.SUBSCRIBE),
    "restart-ended": Row("on restart, a terminal or LOST task", Do.NOTHING, Ends.TERMINAL),
    "crash-before-save": Row("crashed before saving a queued line", Do.WATCH_FROM_CURSOR, Ends.SUBSCRIBE),
    "crash-after-save": Row("crashed after saving a queued line or taking it", Do.NOTHING, Ends.SAVED),
    "slot-wait": Row("a task needs a watch while maxWatches are held", Do.QUEUE, Ends.SUBSCRIBE,
                     act(Say.WATCH_QUEUED)),
    "watch-retry": Row("watch answers unavailable or too_many_watches", Do.RETRY_WATCH, Ends.GIVE_UP, LOST),
    "watch-refused": Row("watch answers any other code", Do.GIVE_UP_NOW, Ends.GIVE_UP,
                         {"project_not_allowed": GONE, "not_found": GONE, "gone": GONE, "*": LOST}),
    "ended-unavailable": Row("watch_ended unavailable", Do.RETRY_WATCH, Ends.GIVE_UP, LOST),
    "ended-resync": Row("watch_ended resync, for a live task", Do.READ_THEN_WATCH, Ends.SUBSCRIBE),
    "read-failed": Row("the read in launch-replayed, tell, or ended-resync fails", Do.RETRY_READ, Ends.GIVE_UP,
                       {"not_found": GONE, "*": LOST}),
    "ended-not-found": Row("watch_ended not_found or gone", Do.GIVE_UP_NOW, Ends.GIVE_UP, GONE),
    "gone-event": Row("a gone event, from a stored thread.deleted", Do.DELETED, Ends.TERMINAL,
                      act(Say.THREAD_DELETED)),
    "done-event": Row("a done event for a live task", Do.DONE, Ends.TERMINAL),
    "ended-after-terminal": Row("any end of a watch for a terminal or LOST task", Do.NOTHING, Ends.TERMINAL),
    "close-1013": Row("the socket closes with 1013", Do.RECONNECT_NOW, Ends.SUBSCRIBE),
    "close-other": Row("any other close or drop, or an upgrade that cannot connect", Do.RECONNECT_RETRY,
                       Ends.GIVE_UP, act(Say.SEAM_DOWN, reason="connection")),
    "upgrade-401-or-403": Row("an upgrade answers 401 or 403, or the session is revoked", Do.GIVE_UP_ALL,
                              Ends.GIVE_UP, act(Say.SEAM_DOWN, reason="access")),
    "upgrade-404": Row("an upgrade answers 404", Do.GIVE_UP_ALL, Ends.GIVE_UP,
                       act(Say.SEAM_DOWN, reason="seam_off")),
}

C = T.RefusalCode
LAUNCH_ROWS: dict[C, str] = {
    C.OPERATION_NOT_ALLOWED: "launch-refused", C.PROJECT_NOT_ALLOWED: "launch-refused",
    C.ABOVE_CEILING: "launch-refused", C.NOT_FOUND: "launch-refused", C.GONE: "launch-refused",
    C.REFUSED: "launch-refused", C.TOO_MANY_WATCHES: "launch-refused", C.NO_DEFAULT_MODEL: "launch-refused",
    C.INVALID_REQUEST: "launch-refused", C.THREAD_BUSY: "launch-refused", C.UNAVAILABLE: "launch-unavailable",
}
SEND_ROWS: dict[C, str] = {
    C.OPERATION_NOT_ALLOWED: "tell-refused", C.PROJECT_NOT_ALLOWED: "tell-refused",
    C.ABOVE_CEILING: "tell-refused", C.NOT_FOUND: "tell-refused", C.GONE: "tell-refused",
    C.REFUSED: "tell-refused", C.TOO_MANY_WATCHES: "tell-refused", C.NO_DEFAULT_MODEL: "tell-refused",
    C.INVALID_REQUEST: "tell-refused", C.THREAD_BUSY: "tell-busy", C.UNAVAILABLE: "tell-unavailable",
}
WATCH_CALL_ROWS: dict[C, str] = {
    C.OPERATION_NOT_ALLOWED: "watch-refused", C.PROJECT_NOT_ALLOWED: "watch-refused",
    C.ABOVE_CEILING: "watch-refused", C.NOT_FOUND: "watch-refused", C.GONE: "watch-refused",
    C.REFUSED: "watch-refused", C.TOO_MANY_WATCHES: "watch-retry", C.NO_DEFAULT_MODEL: "watch-refused",
    C.INVALID_REQUEST: "watch-refused", C.THREAD_BUSY: "watch-refused", C.UNAVAILABLE: "watch-retry",
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

DONE_STATUS: dict[T.TerminalStatus, Status] = {
    T.TerminalStatus.COMPLETED: Status.COMPLETED,
    T.TerminalStatus.FAILED: Status.FAILED,
    T.TerminalStatus.INTERRUPTED: Status.CANCELLED,
    T.TerminalStatus.CANCELLED: Status.CANCELLED,
    T.TerminalStatus.ROLLED_BACK: Status.CANCELLED,
}
SPOKEN_RUNS = frozenset({T.RunStatus.RUNNING, T.RunStatus.QUEUED})


def about(task: JournalTask | None, said: Act) -> Act:
    """`said` with the task's thread title and project filled in, where its act has those slots."""
    if task is None:
        return said
    slots = RULES[said.kind].slots
    known = {"thread": task.title, "project": task.project_name}
    return said.with_slots(**{name: value for name, value in known.items()
                              if name in slots and value and name not in said.slots})


def row_act(row: Row, code: Any = None) -> Act | None:
    if isinstance(row.says, dict):
        key = getattr(code, "value", code)
        return row.says.get(key, row.says["*"])
    return row.says


def refused(refusal: SeamRefused, op: str) -> Act:
    """T3's recorded reason when it has one, else the seam's own message, as the reason the voice words."""
    return act(Say.REFUSED, op=op, code=refusal.code.value, reason=refusal.reason or refusal.message)


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
    releasing: set[str] = field(default_factory=set)
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
        self.outbox = [u for u in self.journal.pending_lines if not u.superseded]

    def run(self, name: str, task: JournalTask | None, now: float, **context) -> None:
        row = ROWS[name]
        self.trail.append((name, task.task_id if task is not None else None))
        ACTIONS[row.do](self, name, row, task, now, **context)

    def take(self, task_id: str | None = None) -> list[Update]:
        """Reserve the outbox's lines: they leave the outbox and stay in the journal until `ack`, so a crash before
        they are heard finds them again."""
        taken = [u for u in self.outbox if task_id is None or u.task_id == task_id]
        self.outbox = [u for u in self.outbox if task_id is not None and u.task_id != task_id]
        return taken

    def ack(self, updates: list[Update]) -> None:
        """The lines were heard whole: they leave the journal."""
        self.journal.remove_lines(updates)

    def give_back(self, updates: list[Update]) -> None:
        """Lines taken but never heard go back to the front of the outbox, and of the journal's pending lines. One a
        newer line about its thread has overtaken is superseded instead, so it is never said after that line."""
        saved = set(map(id, self.journal.pending_lines))
        self.journal.pending_lines[:0] = [u for u in updates if id(u) not in saved]
        self.outbox[:0] = [u for u in updates if not self.overtaken(u)]
        self.journal.save()

    def overtaken(self, update: Update) -> bool:
        if self.journal.stale(update):
            update.superseded = True
        return update.superseded

    def speak(self, task: JournalTask, said: Act, row: str | None = None, sequences=(), *,
              durable: bool = True) -> None:
        sequences = tuple(s for s in sequences if s is not None)
        task.link.evidence.extend(sequences)
        update = Update(task.task_id, (about(task, said),), task.status.value, row, task.link.thread_id, sequences)
        self.journal.stamp(update)
        self.journal.supersede(update)
        self.outbox = [u for u in self.outbox if not u.superseded]
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
            elif task.status is Status.HELD:
                if task.task_id not in self.budgets:
                    self.run("restart-held", task, now)
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

    def stop(self, task: JournalTask, now: float) -> Act:
        """Interrupt a stop task's thread once and speak the result. A retry reports the first saved result again."""
        stop = self.journal.ensure_stop(task)
        if stop["kind"] is None:
            seam = self.ensure(now)
            try:
                result = seam.interrupt(stop["request_id"], task.link.thread_id) if seam is not None else None
            except SeamRefused as refusal:
                return self._stopped(task, Status.FAILED, refused(refusal, "stop"))
            except (SeamUnavailable, SeamConnectionEnded):
                result = None
            if result is None:
                return self._stopped(task, Status.FAILED, act(Say.STOP_UNCONFIRMED))
            stop["kind"] = INTERRUPT_KIND[type(result)]
            stop["sequence"] = getattr(result, "sequence", None)
        return self._stopped(task, Status.COMPLETED, act(STOP_ACTS[stop["kind"]]), stop["sequence"])

    def _stopped(self, task: JournalTask, status: Status, said: Act, sequence: int | None = None) -> Act:
        task.status = status
        self.speak(task, said, "stop", (sequence,))
        self.journal.save()
        return about(task, said)

    def drop(self, task: JournalTask, now: float) -> None:
        """Mason took back a held follow-up."""
        if task.status is Status.HELD:
            self.run("held-dropped", task, now)

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
        for task in self.journal.held():
            if task.status is Status.HELD and task.task_id not in self.budgets:
                self.read_first(task, now, keep_cursor=True)
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
            if (task.kind is Kind.START and task.status is Status.UNSTARTED and task_id not in self.acked
                    and now >= began + ACK_AFTER_S):
                self.acked.add(task_id)
                self.speak(task, act(Say.LAUNCH_PENDING), "launch-unavailable", durable=False)
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
        thread = task.link.thread_id
        owner = self.watching.get(thread)
        if owner == task.task_id or not self._yields(owner, task):
            return
        if task.link.cursor is None:
            return self.read_first(task, now)
        seam = self.seam
        if seam is None:
            return
        if owner is None and len(self.watching) >= seam.hello.ceiling.max_watches:
            return self.run("slot-wait", task, now)
        try:
            seam.watch(thread, task.link.cursor, task.link.run_id)
        except SeamRefused as refusal:
            return self.run(WATCH_CALL_ROWS[refusal.code], task, now, code=refusal.code)
        except SeamUnavailable:
            return self.run("watch-retry", task, now, code=C.UNAVAILABLE)
        except SeamConnectionEnded:
            return
        if owner is not None:
            self.windows.pop(owner, None)
            self.opened_at.pop(owner, None)
        self.watching[thread] = task.task_id
        self.opened_at[task.task_id] = now

    def _yields(self, owner: str | None, task: JournalTask) -> bool:
        """The seam keeps one watch per thread, and a new one replaces it. A live task keeps its watch, a held
        follow-up hands it to a live task, and an ended task hands it to anyone."""
        if owner is None:
            return True
        status = self.journal.tasks[owner].status
        return status in ENDED or (status is Status.HELD and task.status is Status.LIVE)

    def read_first(self, task: JournalTask, now: float, *, keep_cursor: bool = False) -> None:
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
        if task.status is Status.HELD and state.run is not None and state.run.status in TERMINAL_RUN_STATUSES:
            if task.link.thread_id in self.releasing:
                # Another run can finish between the release's busy reply and this read.
                self.budgets[task.task_id] = Budget(due_at=now + RETRY_WAITS_S[0], op="read")
            else:
                self.run("held-release", task, now)
            return
        task.title = state.title or task.title
        need, link = state.pending, task.link
        if need is not None and link.requests.get(need.request_id) != "needs_you":
            self.speak(task, need_act(need, task.ended), "read", (state.as_of_sequence,))
            link.requests[need.request_id] = "needs_you"
            link.pending = {"request_id": need.request_id, "kind": need.kind.value, "prompt": need.prompt}
        if not keep_cursor or link.cursor is None:
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
            if task.status in (Status.LIVE, Status.HELD):
                self.watch(task, now)

    def _wants_socket(self) -> bool:
        return any(t.status in (Status.LIVE, Status.HELD) for t in self.journal.tasks.values()) or any(
            b.due_at is not None for b in self.budgets.values())

    def _retry(self, task: JournalTask, op: str, name: str, now: float, code: Any = None) -> None:
        budget = self.budgets.setdefault(task.task_id, Budget())
        budget.failures += 1
        budget.op = op
        if budget.failures >= MAX_FAILURES:
            del self.budgets[task.task_id]
            self.give_up(task, row_act(ROWS[name], code), name)
        else:
            budget.due_at = now + RETRY_WAITS_S[budget.failures - 1]

    def give_up(self, task: JournalTask, said: Act | None, name: str) -> None:
        task.status = Status.LOST
        if said is not None:
            self.speak(task, said, name)
        self.journal.save()
        self.budgets.pop(task.task_id, None)
        self._release_watch(task)

    def _release_watch(self, task: JournalTask) -> None:
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
            if task.status is Status.LIVE:
                if task.link.run_id is not None and item.run_id != task.link.run_id:
                    return
                self.run("done-event", task, now, event=item)
            held = self.journal.held(item.thread_id)
            if held:
                self.run("held-release", held[0], now, event=item)
            return
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
        # A held follow-up watches someone else's run, so it speaks only a need.
        quiet = task.status is Status.HELD
        if isinstance(event, T.RunEvent):
            pair = [event.run_id, event.status.value]
            if pair not in link.runs:
                link.runs.append(pair)
                if event.status in SPOKEN_RUNS and not task.ended and not quiet:
                    self.speak(task, act(Say.RUN_UPDATE, status=event.status.value), "run", seq)
        elif isinstance(event, T.ReplyEvent):
            if event.message_id not in link.replies:
                if event.text.strip() and not quiet:
                    self.speak(task, act(Say.REPLIED, status="running", reply=event.text), "reply", seq)
                link.replies[event.message_id] = event.sequence
        elif isinstance(event, T.NeedsYouEvent):
            if link.requests.get(event.request_id) != "needs_you":
                self.speak(task, need_act(event, task.ended), "needs_you", seq)
                link.requests[event.request_id] = "needs_you"
                link.pending = {"request_id": event.request_id, "kind": event.kind.value, "prompt": event.prompt}
        elif isinstance(event, T.ResolvedEvent):
            before = link.requests.get(event.request_id)
            if before != "resolved":
                if before == "needs_you":
                    self.speak(task, act(Say.NEED_RESOLVED, status=event.status.value), "resolved", seq)
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


STOP_ACTS = {"requested": Say.STOPPING, "nothing_running": Say.NOTHING_RUNNING}
INTERRUPT_KIND = {cls: kind for kind, cls in T.INTERRUPT_KINDS.items()}


# What a need asks of Mason, by what raised it. The prompt is only context for the voice's own words.
NEEDS = {T.NeedSource.COMMAND: "command_approval", T.NeedSource.FILE_CHANGE: "file_change",
         T.NeedSource.FILE_READ: "file_read", T.NeedSource.PERMISSION: "permission",
         T.NeedSource.MCP_ELICITATION: "tool_input", T.NeedSource.USER_INPUT: "question",
         T.NeedSource.LINK_REQUEST: "link", T.NeedSource.SECRET_REQUEST: "secret", T.NeedSource.CARD: "card"}
CARDS = frozenset({T.NeedSource.LINK_REQUEST, T.NeedSource.SECRET_REQUEST, T.NeedSource.CARD})


def need_of(kind: T.PendingKind, source: T.NeedSource) -> str:
    """A request only the app can still act on is unanswerable, whatever raised it; a card asks in the app."""
    if kind is T.PendingKind.IN_APP and source not in CARDS:
        return "unanswerable"
    return NEEDS.get(source, kind.value)


def need_act(need: T.PendingNeed | T.NeedsYouEvent, ended: bool) -> Act:
    return act(Say.NEEDS_YOU, need=need_of(need.kind, need.source), prompt=need.prompt, waiting_for=need.kind.value,
               after_end=ended)


# Actions, one per Do. Each takes (follow, row name, row, task, now, **context).


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


def _read_sent(f: Follow, name, row, task, now, result):
    if f.watching.get(task.link.thread_id) == task.task_id:
        # The held watch gives way to one anchored on the sent run, which the seam lets replace it.
        del f.watching[task.link.thread_id]
        f.opened_at.pop(task.task_id, None)
    _read_launched(f, name, row, task, now, result)


def _started(f: Follow, name, task, sequence):
    task.status = Status.LIVE
    if not task.announced:
        said = act(Say.LAUNCHED, request=task.instruction) if task.kind is Kind.START else act(Say.SENT_FOLLOWUP)
        f.speak(task, said, name, (sequence,))
        task.announced = True
    f.journal.save()


def _fail_refused(f: Follow, name, row, task, now, refusal):
    task.status = Status.FAILED
    if refusal.thread_id and task.link.thread_id is None:
        task.link.thread_id = refusal.thread_id
    f.budgets.pop(task.task_id, None)
    f.speak(task, refused(refusal, "start" if task.kind is Kind.START else "tell"), name)
    f.journal.save()


def _retry_launch(f: Follow, name, row, task, now, **_):
    f._retry(task, "launch", name, now)


def _retry_send(f: Follow, name, row, task, now, **_):
    f._retry(task, "send", name, now)


def _no_watch(f: Follow, name, row, task, now, **_):
    pass


REISSUE_OPS = {Kind.START: "launch", Kind.TELL: "send", Kind.STOP: "interrupt"}


def _reissue(f: Follow, name, row, task, now, **_):
    f.begun_at.setdefault(task.task_id, now)
    op = REISSUE_OPS[task.kind]
    if f.seam is None:
        f.budgets[task.task_id] = Budget(due_at=now, op=op)
        return
    OPS[op](f, task, now)


def _watch_from_cursor(f: Follow, name, row, task, now, **_):
    f.watch(task, now)


def _nothing(f: Follow, name, row, task, now, **_):
    pass


def _queue(f: Follow, name, row, task, now, **_):
    if task.task_id not in f.queue:
        f.queue.append(task.task_id)
    if task.task_id not in f.queued_said:
        f.queued_said.add(task.task_id)
        f.speak(task, row.says.with_slots(following=len(f.watching)), name, durable=False)


def _retry_watch(f: Follow, name, row, task, now, code=None, **_):
    f._retry(task, "watch", name, now, code)


def _give_up_now(f: Follow, name, row, task, now, code=None, **_):
    f.give_up(task, row_act(row, code), name)


def _read_then_watch(f: Follow, name, row, task, now, **_):
    f.read_first(task, now)


def _retry_read(f: Follow, name, row, task, now, code=None, **_):
    if code is C.UNAVAILABLE:
        f._retry(task, "read", name, now, code)
    else:
        f.give_up(task, row_act(row, code), name)


def _deleted(f: Follow, name, row, task, now, event):
    task.status = Status.FAILED
    f.speak(task, row.says, name, (event.sequence,))
    f.journal.save()


def _done(f: Follow, name, row, task, now, event):
    task.status = DONE_STATUS[event.status]
    f.speak(task, act(Say.DONE, status=event.status.value), name, (event.sequence, event.as_of_sequence))
    f.journal.save()
    f.windows[task.task_id] = now + DONE_WINDOW_S
    waiting = next((t for t in f.journal.by_thread(task.link.thread_id) if t.status is Status.LIVE), None)
    if waiting is not None:
        f.watch(waiting, now)


def _hold(f: Follow, name, row, task, now, refusal=None):
    # thread_busy delivered nothing, and the seam answers every later copy of a send it queued and then
    # cancelled with thread_busy again (send.ts), so the release needs a request id of its own.
    first = task.status is not Status.HELD
    task.status = Status.HELD
    task.link.request_id = ulid()
    f.journal.save()
    f.read_first(task, now)
    if first and task.status is Status.HELD:
        f.speak(task, row.says, name)
        f.journal.save()


def _release(f: Follow, name, row, task, now, **_):
    thread = task.link.thread_id
    f.releasing.add(thread)
    try:
        for held in f.journal.held(thread):
            f.send(held, now)
    finally:
        f.releasing.remove(thread)


def _drop(f: Follow, name, row, task, now, **_):
    task.status = Status.CANCELLED
    f.budgets.pop(task.task_id, None)
    f.speak(task, row.says, name)
    f.journal.save()
    f._release_watch(task)
    for other in f.journal.held(task.link.thread_id):
        f.watch(other, now)


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
    if row.do is Do.GIVE_UP_ALL:
        f.dead = name
    open_tasks = [t for t in f.journal.tasks.values() if not t.ended]
    for i, open_task in enumerate(open_tasks):
        f.give_up(open_task, row.says if i == 0 else None, name)
    f.queue.clear()


ACTIONS: dict[Do, Callable] = {
    Do.WATCH_LAUNCHED: _watch_launched,
    Do.READ_LAUNCHED: _read_launched,
    Do.FAIL_REFUSED: _fail_refused,
    Do.RETRY_LAUNCH: _retry_launch,
    Do.READ_SENT: _read_sent,
    Do.RETRY_SEND: _retry_send,
    Do.NO_WATCH: _no_watch,
    Do.REISSUE: _reissue,
    Do.WATCH_FROM_CURSOR: _watch_from_cursor,
    Do.NOTHING: _nothing,
    Do.QUEUE: _queue,
    Do.RETRY_WATCH: _retry_watch,
    Do.GIVE_UP_NOW: _give_up_now,
    Do.READ_THEN_WATCH: _read_then_watch,
    Do.RETRY_READ: _retry_read,
    Do.DELETED: _deleted,
    Do.DONE: _done,
    Do.RECONNECT_NOW: _reconnect_now,
    Do.RECONNECT_RETRY: _reconnect_retry,
    Do.GIVE_UP_ALL: _give_up_all,
    Do.HOLD: _hold,
    Do.RELEASE: _release,
    Do.DROP: _drop,
}

OPS: dict[str, Callable[[Follow, JournalTask, float], None]] = {
    "launch": Follow.launch,
    "send": Follow.send,
    "watch": Follow.watch,
    "read": Follow.read_first,
    "interrupt": Follow.stop,
}
