"""The decider's check of each worded T3 line against its acts: typed yes/no questions, never text.

Code's slot check (voice/t3/acts.py) sees named values and a few status words. It cannot see a line that names the
right thread and says the wrong thing about it: "Alpha is complete?" for a stop confirm, "Alpha has failed." for a
working status, or "Beta is running. Alpha is unrelated." for Alpha's status. So after the slot check passes, the
hosted decider (d1.2, voice/liquid_decider) answers one set of yes/no questions per line, built from its acts: is
the line about the act's thread, does it say what the act means, and does it claim anything the acts do not say.
A line is spoken only when every answer falls on the side its act requires. A timeout, an error or a spend stop is
a failed check, so the line is never spoken on it.

The decider classifies; it writes nothing (Mason, 2026-11-18). The question text below is for the decider only.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

from voice.t3.acts import QUESTIONS, Act, Kind, forbidden_claims

VERIFY_TIMEOUT_S = 2.0
YES = 0.5

# What an act means, as the clause a question checks: "Is the line a question that asks <clause>?" for a confirm
# act, else "Does the line tell them that <clause>?". Slots fill the braces.
MEANS: dict[Kind, str] = {
    Kind.CONFIRM_LAUNCH: "whether to start a new coding thread in {project}",
    Kind.ASK_WHICH: "which one they mean of: {candidates}",
    Kind.NO_PROJECT: "the assistant cannot find any project to use",
    Kind.LAUNCH_PENDING: "T3 was asked to start the thread and has not confirmed it yet",
    Kind.LAUNCHED: "a new coding thread started in {project}",
    Kind.LAUNCH_UNCONFIRMED: "T3 never confirmed that the thread started",
    Kind.NO_THREAD: "no thread matches what they asked about",
    Kind.CONFIRM_THREAD: "whether {thread} is the thread they mean",
    Kind.CONFIRM_TELL: "whether to send the message \"{message}\" to {thread}",
    Kind.CONFIRM_STOP: "whether to stop {thread}",
    Kind.CANCELLED: "the assistant will not {op}",
    Kind.THREAD_STATUS: "{thread} is {status}",
    Kind.TELL_PENDING: "T3 was asked to pass the message to {thread} and has not confirmed it yet",
    Kind.SENT_FOLLOWUP: "the message reached {thread}",
    Kind.TELL_UNCONFIRMED: "T3 never confirmed that the message reached {thread}",
    Kind.HELD_FOLLOWUP: "{thread} is still working, so the message waits until its run ends",
    Kind.DROPPED_FOLLOWUP: "the held message for {thread} will not be sent",
    Kind.STOPPING: "{thread} is being stopped now",
    Kind.NOTHING_RUNNING: "{thread} had no run going, so there was nothing to stop",
    Kind.STOP_UNCONFIRMED: "the assistant could not reach T3 to stop {thread}",
    Kind.RUN_UPDATE: "{thread} is {status}",
    Kind.REPLIED: "{thread} posted a reply about its work",
    Kind.NEEDS_YOU: "{thread} is waiting on them for {need}",
    Kind.NEED_RESOLVED: "a request {thread} made of them was {status}",
    Kind.DONE: "the run of {thread} ended as {status}",
    Kind.THREAD_DELETED: "{thread} was deleted",
    Kind.LOST_TRACK: "the assistant lost track of {thread}",
    Kind.THREAD_GONE: "the assistant can no longer see {thread}",
    Kind.WATCH_QUEUED: "the assistant will follow {thread} once one of the threads it follows ends",
    Kind.UNREACHABLE: "the assistant cannot reach T3 right now",
    Kind.SEAM_DOWN: "the assistant stopped following their threads",
    Kind.REFUSED: "T3 refused to {op}",
    Kind.LEGACY_UPDATE: "the same news as this older line: {text}",
}
assert set(MEANS) == set(Kind)

STATUS = {"working": "still working", "running": "running", "queued": "queued to run",
          "waiting_on_you": "waiting on them", "waiting_on_background": "waiting on background work", "idle": "idle",
          "finished": "finished", "completed": "finished", "failed": "failed",
          "interrupted": "interrupted before it finished", "cancelled": "cancelled", "rolled_back": "rolled back",
          "lost": "lost", "resolved": "answered", "expired": "expired"}
NEED = {"command_approval": "approval to run a command", "file_change": "approval to change files",
        "file_read": "approval to read a file", "permission": "permission to go on", "tool_input": "input to a tool",
        "question": "an answer to its question", "link": "something only the app can take",
        "secret": "something only the app can take", "card": "something only the app can take",
        "unanswerable": "a request no one can answer any more", "approval": "an approval",
        "in_app": "something only the app can take"}
# "finished" means the run ended while it runs, and a successful end once it ended otherwise. One wording for both
# refused right lines either way: an interrupted run "has ended", and a working thread's summary reads as completed
# work (measured on the fixture, 2026-11-18). A reply is never asked it (UNASKED).
CLAIMS = {"finished": "the run of {thread} has already ended", "stopped": "{thread} has already stopped or was cancelled",
          "started": "the thread has already started", "sent": "the message has already been sent",
          "working": "{thread} is still working or running"}
CLAIMS_ONCE_ENDED = {"finished": "{thread} completed its work successfully"}
ENDED_UNFINISHED = frozenset({"interrupted", "cancelled"})
OPS = {"start": "start the thread", "ask": "look the thread up", "tell": "send the message", "stop": "stop the thread"}


@dataclass(frozen=True)
class Ask:
    """One yes/no question about a line, and the answer its act requires."""

    name: str
    question: str
    want: bool

    def problem(self) -> str:
        return f"a checker answered {'no' if self.want else 'yes'} to: {self.question}"


@dataclass(frozen=True)
class Verdict:
    passed: bool
    answers: dict[str, float] = field(default_factory=dict)
    failed: tuple[str, ...] = ()
    ms: float = 0.0
    error: str | None = None
    problems: tuple[str, ...] = ()


def _fill(template: str, slots: dict) -> str:
    values = dict(slots)
    values["status"] = STATUS.get(str(slots.get("status")), str(slots.get("status")))
    values["need"] = NEED.get(str(slots.get("need")), "something from them")
    values["op"] = OPS.get(str(slots.get("op")), "do it")
    if isinstance(slots.get("candidates"), (list, tuple)):
        values["candidates"] = ", ".join(slots["candidates"])
    values.setdefault("thread", "the thread")
    values.setdefault("project", "the project")
    return template.format_map(_Blank(values))


class _Blank(dict):
    def __missing__(self, key):
        return "it"


def asks(acts: Iterable[Act]) -> list[Ask]:
    """The questions a line for `acts` must answer as given: per act, whether it is about the act's thread and says
    what the act means; per status claim the acts forbid, whether the line makes it; then once whether it claims
    anything the acts do not."""
    acts = list(acts)
    out = []
    for n, a in enumerate(acts, 1):
        means = _fill(MEANS[a.kind], a.slots)
        if "thread" in a.slots:
            out.append(Ask(f"a{n}_about", f"Is the line about the thread named \"{a.slots['thread']}\"?", True))
        elif a.kind is Kind.CANCELLED:
            out.append(Ask(f"a{n}_names_target",
                           "Does the cancellation name a specific thread or project? Generic references like "
                           "'the thread' or 'it' do not count.", False))
        if a.kind in QUESTIONS:
            out.append(Ask(f"a{n}_means", f"Is the line a question that asks {means}?", True))
        else:
            out.append(Ask(f"a{n}_means", f"Does the line tell them that {means}?", True))
            out.append(Ask(f"a{n}_asks_unrequested",
                           "Does the line ask the person a question the act did not call for?", False))
    # Without these, "I've stopped X, right?" passed for a stop confirm and "X has stopped." for a stop just asked
    # for: the meaning question misses tense (measured on the fixture, 2026-11-18).
    subject = next((a.slots["thread"] for a in acts if "thread" in a.slots), "the thread")
    for claim, wording in asked_claims(acts).items():
        out.append(Ask(f"claims_{claim}", f"Does the line say that {_fill(wording, {'thread': subject})}?", False))
    # Of three phrasings probed on the six fixture lines, this one kept the widest gap (measured, 2026-11-18).
    out.append(Ask("invents", "Does the line state something about a thread that `facts` does not support?", False))
    return out


# A reply reports its own work ("I added clamp, the tests pass"), which d1.2 reads as the run ending on every smoke
# retry (measured, 2026-11-18). Its run status rides in its facts, so the unsupported-claim question still refuses
# a reply line that says the thread is done (PM 2 ruling, 2026-11-18).
UNASKED: dict[Kind, frozenset[str]] = {Kind.REPLIED: frozenset({"finished"})}


def asked_claims(acts: Iterable[Act]) -> dict[str, str]:
    """The forbidden status claims the decider is asked about, each with its wording: each act's own, less those it
    is never asked, worded for the first act that asks it."""
    acts = list(acts)
    allowed = forbidden_claims(acts)
    asked: dict[str, str] = {}
    for a in acts:
        ended = str(a.slots.get("status")) in ENDED_UNFINISHED
        for claim in sorted(forbidden_claims([a]) - UNASKED.get(a.kind, frozenset())):
            if claim in allowed and claim not in asked:
                asked[claim] = (CLAIMS_ONCE_ENDED if ended else {}).get(claim, CLAIMS[claim])
    return dict(sorted(asked.items()))


def request(acts: Iterable[Act], line: str) -> dict:
    """One decisions body: the line and its acts' facts as state, and a noul question per Ask. An act with no thread
    carries what it means ("says"), because the unsupported-claim question is about a thread and refused "Should I
    start a new coding thread in Voice sandbox?" three times in three on the 2026-11-18 rehearsal."""
    acts = list(acts)
    state = {"line": line, "facts": [{"act": a.kind.value, **a.slots, **({} if "thread" in a.slots else {"says": _fill(MEANS[a.kind], a.slots)})}
                         for a in acts],
             "context": "A voice assistant is about to say `line` to the person it is talking to, about their coding "
                        "threads in an app called T3. `facts` is everything it may say."}
    questions = {ask.name: {"type": "noul", "instructions": ask.question,
                            "criteria": {"true": "Yes.", "false": "No."}} for ask in asks(acts)}
    return {"model": "d1.2", "state": state, "questions": questions}


