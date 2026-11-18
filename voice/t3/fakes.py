"""A scripted seam and connector for lifecycle and task-service tests. No socket, no thread."""

from __future__ import annotations

from collections import deque
from typing import Any

from voice.t3 import types as T
from voice.t3.seam import SeamConnectionEnded, SeamRefused, SeamUnavailable


def hello(max_watches: int = 16, projects=(("p1", "Voice sandbox", ("sandbox", "voice sandbox")),)) -> T.Hello:
    return T.Hello(
        ceiling=T.Ceiling(T.RuntimeMode.AUTO_ACCEPT_EDITS, "default", tuple(T.Operation), max_watches),
        projects=tuple(T.Project(pid, name, tuple(spoken)) for pid, name, spoken in projects),
        latest_sequence=1,
    )


def refusal(code: str, message: str = "No.", reason: str | None = None, thread_id: str | None = None) -> SeamRefused:
    return SeamRefused(T.ResultErr(T.RefusalCode(code), message, reason, None, thread_id))


def unavailable() -> SeamUnavailable:
    return SeamUnavailable("unavailable")


def launched(thread="t1", run="r1", sequence=93, replayed=False) -> T.LaunchResult:
    return T.LaunchResult(thread, run, T.RuntimeMode.AUTO_ACCEPT_EDITS, "Showcase", sequence, replayed)


def sent(thread="t1", run="r2", sequence=190, replayed=False, delivery=T.Delivery.STARTED) -> T.SendResult:
    return T.SendResult(thread, f"m-{run}", run, delivery, sequence, replayed)


def requested(run="r1", sequence=95) -> T.InterruptRequested:
    return T.InterruptRequested(run, sequence)


def busy() -> SeamRefused:
    return refusal("thread_busy", "That thread is busy.")


def state(thread="t1", as_of=120, pending=None, activity=T.ThreadActivity.WORKING, title="Fix the flaky test",
          last_reply=None, project="p1") -> T.ThreadState:
    return T.ThreadState(thread, project, title, activity, None, pending, last_reply, as_of)


def need(request_id="q1", prompt="Run the tests?", kind=T.PendingKind.APPROVAL) -> T.PendingNeed:
    return T.PendingNeed(request_id, kind, prompt)


def run_event(seq, status=T.RunStatus.RUNNING, thread="t1", run="r1"):
    return T.RunEvent(thread, run, status, seq)


def reply(seq, message="m1", text="I added the helper. The tests pass.", thread="t1"):
    return T.ReplyEvent(thread, "r1", message, text, seq)


def needs_you(seq, request_id="q1", prompt="Run the tests?", thread="t1"):
    return T.NeedsYouEvent(thread, seq, request_id, T.PendingKind.APPROVAL, prompt)


def resolved(seq, request_id="q1", thread="t1"):
    return T.ResolvedEvent(thread, request_id, T.ResolvedStatus.RESOLVED, seq)


def done(seq, as_of=None, status=T.TerminalStatus.COMPLETED, text="I added clamp. The tests pass.",
         message="m9", thread="t1", run="r1"):
    return T.DoneEvent(thread, run, status, T.RunResult(text, message, None), seq,
                       as_of if as_of is not None else seq + 1)


def gone(seq, thread="t1"):
    return T.GoneEvent(thread, seq)


def ended(reason: str, thread="t1"):
    return T.WatchEnded(thread, T.WatchEndReason(reason))


class FakeSeam:
    """Every call is recorded in `calls`. An answer queued in `answers[op]` is returned or raised once."""

    def __init__(self, max_watches: int = 16, read: T.ThreadState | None = None, projects=None):
        self.hello = hello(max_watches) if projects is None else hello(max_watches, projects)
        self.calls: list[tuple] = []
        self.answers: dict[str, deque] = {}
        self.items: deque = deque()
        self.read = read
        self.listing: T.ThreadList | None = None
        self.closed = False

    def answer(self, op: str, *values: Any) -> FakeSeam:
        self.answers.setdefault(op, deque()).extend(values)
        return self

    def push(self, *items: Any) -> None:
        self.items.extend(items)

    def _answer(self, op: str, default: Any) -> Any:
        queued = self.answers.get(op)
        value = queued.popleft() if queued else default
        if isinstance(value, Exception):
            raise value
        return value

    def launch(self, request_id, project_id, instruction):
        self.calls.append(("launch", request_id, project_id, instruction))
        return self._answer("launch", launched())

    def send(self, request_id, thread_id, text):
        self.calls.append(("send", request_id, thread_id, text))
        return self._answer("send", unavailable())

    def interrupt(self, request_id, thread_id):
        self.calls.append(("interrupt", request_id, thread_id))
        return self._answer("interrupt", unavailable())

    def watch(self, thread_id, after_sequence, run_id=None):
        self.calls.append(("watch", thread_id, after_sequence, run_id))
        return self._answer("watch", T.WatchStarted(thread_id))

    def unwatch(self, thread_id):
        self.calls.append(("unwatch", thread_id))
        return T.WatchStarted(thread_id)

    def read_thread(self, thread_id):
        self.calls.append(("read", thread_id))
        return self._answer("read", self.read or state(thread_id))

    def list_threads(self, project_id=None, limit=None):
        self.calls.append(("list",))
        return self._answer("list", self.listing)

    def drain(self, timeout=None):
        while self.items:
            item = self.items.popleft()
            yield item
            if isinstance(item, T.SocketEnd):
                return

    def close(self):
        self.closed = True

    def ops(self, op: str) -> list[tuple]:
        return [c for c in self.calls if c[0] == op]


class Connector:
    """Each connect pops the next entry: a FakeSeam is returned, a SocketEnd is raised as a failed upgrade."""

    def __init__(self, *entries: Any):
        self.entries = deque(entries)
        self.count = 0

    def __call__(self):
        self.count += 1
        entry = self.entries.popleft() if self.entries else T.UNREACHABLE
        if isinstance(entry, FakeSeam):
            return entry
        raise SeamConnectionEnded(entry)
