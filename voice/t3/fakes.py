"""A scripted seam and connector for lifecycle and task-service tests. No socket, no thread."""

from __future__ import annotations

from collections import deque
from typing import Any

from voice.t3 import types as T
from voice.t3.acts import Kind
from voice.t3.seam import SeamConnectionEnded, SeamRefused, SeamUnavailable
from voice.t3.pick.prefilter import prefilter
from voice.t3.verify import ELSEWHERE, NO_ANSWER, NONE, OWN_WORDS, UNCLEAR, Chosen, Picked, Reading, Verdict


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


def responded(thread="t1", pending="q1", decision="approve", sequence=96, replayed=False) -> T.RespondResult:
    return T.RespondResult(thread, pending, decision, sequence, replayed)


def answered(thread="t1", pending="q1", answers=(("db", "pg"),), sequence=97, replayed=False) -> T.AnswerResult:
    return T.AnswerResult(thread, pending, tuple(T.RecordedAnswer(q, a) for q, a in answers), sequence, replayed)


def question(qid="db", text="Which database should it use?", options=("Postgres", "SQLite"), own_words=False):
    return T.Question(qid, text, tuple(T.QuestionOption(label, f"{label} in more words") for label in options),
                      own_words)


def busy() -> SeamRefused:
    return refusal("thread_busy", "That thread is busy.")


def state(thread="t1", as_of=120, pending=None, activity=T.ThreadActivity.WORKING, title="Fix the flaky test",
          last_reply=None, project="p1", pending_detailed=True) -> T.ThreadState:
    return T.ThreadState(thread, project, title, activity, None, pending, last_reply, as_of, pending_detailed)


def need(request_id="q1", prompt="Run the tests?", kind=T.PendingKind.APPROVAL,
         source=T.NeedSource.COMMAND, questions=()) -> T.PendingNeed:
    return T.PendingNeed(request_id, kind, prompt, source, tuple(questions))


def run_event(seq, status=T.RunStatus.RUNNING, thread="t1", run="r1"):
    return T.RunEvent(thread, run, status, seq)


def reply(seq, message="m1", text="I added the helper. The tests pass.", thread="t1"):
    return T.ReplyEvent(thread, "r1", message, text, seq)


def needs_you(seq, request_id="q1", prompt="Run the tests?", thread="t1", kind=T.PendingKind.APPROVAL,
              source=T.NeedSource.COMMAND, questions=()):
    return T.NeedsYouEvent(thread, seq, request_id, kind, prompt, source, tuple(questions))


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

    def respond(self, request_id, thread_id, pending_request_id, decision):
        self.calls.append(("respond", request_id, thread_id, pending_request_id, decision))
        return self._answer("respond", unavailable())

    def answer_question(self, request_id, thread_id, pending_request_id, answers):
        self.calls.append(("answer_question", request_id, thread_id, pending_request_id, answers))
        return self._answer("answer_question", unavailable())

    def record_call(self, request_id, thread_id, call_id, started_at, ended_at, lines):
        self.calls.append(("record_call", request_id, thread_id, call_id, started_at, ended_at, lines))
        return self._answer("record_call", T.RecordResult(thread_id, "turn-item:voice-call:x", len(lines), False, 99,
                                                          False))

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


class Agree:
    """A decider that passes every line it is asked about, so a test sees only code's slot check."""

    calls = 0

    def verify(self, acts, line):
        self.calls += 1
        return Verdict(True)

    def reuses(self, title, gist, asked, question):
        return False