def judge(acts: Iterable[Act], answers: Any) -> tuple[bool, dict[str, float], tuple[str, ...], tuple[str, ...]]:
    """Each question's P(yes), and the questions whose answer is missing or on the wrong side."""
    probabilities, failed, problems = {}, [], []
    for ask in asks(acts):
        answer = answers.get(ask.name) if isinstance(answers, dict) else None
        value = answer.get("noul") if isinstance(answer, dict) and answer.get("type") == "noul" else None
        if not isinstance(value, (int, float)) or not 0 <= value <= 1:
            failed.append(ask.name)
            problems.append(f"the decider gave no answer to: {ask.question}")
            continue
        probabilities[ask.name] = float(value)
        if (value >= YES) != ask.want:
            failed.append(ask.name)
            problems.append(ask.problem())
    return not failed, probabilities, tuple(failed), tuple(problems)


@dataclass
class Decider:
    """The line check over a decider client (`call(route, body, label)`), one request in flight."""

    client: Any
    lock: threading.Lock = field(default_factory=threading.Lock)
    calls: int = 0

    def verify(self, acts: Iterable[Act], line: str) -> Verdict:
        acts = list(acts)
        started = time.perf_counter()
        try:
            with self.lock:
                self.calls += 1
                record = self.client.call("decisions", request(acts, line), label="t3_line")
        except Exception as exc:
            # A spend or billing stop, a timeout, or a dropped connection: the line is not spoken on it.
            return Verdict(False, ms=_ms(started), error=f"{type(exc).__name__}: {exc}")
        if record.get("status") != 200:
            return Verdict(False, ms=_ms(started), error=f"decider status {record.get('status')}")
        passed, answers, failed, problems = judge(acts, (record.get("response") or {}).get("answers"))
        return Verdict(passed, answers, failed, _ms(started), problems=problems)

    def reads(self, said: Act, reply: str, options: tuple[str, ...] = ()) -> Reading:
        """What `reply` does about the question `said` asked, and which of `options` it changes to when it asks for
        something else. A timeout or any error reads as nothing, and nothing never acts."""
        try:
            with self.lock:
                self.calls += 1
                record = self.client.call("decisions", reading_request(said, reply, options), label="t3_reply")
        except Exception as exc:
            return Reading(None, error=f"{type(exc).__name__}: {exc}")
        if record.get("status") != 200:
            return Reading(None, error=f"decider status {record.get('status')}")
        return judge_reading((record.get("response") or {}).get("answers"), options)