class Reader(Agree):
    """A decider whose reading of each reply is the test's own: reply to label, or to (label, the option it asks for),
    where the option `ELSEWHERE` is something that was not offered. A reply the test did not script reads as
    unrelated, except one that names a thread it was asked to choose. What d1.2 really reads is in
    voice/t3/test_replies.py."""

    def __init__(self, readings: dict, picking: dict | None = None, choosing: dict | None = None):
        self.readings, self.asked = readings, []
        self.picking, self.picked = picking or {}, []
        self.choosing, self.chosen = choosing or {}, []
        self.answering = []  # the words each pick was made for as an answer to a thread's question
        self.call_threads = []  # the call's own thread each pick was shown, or None
        self.spoken = []  # the thread just spoken about each pick was shown, or None
        self.naming = {}  # request to P(it says the picked thread's title); unscripted is no answer
        self.joining = {}  # (first, second) to P(they are one question cut in two); unscripted is no answer

    def chooses(self, question, options, reply, own_words=False):
        """The test's choice for `reply`: the 0-based index of an option, OWN_WORDS, UNCLEAR or NO_ANSWER, or a Chosen
        as it is. A reply the test did not script answers nothing. What d1.2 really chooses is measured by
        voice/t3/eval/answer_choice.py."""
        self.chosen.append((question, tuple(options), reply, own_words))
        scripted = self.choosing.get(reply, NO_ANSWER)
        if isinstance(scripted, Chosen):
            return scripted
        label = f"option_{scripted + 1:02d}" if isinstance(scripted, int) else scripted
        return Chosen(label, {label: 0.9})

    def names(self, request, thread):
        return self.naming.get(request)

    def one_question(self, first, second, title):
        return self.joining.get((first, second))

    def picks(self, request, threads, recent=None, answering=False, call_thread=None, spoken=None):
        """The test's pick for `request`: a thread id, NONE or UNCLEAR, or each thread's share. A request the test
        did not script is picked by matching its words against the titles, and leans (0.8) to a lone match, so it is
        confirmed. What d1.2 really picks is in voice/t3/test_picks.py."""
        self.picked.append((request, tuple(threads), recent))
        self.call_threads.append(call_thread)
        self.spoken.append(spoken)
        if answering:
            self.answering.append(request)
        scripted = self.picking.get(request)
        if isinstance(scripted, dict):
            return Picked(max(scripted, key=scripted.get), dict(scripted))
        if scripted is not None:
            return Picked(scripted, {scripted: 0.8})
        match = prefilter(request, [{"threadId": t.thread_id, "title": t.title, "projectId": t.project,
                                     "activity": t.activity} for t in threads], {t.project: t.project for t in threads})
        hits = match.candidates
        if len(hits) == 1:
            return Picked(hits[0], {hits[0]: 0.8})
        if hits:
            return Picked(UNCLEAR, {UNCLEAR: 0.4, **{hit: 0.6 / len(hits) for hit in hits}})
        about = next((t.thread_id for t in threads if not match.words and t.title == recent), None)
        if about is not None:
            return Picked(about, {about: 0.8})
        return Picked(NONE, {NONE: 0.9}) if match.words and not match.state_only else Picked(UNCLEAR, {UNCLEAR: 0.9})

    def reads(self, said, reply, options=()):
        self.asked.append((said, reply, tuple(options)))
        label, target = (self.readings.get(reply, "unrelated"), None)
        if isinstance(label, tuple):
            label, target = label
        elif reply not in self.readings and said.kind is Kind.ASK_WHICH and said.slots.get("of") == "thread":
            # An unscripted reply to "which thread" chooses the one title its words fit, as d1.2 does by name.
            named = prefilter(reply, [{"threadId": title, "title": title, "projectId": ""} for title in options])
            if len(named.candidates) == 1:
                label, target = "yes", named.candidates[0]
        # A scripted "withdraw" is a no that takes the whole request back; a yes says yes and asks nothing.
        withdraws = 1.0 if label == "withdraw" else 0.0
        label = "no" if label == "withdraw" else label
        shares = {"agrees": 1.0 if label == "yes" else 0.0, "asks": 1.0 if label == "question" else 0.0,
                  "withdraws": withdraws, "adds": 0.0}
        whole = {name: 1.0 if name == label else 0.0 for name in ("yes", "no", "question", "other", "unrelated")}
        kept = target is None and label != "other"
        if target == ELSEWHERE:
            return Reading(label, whole, elsewhere=True, complete=True, **shares)
        return Reading(label, whole, target=target, complete=True, kept=kept, **shares)