READ_CRITERIA = {
    "yes": "It agrees to exactly what the assistant asked, with nothing added, changed or conditional.",
    "no": "It declines what the assistant asked, or takes it back.",
    "question": "It asks the assistant something, or asks about what was proposed, instead of answering.",
    "other": "It agrees but changes the project, thread or terms, adds a condition, or names something else.",
    "unrelated": "It does not answer what the assistant asked.",
}
# The reply's own label with most of the weight is a yes. The recorded yes replies score 0.60 and up, and the most any
# other reply gives yes is 0.36.
YES_CONFIDENCE = 0.5
# A reply that is any share a question or another target is not an unconditional yes, however much of it says yes.
OTHER_CEILING = 0.2
OPTION_CONFIDENCE = 0.6
ELSEWHERE_CONFIDENCE = 0.5
KEEPS, ELSEWHERE = "keeps", "elsewhere"


def reading_request(said: Act, reply: str, options: tuple[str, ...] = ()) -> dict:
    """One decisions body asking what the person's reply does about the question `said` asked them. `reply` is the
    transcript, whose punctuation need not match how they said it: a rising "please do" arrives with a question mark.
    `options` are the other things the reply could be asking for, by name, and add a question of whether it keeps
    what was proposed, changes to one of them, or changes without saying which."""
    state = {"assistant_asked": _fill(MEANS[said.kind], said.slots), "reply": reply,
             "context": "A voice assistant asked the person a yes or no question about their coding threads. `reply` "
                        "is a transcript of what they said back."}
    questions = {"reading": {"type": "choice", "instructions": "What does the person's reply do about what the "
                                                               "assistant asked?", "criteria": READ_CRITERIA}}
    if options:
        state["alternatives"] = list(options)
        criteria = {KEEPS: "It keeps what the assistant proposed, or does not say to change it."}
        criteria.update({f"option_{i}": f"It changes to {name!r}." for i, name in enumerate(options, 1)})
        criteria[ELSEWHERE] = ("It changes away from what was proposed to another of the alternatives, without making "
                               "clear which one.")
        questions["target"] = {"type": "choice", "criteria": criteria,
                               "instructions": "The assistant proposed one thing, and `alternatives` lists every other "
                                               "thing it could be. Does the reply keep what was proposed, or change "
                                               "to one of the alternatives?"}
    return {"model": "d1.2", "state": state, "questions": questions}


@dataclass(frozen=True)
class Reading:
    """What a reply does about a pending question, by the decider. `target` is the option it changes to, and
    `elsewhere` says it changes to another option without making clear which."""

    label: str | None
    probabilities: dict[str, float] = field(default_factory=dict)
    error: str | None = None
    target: str | None = None
    elsewhere: bool = False

    @property
    def yes(self) -> bool:
        """An unconditional yes: the reply's own label, confident, with no real share of a question, a changed target
        or a condition. Anything short of that is not a yes."""
        p = self.probabilities
        return (self.label == "yes" and p.get("yes", 0.0) >= YES_CONFIDENCE
                and max(p.get("other", 0.0), p.get("question", 0.0)) <= OTHER_CEILING)

    @property
    def kind(self) -> str | None:
        """"yes", "no", or None for a reply that neither agrees outright nor declines."""
        return "yes" if self.yes else "no" if self.label == "no" else None


def _scores(answer: Any, labels) -> dict[str, float] | None:
    """A choice answer's shares when its label is one of `labels` and every share is a number from 0 to 1."""
    probabilities = answer.get("probabilities") if isinstance(answer, dict) else None
    choice = answer.get("choice") if isinstance(answer, dict) else None
    if (not isinstance(probabilities, dict) or not isinstance(choice, str) or choice not in labels
            or not set(probabilities) <= set(labels)):
        return None
    for value in probabilities.values():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0.0 <= value <= 1.0:
            return None
    return {label: float(value) for label, value in probabilities.items()}


def judge_reading(answers: Any, options: tuple[str, ...] = ()) -> Reading:
    """The decider's answer as a Reading. A label that is not an option, or a score that is not a number from 0 to 1,
    is no reading at all, never an error. A change of target is kept only when the decider is confident of it."""
    answers = answers if isinstance(answers, dict) else {}
    scores = _scores(answers.get("reading"), READ_CRITERIA)
    if scores is None:
        return Reading(None, error="the decider gave no reading")
    names = {f"option_{i}": name for i, name in enumerate(options, 1)}
    picked = answers.get("target") if options else None
    shares = _scores(picked, {*names, KEEPS, ELSEWHERE}) if picked is not None else None
    share = shares.get(picked["choice"], 0.0) if shares is not None else 0.0
    target = names.get(picked["choice"]) if share >= OPTION_CONFIDENCE else None
    elsewhere = shares is not None and picked["choice"] == ELSEWHERE and share >= ELSEWHERE_CONFIDENCE
    return Reading(answers["reading"]["choice"], scores, target=target, elsewhere=elsewhere)


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 1)


def default_decider() -> Decider | None:
    """Liquid's hosted d1.2 with one attempt and a short timeout, when its key is configured. Without one no T3 line
    can pass, and every act waits."""
    if not (os.environ.get("LIQUID_API_KEY") or os.environ.get("VOICE_ENV_FILE")):
        return None
    from voice.liquid_decider.client import LiquidClient
    from voice.segmentation.client import Pacer

    return Decider(LiquidClient(pacer=Pacer(first_s=0.0, cap_s=0.0), timeout_s=VERIFY_TIMEOUT_S, max_attempts=1))
