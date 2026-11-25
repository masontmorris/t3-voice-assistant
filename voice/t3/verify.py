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

import math
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

from voice.t3.acts import QUESTIONS, Act, Kind, forbidden_claims, outcome_meaning

# The acts that can say what a pending request is about (speech.REQUEST_ACTS).
REQUEST_ACTS = frozenset({Kind.THREAD_STATUS, Kind.NEEDS_YOU, Kind.CORRECTION, Kind.NEEDS_THE_APP})

VERIFY_TIMEOUT_S = 2.0
YES = 0.5
# The acts whose line says the thread's summary after its status (speech.MEANINGS).
SAYS_SUMMARY = frozenset({Kind.THREAD_STATUS, Kind.DONE, Kind.CONFIRM_RESPOND})

# Whether a summary written for other words answers the question now asked: a choice, so the decider makes it.
REUSE_CONTEXT = ("A voice assistant holds a short spoken `summary` of a coding thread. It was written for the words "
                 "`summary_was_for`, or, when that is null, to say what the thread reported. The person has now asked "
                 "`question`, or, when that is null, wants to hear what the thread reported.")
REUSE_QUESTION = "Does `summary` answer `question`?"
# P(yes) a reuse needs. Neither decider reuses a wrong summary at it in voice/t3/fixtures/summary_reuse.json.
REUSE_YES = 0.8

# What an act means, as the clause a question checks: "Is the line a question that asks <clause>?" for a confirm
# act, else "Does the line tell them that <clause>?". Slots fill the braces.
MEANS: dict[Kind, str] = {
    Kind.CONFIRM_LAUNCH: "whether to start a new coding thread in {project}",
    Kind.ASK_WHICH: "which one they mean of: {candidates}",
    Kind.NO_PROJECT: "the assistant cannot find any project to use",
    Kind.LAUNCH_PENDING: "T3 was asked to start the thread and has not confirmed it yet",
    Kind.LAUNCHED: "a new coding thread started in {project}",
    Kind.LAUNCH_UNCONFIRMED: "T3 never confirmed that the thread started",
    Kind.NOT_ASKED: "the assistant did not get to another thing they asked for, so nothing was done about it",
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
    Kind.SAME_TITLE: ("more than one thread is called {thread}, so the assistant could not tell which one they meant, did not "
                      "send a message or stop anything, and they can do it in the app"),
    Kind.WATCH_QUEUED: "the assistant will follow {thread} once one of the threads it follows ends",
    Kind.UNREACHABLE: "the assistant cannot reach T3 right now",
    Kind.SEAM_DOWN: "the assistant stopped following their threads",
    Kind.REFUSED: "T3 refused to {op}",
    Kind.LEGACY_UPDATE: "the same news as this older line: {text}",
    Kind.UNANSWERED: "the assistant could not give an answer",
    Kind.NOTHING_DONE: "the assistant has not started, sent, stopped or approved anything, and what it said a moment ago about doing so was wrong",
    Kind.CORRECTION: "what the assistant said a moment ago about {thread} was wrong, and {thread} is {status}",
    Kind.CONFIRM_RESPOND: "whether to {decision} the request {thread} is waiting on, which is {need}",
    Kind.RESPONDED: "the request {thread} was waiting on was {decided}",
    Kind.NOTHING_PENDING: "{thread} is not waiting on any request, so there was nothing to answer",
    Kind.NEEDS_THE_APP: ("the request {thread} is waiting on, which is {need}, can only be approved in the app, not by "
                         "voice"),
    Kind.ASK_QUESTION: ("which option they choose for the question {thread} is asking them, \"{question}\", the options "
                        "being: {options}"),
    Kind.CONFIRM_ANSWER: "whether to give {thread} the answer: {answers}",
    Kind.ANSWERED: "{thread} was given the answer: {answers}",
    Kind.ANSWER_IN_APP: "{thread}'s question cannot be answered by voice, so they have to answer it in the app",
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
NEED_KIND_YES = 0.9
# "finished" means the run ended while it runs, and a successful end once it ended otherwise. One wording for both
# refused right lines either way: an interrupted run "has ended", and a working thread's summary reads as completed
# work (measured on the fixture, 2026-11-18). A reply is never asked it (UNASKED).
CLAIMS = {"finished": "the run of {thread} has already ended", "stopped": "{thread} has already stopped or was cancelled",
          "started": "the thread has already started", "sent": "the message has already been sent",
          "working": "{thread} is still working or running",
          "answered": "the request was already approved or denied, as something that has happened"}
# A claim the decider reads from a question about approving at 0.61 and from a real claim at 0.80 and up (local d1-2B, 8
# made-up confirm_respond lines), so this one claim is held to a higher bar than YES.
CLAIM_BARS = {"answered": 0.75}
CLAIMS_ONCE_ENDED = {"finished": "{thread} completed its work successfully"}
ENDED_UNFINISHED = frozenset({"interrupted", "cancelled"})
OPS = {"start": "start the thread", "ask": "look the thread up", "tell": "send the message", "stop": "stop the thread",
       "respond": "answer the request", "answer": "give the answer"}


@dataclass(frozen=True)
class Ask:
    """One yes/no question about a line, and the answer its act requires."""

    name: str
    question: str
    want: bool
    fix: str = ""
    bar: float = 0.5

    def problem(self) -> str:
        return self.fix or f"a checker answered {'no' if self.want else 'yes'} to: {self.question}"


@dataclass(frozen=True)
class Verdict:
    passed: bool
    answers: dict[str, float] = field(default_factory=dict)
    failed: tuple[str, ...] = ()
    ms: float = 0.0
    error: str | None = None
    problems: tuple[str, ...] = ()


# A question with no options to choose among asks for what they want to say.
OPEN_QUESTION = "what they want to answer to the question {thread} is asking them, \"{question}\""

# Per value of a list slot, the question that it is said as what it is: an option offered, an answer asked about or given.
OFFERED: dict[Kind, tuple[tuple[str, str], ...]] = {
    Kind.CONFIRM_ANSWER: (("answers", 'Does the line say that the answer "{value}" would be given?'),),
    Kind.ANSWERED: (("answers", 'Does the line say that the answer "{value}" was given?'),),
}


def _fill(template: str, slots: dict) -> str:
    values = dict(slots)
    values["status"] = STATUS.get(str(slots.get("status")), str(slots.get("status")))
    values["decision"] = slots.get("decision", "answer")
    values["decided"] = {"approve": "approved", "deny": "denied"}.get(str(slots.get("decision")), "answered")
    values["need"] = NEED.get(str(slots.get("need")), "something from them")
    values["op"] = OPS.get(str(slots.get("op")), "do it")
    for name, joiner in (("candidates", ", "), ("options", ", "), ("answers", "; "), ("questions", "; ")):
        if isinstance(slots.get(name), (list, tuple)):
            values[name] = joiner.join(slots[name])
    values.setdefault("thread", begun_phrase(slots) or "the thread")
    values.setdefault("project", "the project")
    return template.format_map(_Blank(values))


class _Blank(dict):
    def __missing__(self, key):
        return "it"


def begun_phrase(slots: dict) -> str | None:
    """How the checker's questions name a thread that has no title: as the call's own, or by its project and what it
    was started for."""
    if slots.get("on_call"):
        return "this thread, the one the call was opened on"
    if not slots.get("begun_in"):
        return None
    return f"the thread started for them in {slots['begun_in']}" + (
        f" for {slots['begun_for']}" if slots.get("begun_for") else "")


# The line that drops a thread-pick question the caller took back. Checked as "will not look the thread up", a line
# that said a message or a stop was off, or that named the threads again, could pass.
DROPPED_PICK = ("the assistant is leaving their question about which thread they meant, and will not look into any of "
                "those threads now")


def act_meaning(a: Act) -> str:
    if a.kind is Kind.UNANSWERED and (outcomes := a.slots.get("outcomes")):
        return "the assistant " + outcome_meaning(outcomes)
    if a.kind is Kind.NO_THREAD:
        if a.slots.get("other"):
            return "no other thread matches what they asked about, besides the one they turned down"
        return "no thread matches what they asked about" + (f", after the assistant checked {a.slots['recent']} "
                                                            "recent threads" if a.slots.get("recent") else "")
    if a.kind is Kind.CANCELLED and a.slots.get("op") == "ask":
        return DROPPED_PICK
    return _fill(OPEN_QUESTION if a.kind is Kind.ASK_QUESTION and not a.slots.get("options") else MEANS[a.kind], a.slots)


def asks(acts: Iterable[Act]) -> list[Ask]:
    """The questions a line for `acts` must answer as given: per act, whether it is about the act's thread and says
    what the act means; per status claim the acts forbid, whether the line makes it; then once whether it claims
    anything the acts do not."""
    acts = list(acts)
    out = []
    for n, a in enumerate(acts, 1):
        means = act_meaning(a)
        if "thread" in a.slots:
            out.append(Ask(f"a{n}_about", f"Is the line about the thread named \"{a.slots['thread']}\"?", True))
        elif a.kind is Kind.CANCELLED:
            out.append(Ask(f"a{n}_names_target",
                           "Does the cancellation name a specific thread or project? Generic references like "
                           "'the thread' or 'it' do not count.", False))
        if a.kind is Kind.CANCELLED and a.slots.get("op") == "ask":
            out.append(Ask(f"a{n}_offers", "Does the line ask them to choose between threads, or offer threads to "
                           "choose from, even without their names?", False,
                           "the line offers the threads again. Say only that you are leaving the question."))
        if a.kind in QUESTIONS:
            out.append(Ask(f"a{n}_means", f"Is the line a question that asks {means}?", True))
        else:
            meaning = (f"Does the line tell them only that {means}, without a reason, outcome or other claim?"
                       if a.kind is Kind.UNANSWERED else f"Does the line tell them that {means}?")
            out.append(Ask(f"a{n}_means", meaning, True))
            out.append(Ask(f"a{n}_asks_unrequested",
                           "Does the line ask the person a question the act did not call for?", False))
        if a.slots.get("recap"):
            out.append(Ask(f"a{n}_recap", f"Does the line recap that {means} as an action already reported, "
                           "without saying another action happened?", True))
        # The meaning question alone passes a status line that leaves the summary out.
        if a.kind in SAYS_SUMMARY and "summary" in a.slots:
            confirming = a.kind is Kind.CONFIRM_RESPOND
            out.append(Ask(f"a{n}_summary",
                           f"Does the line pass on this {'description of the request' if confirming else 'report from the thread'}: "
                           f"\"{a.slots['summary']}\"?", True,
                           f"the line left out what the request would do: \"{a.slots['summary']}\". Say that, in your "
                           "own words, in the question." if confirming else
                           f"the line left out what the thread reported: \"{a.slots['summary']}\". Say that, in your "
                           "own words, after the status."))
    for n, a in enumerate(acts, 1):
        if account := a.slots.get("begun_for"):
            out.append(Ask(f"a{n}_begun_for", f'Does the line say what the thread was started for, as: "{account}"?',
                           True, f"the line left out what the thread was started for: \"{account}\". Say that, in "
                           "your own words, to tell it from the other thread started in the same project."))
    # A request held behind an earlier one of the same breath is asked after that one is answered. Worded like it, the
    # caller cannot tell the two questions apart.
    for n, a in enumerate(acts, 1):
        if a.slots.get("another"):
            out.append(Ask(f"a{n}_another", "Does the line say in words that this is another or a second request, "
                           "apart from the one the person has just answered, rather than only asking the question?", True,
                           "the line asks as if it were the first question. Say that this is another request, apart "
                           "from the one just answered."))
        if about := a.slots.get("about"):
            out.append(Ask(f"a{n}_about_request", f'Does the line say what the request is about, as: "{about}"?', True,
                           f'the line left out what the request is about: "{about}". Say that, in your own words.'))
    # A request is described only from `request`, which the approval gate passed against the stored request. A thread
    # that has none is told only by the kind of thing it waits for.
    # An approval asked with no description of the request is told by its kind alone, as a thread's need is.
    for n, a in enumerate(acts, 1):
        asks_by_kind = (a.kind is Kind.CONFIRM_RESPOND and a.slots.get("decision") == "approve"
                        and not a.slots.get("summary"))
        if (a.kind not in REQUEST_ACTS and not asks_by_kind) or not a.slots.get("need"):
            continue
        if request := a.slots.get("request"):
            out.append(Ask(f"a{n}_request", f'Does the line say what the request would do, as this description does: '
                           f'"{request}"?', True,
                           f"the line left out what the request would do: \"{request}\". Say that, in your own words."))
            out.append(Ask(f"a{n}_reads_out", "Does the line read out a command, a program's name, a file name, a path or "
                           "code, instead of describing it in plain words?", False,
                           "the line reads out a command, file name or path. Describe what the request would do in "
                           "plain words, without naming a program, a file or a path."))
            out.append(Ask(f"a{n}_request_adds", f'Does the line say anything about what the request would do or what '
                           f'it is for that this description does not: "{request}"?', False,
                           f"the line says more about the request than this description does: \"{request}\". Say "
                           "that and nothing more."))
        else:
            out.append(Ask(f"a{n}_kind_only", f"Not counting how the line names the thread ({kind_only_names(a)}), "
                           "does the line say what the request or command would do or what it is for, instead of only "
                           "what kind of request the thread waits on?", False,
                           "the line says what the request does or is for. Say only the kind of thing the thread "
                           "waits for, such as approval to run a command."))
            # The kind is a typed field (`need`), but the writer copied a command example's words for a file change and
            # the meaning question passed it (app line eval, 2 of 4 drafts). On made-up lines (record_kind_only) the
            # right kind drew 0.99 or more and a wrong one 0.08 to 0.75, so a yes needs 0.9; no held-out set.
            kind = NEED.get(a.slots["need"], a.slots["need"])
            out.append(Ask(f"a{n}_need_kind", f"Does the line say the thread waits for {kind}, and not for a different "
                           "kind of request?", True,
                           f"the line names the wrong kind of request. Say it waits for {kind}.", bar=NEED_KIND_YES))
    # The slot check finds an option anywhere in the line, so "Strong" is found in "how strong should it be" and a line
    # that leaves the option out passes it (rehearsal). Each option and answer is asked about on its own.
    for n, a in enumerate(acts, 1):
        options = a.slots.get("options") if a.kind is Kind.ASK_QUESTION else None
        for i, value in enumerate(options or (), 1):
            # A reply by place is read against this order, so the line must tell the options in it.
            out.append(Ask(f"a{n}_options_{i}",
                           f'Does the line offer "{value}" as choice number {i} of {len(options)}, the choices in '
                           f'order being {", ".join(options)}?', True,
                           f"the line left out {value!r} or told the options in another order. Say every option, each as "
                           f"given, in this order: {', '.join(options)}."))
        for key, template in OFFERED.get(a.kind, ()):
            for i, value in enumerate(a.slots.get(key) or (), 1):
                out.append(Ask(f"a{n}_{key}_{i}", template.format(value=value), True,
                               f"the line left out {value!r}. Say every option or answer, each as given."))
    # Without these, "I've stopped X, right?" passed for a stop confirm and "X has stopped." for a stop just asked
    # for: the meaning question misses tense (measured on the fixture, 2026-11-18).
    subject = next((a.slots["thread"] for a in acts if "thread" in a.slots),
                   next((begun_phrase(a.slots) for a in acts if "begun_in" in a.slots or "on_call" in a.slots),
                        "the thread"))
    for claim, wording in asked_claims(acts).items():
        out.append(Ask(f"claims_{claim}", f"Does the line say that {_fill(wording, {'thread': subject})}?", False,
                       bar=CLAIM_BARS.get(claim, YES)))
    # Of three phrasings probed on the six fixture lines, this one kept the widest gap (measured, 2026-11-18).
    out.append(Ask("invents", "Does the line state something about a thread that `facts` does not support?", False,
                   "the line says something the facts do not support. Say only what the act means and add nothing."))
    return out


def kind_only_names(a: Act) -> str:
    """How a line may name the thread, quoted for the kind-only question: told only "the thread's name", d1.2 read a
    title that reads as an instruction, or what an untitled thread was started for, as what the request is for (5 of 5
    file-change lines, and the gate's untitled-thread approvals)."""
    named = f' and the tool "{a.slots["tool"]}"' if a.slots.get("tool") else ""
    if a.slots.get("begun_for"):
        return f'the thread started for "{a.slots["begun_for"]}"{named}'
    if a.slots.get("thread"):
        return f'its name, "{a.slots["thread"]}"{named}'
    return f"its name{named}"


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
                if a.kind is Kind.UNANSWERED and claim == "answered":
                    asked[claim] = "a thread question was answered or a permission request was approved or denied"
                else:
                    asked[claim] = (CLAIMS_ONCE_ENDED if ended else {}).get(claim, CLAIMS[claim])
    return dict(sorted(asked.items()))


def request(acts: Iterable[Act], line: str) -> dict:
    """One decisions body: the line and its acts' facts as state, and a noul question per Ask. An act with no thread
    carries what it means ("says"), because the unsupported-claim question is about a thread and refused "Should I
    start a new coding thread in Voice sandbox?" three times in three on the 2026-11-18 rehearsal."""
    acts = list(acts)
    state = {"line": line, "facts": [{"act": a.kind.value, **a.slots, **({} if "thread" in a.slots else {"says": act_meaning(a)})}
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
        if (value >= ask.bar) != ask.want:
            failed.append(ask.name)
            problems.append(ask.problem())
    return not failed, probabilities, tuple(failed), tuple(problems)


def reuse_request(title: str, gist: str, asked: str | None, question: str) -> dict:
    """One decisions body: does `gist`, written for `asked`, answer `question`? The thread's records are not in it:
    whether the summary is true was checked when it was written."""
    state = {"thread": title, "summary": gist, "summary_was_for": asked, "question": question, "context": REUSE_CONTEXT}
    return {"model": "d1.2", "state": state,
            "questions": {"answers": {"type": "noul", "instructions": REUSE_QUESTION,
                                      "criteria": {"true": "Yes.", "false": "No."}}}}


# A free reply (the voice model's own words, nothing drafted or checked) is judged against what T3 holds about the thread
# the call last spoke of: one typed choice of what the reply says about a thread relative to `facts`.
CLAIM_CONTEXT = ("A voice assistant just said `reply` aloud to the person it is talking to on a call about their coding "
                 "threads in an app called T3. `facts` is everything true that the assistant knows about the thread it "
                 "last spoke of: its state in T3, the end of its last reply, and what the assistant itself has sent to "
                 "it or stopped. Nothing else is known. A thread's state, a request it waits on, and what the assistant "
                 "sent to it or stopped are only what facts show. Saying a thread was approved, answered, sent to, "
                 "started, stopped or finished is supported only when facts show that exactly.")
CLAIM_QUESTION = "Compare `reply` with `facts`. What does the reply say about a coding thread?"
CLAIM_CRITERIA = {"nothing": "The reply says nothing about a coding thread.",
                  "supported": "Everything the reply says about a thread is shown by facts.",
                  "unsupported": "The reply says something about a thread that facts do not show, or that facts "
                                 "contradict."}
# The share a reply needs for `unsupported` to be corrected. A correction cuts a reply that may be right, so this is the
# sure side: on voice/t3/fixtures/claims.json it flags 8 of 10 wrong replies and 0 of 10 others (seen), and the held-out
# set is in docs/voice/live-reply-check.md.
CLAIM_UNSUPPORTED_YES = 0.5
# With no thread in view every claim is unsupported unless the voice did it, and the local d1-2B reads an offer or a
# question about doing something as one (0.5 to 0.77 on 7 of 15 such replies, voice/t3/eval/claim_cases.py), so its bar
# is set above the highest share of a reply that is no claim on the seen set.
CLAIM_NO_THREAD_YES = 0.75
NO_THREAD_NOTE = ("Nothing has been started, sent, stopped or approved. An offer, a question about doing something, or "
                  "something that would happen only after a yes is not a claim.")
CLAIM_TIMEOUT_S = 2.0


def claim_request(reply: str, facts: dict) -> dict:
    """One decisions body: what `reply` says about a thread, and whether `facts` show it."""
    return {"model": "d1.2", "state": {"reply": reply, "facts": facts, "context": CLAIM_CONTEXT},
            "questions": {"reading": {"type": "choice", "instructions": CLAIM_QUESTION, "criteria": CLAIM_CRITERIA}}}


# A free reply on the turn that took a thread-pick question back is held until this answer: whether it offers the
# taken-back threads as choices again. Under the note that tells it not to, the call's voice still said the old question
# after "Never mind." (docs/voice/live-reply-check.md). An unreadable answer counts as an offer: the reply is never heard,
# and a checked line is said in its place.
REOFFER_CONTEXT = ("A voice assistant is about to say `reply` aloud on a call about the person's coding threads. It had "
                   "asked them to choose between the threads in `taken_back`, and they took that question back.")
REOFFER_QUESTION = "Does the reply ask them to choose between those threads, or list them as choices, again?"
REOFFER_YES = 0.5


def reoffer_request(reply: str, threads: list[str]) -> dict:
    """One decisions body: whether `reply` offers `threads` as choices again."""
    return {"model": "d1.2", "state": {"reply": reply, "taken_back": threads, "context": REOFFER_CONTEXT},
            "questions": {"answers": {"type": "noul", "instructions": REOFFER_QUESTION,
                                      "criteria": {"true": "Yes.", "false": "No."}}}}


@dataclass(frozen=True)
class Claim:
    """The decider's reading of a free reply: P(it says something about a thread that `facts` do not show) and P(it says
    nothing about a thread). `wrong` is `unsupported` at CLAIM_UNSUPPORTED_YES or more. An unreadable answer is never
    wrong, and says so in `error`."""

    unsupported: float | None = None
    nothing: float | None = None
    ms: float = 0.0
    error: str | None = None
    bar: float = CLAIM_UNSUPPORTED_YES

    @property
    def wrong(self) -> bool:
        return self.unsupported is not None and self.unsupported >= self.bar


def noul(record: dict, name: str) -> float | None:
    """The P(yes) of question `name` in a decider record, or None for anything else: a failed call, a body that is
    not the decider's, a missing answer, or a value that is not a probability. A boolean is not one, though Python
    counts it as a number."""
    response = record.get("response") if isinstance(record, dict) and record.get("status") == 200 else None
    answers = response.get("answers") if isinstance(response, dict) else None
    answer = answers.get(name) if isinstance(answers, dict) else None
    value = answer.get("noul") if isinstance(answer, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value) if 0 <= value <= 1 else None


@dataclass
class Decider:
    """The line check over a decider client (`call(route, body, label)`), one request in flight."""

    client: Any
    lock: threading.Lock = field(default_factory=threading.Lock)
    calls: int = 0

    def verify(self, acts: Iterable[Act], line: str, *, fresh: int = 0) -> Verdict:
        """The decider's verdict on `line` for `acts`. The hosted decider keeps the first answer it gave for a request
        and serves it again, so a line it refused once is refused every time it is asked about. `fresh` is for a line
        that was refused and came back. 1 reads it once anew. 2 reads it twice anew and passes it only if both pass,
        since overturning a refusal takes more evidence than making it: the two readings run side by side when the
        client has a second connection (`spare`), so they cost one call's time."""
        acts = list(acts)
        if fresh < 2:
            with self.lock:
                return self._read(self.client, acts, line, bool(fresh))
        with self.lock:
            spare = getattr(self.client, "spare", None)
            if spare is None:
                first = self._read(self.client, acts, line, True)
                return first if not first.passed else self._read(self.client, acts, line, True)
            other, second = spare(), []
            worker = threading.Thread(target=lambda: second.append(self._read(other, acts, line, True)))
            worker.start()
            first = self._read(self.client, acts, line, True)
            worker.join()
        return first if not first.passed else second[0]

    def _read(self, client, acts: list[Act], line: str, fresh: bool) -> Verdict:
        started = time.perf_counter()
        try:
            self.calls += 1
            record = client.call("decisions", request(acts, line), label="t3_line",
                                 **({"fresh": True} if fresh else {}))
        except Exception as exc:
            # A spend or billing stop, a timeout, or a dropped connection: the line is not spoken on it.
            return Verdict(False, ms=_ms(started), error=f"{type(exc).__name__}: {exc}")
        if record.get("status") != 200:
            return Verdict(False, ms=_ms(started), error=f"decider status {record.get('status')}")
        passed, answers, failed, problems = judge(acts, (record.get("response") or {}).get("answers"))
        return Verdict(passed, answers, failed, _ms(started), problems=problems)

    def reuses(self, title: str, gist: str, asked: str | None, question: str | None) -> bool:
        """Whether `gist`, written for `asked`, answers `question`: only on a confident yes. A timeout, an error or an
        answer that is not a probability is no, and the summary is written again. With no question it is not asked:
        neither decider could tell a full report from a narrow answer."""
        if not question:
            return False
        try:
            with self.lock:
                self.calls += 1
                record = self.client.call("decisions", reuse_request(title, gist, asked, question), label="t3_reuse")
        except Exception:
            return False
        p_yes = noul(record, "answers")
        return p_yes is not None and p_yes >= REUSE_YES

    def claims(self, reply: str, facts: dict) -> Claim:
        """Whether `reply` claims something about a thread that `facts` do not support. A timeout or any error is
        never a claim: nothing is corrected on a check that did not finish."""
        started = time.perf_counter()
        try:
            with self.lock:
                self.calls += 1
                record = self.client.call("decisions", claim_request(reply, facts), label="t3_claim")
        except Exception as exc:
            return Claim(ms=_ms(started), error=f"{type(exc).__name__}: {exc}")
        response = record.get("response") if record.get("status") == 200 else None
        answers = response.get("answers") if isinstance(response, dict) else None
        scores = _scores((answers or {}).get("reading"), CLAIM_CRITERIA) if isinstance(answers, dict) else None
        if scores is None:
            return Claim(ms=_ms(started), error=f"decider status {record.get('status')}, no reading")
        return Claim(scores.get("unsupported", 0.0), scores.get("nothing", 0.0), _ms(started),
                     bar=CLAIM_NO_THREAD_YES if facts.get("thread") is None else CLAIM_UNSUPPORTED_YES)

    def reoffers(self, reply: str, threads: list[str]) -> tuple[float | None, float]:
        """P(`reply` offers the taken-back `threads` as choices again), or None when the check did not finish, and the
        time it took in ms."""
        started = time.perf_counter()
        try:
            with self.lock:
                self.calls += 1
                record = self.client.call("decisions", reoffer_request(reply, threads), label="t3_reoffer")
        except Exception:
            return None, _ms(started)
        return noul(record, "answers"), _ms(started)

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

    def chooses(self, question: str, options: tuple[tuple[str, str], ...], reply: str, own_words: bool = False) -> Chosen:
        """Which of `options` (label and description) `reply` chooses for `question`, or that it gives the caller's own
        words or no answer. A timeout or any error chooses nothing, and nothing is ever sent on it."""
        try:
            with self.lock:
                self.calls += 1
                record = self.client.call("decisions", choice_request(question, options, reply, own_words),
                                          label="t3_answer")
        except Exception as exc:
            return Chosen(None, error=f"{type(exc).__name__}: {exc}")
        if record.get("status") != 200:
            return Chosen(None, error=f"decider status {record.get('status')}")
        return judge_choice((record.get("response") or {}).get("answers"), len(options), own_words)

    def picks(self, request: str, threads: tuple[Listed, ...], recent: str | None = None,
              answering: bool = False, call_thread: str | None = None, spoken: str | None = None) -> Picked:
        """Which of `threads` the words `request` mean, with how sure it is. `recent` is the title the call was just
        about, `spoken` the id of the thread the voice last said something about, and `call_thread` the id of the
        thread the call was opened from. A timeout or any error picks nothing, and nothing is always asked about."""
        if not threads:
            return Picked(NONE, shares={NONE: 1.0})
        try:
            with self.lock:
                self.calls += 1
                record = self.client.call("decisions", pick_request(request, threads, recent, answering, call_thread,
                                                                    spoken), label="t3_pick")
        except Exception as exc:
            return Picked(None, error=f"{type(exc).__name__}: {exc}")
        if record.get("status") != 200:
            return Picked(None, error=f"decider status {record.get('status')}")
        return judge_pick((record.get("response") or {}).get("answers"), threads,
                          sure=getattr(self.client, "pick_sure", PICK_SURE))

    def names(self, request: str, thread: Listed) -> float | None:
        """P(the words `request` say `thread`'s title), or None on a timeout or any error. A pick between its confirm
        and sure bars is held under the bar by a near twin's share or by the rest of a long request; this asks only
        whether the title was said."""
        try:
            with self.lock:
                self.calls += 1
                record = self.client.call("decisions", names_request(request, thread), label="t3_names")
        except Exception:
            return None
        return noul(record, "names")


    def about_line(self, words: str, said: dict) -> float | None:
        """P(the caller's `words` doubt, question or ask about the line the voice said with the facts `said`), or None
        on a timeout or any error."""
        try:
            with self.lock:
                self.calls += 1
                record = self.client.call("decisions", about_line_request(words, said), label="t3_about_line")
        except Exception:
            return None
        return noul(record, "about")


NONE, UNCLEAR = "none", "unclear"
# A thread with at least this share of the choice is one the words may mean: it is confirmed, or offered in "which".
# On 27 probe requests over 12 and over 40 threads (d1.2), a request that fits several threads gave each 0.07 to 0.32
# and "unclear" the most; a request naming one thread, even misheard, gave it 0.72 to 0.99.
PICK_FLOOR = 0.1
PICK_CONFIRM = 0.5
# A thread with this much is one the decider is sure of, and a question about it is answered without first asking
# "do you mean this thread". A thread named plainly got 0.95 to 0.99; a misheard name, "it" for the thread just
# spoken about, and "the one waiting on me" got 0.72 to 0.88, and are confirmed.
PICK_SURE = 0.9


@dataclass(frozen=True)
class Listed:
    """One thread the decider may pick, as the listing describes it."""

    thread_id: str
    title: str
    project: str = ""
    activity: str = ""


@dataclass(frozen=True)
class Picked:
    """The decider's pick of a thread. `choice` is a thread id, NONE for a thread that is not listed, UNCLEAR when the
    words do not settle on one, and None when there was no answer. `shares` is each thread id's share of the choice."""

    choice: str | None
    shares: dict[str, float] = field(default_factory=dict)
    error: str | None = None
    sure_threshold: float = PICK_SURE

    @property
    def thread(self) -> str | None:
        """The one thread the words mean, when it has most of the weight."""
        picked = self.choice not in (None, NONE, UNCLEAR) and self.shares.get(self.choice, 0.0) >= PICK_CONFIRM
        return self.choice if picked else None

    @property
    def sure(self) -> bool:
        return self.thread is not None and self.shares[self.thread] >= self.sure_threshold

    @property
    def candidates(self) -> tuple[str, ...]:
        """The threads the words may mean, likeliest first."""
        fits = [(share, thread) for thread, share in self.shares.items()
                if thread not in (NONE, UNCLEAR) and share >= PICK_FLOOR]
        return tuple(thread for _, thread in sorted(fits, key=lambda fit: -fit[0]))


ANSWERING_CONTEXT = ("A person on a voice call is answering a question one of their coding threads asked. `request` is a "
                     "transcript of what they said, and may mishear a word.")
ANSWERING_PICK = ("Which of the person's threads does `request` name or describe, by its title, project or subject? "
                  "Most of `request` is the answer, not a thread. Words that only pick an option, or a place such as "
                  "\"the second one\", name no thread: choose unclear. The options list the threads, newest first.")


# A call opened from a thread is about it, so its option says so. Named in the state by title instead, a near twin
# that waits on the person ("Fix pause menu in the sandbox project" beside "Fix the Pause Menu") still drew "Is it
# waiting on anything from me?", and with both, a request naming the twin chose the call's thread (d1.2,
# docs/voice/browser-client.md).
CALL_MARK = " The person opened this call from this thread."


# The thread the voice just told the person about is the one "it" means next, so its option says so, by id. Shown only as
# `last_discussed`, by title, news that a thread needed permission was followed by a short reply about "it" that chose
# another thread whose title sounded like one misheard word (docs/voice/spoken-acts.md).
SPOKEN_MARK = " The assistant just told the person about this thread."


def pick_request(request: str, threads: tuple[Listed, ...], recent: str | None = None,
                 answering: bool = False, call_thread: str | None = None, spoken: str | None = None) -> dict:
    """One decisions body asking which listed thread the person's words mean. The threads are the options, so the
    answer's shares say how sure the pick is and which others the words fit."""
    criteria = {f"thread_{i:02d}": f"{t.title!r} in the project {t.project!r}, {t.activity.replace('_', ' ')}."
                                   + (CALL_MARK if t.thread_id == call_thread and not answering else "")
                                   + (SPOKEN_MARK if t.thread_id == spoken and not answering else "")
                for i, t in enumerate(threads, 1)}
    # "Particular" keeps a vague request with nothing discussed out of this option on the local d1-2B.
    criteria[NONE] = "It names or describes a particular thread, and none of these is that thread."
    criteria[UNCLEAR] = "It does not say which thread, or fits several of these equally well."
    state = {"request": request, "last_discussed": recent,
             "context": "A person on a voice call asked their assistant about one of their coding threads. `request` "
                        "is a transcript of what they said, and may mishear a word."}
    instructions = ("Which of the person's threads does `request` mean? The options list them, newest first. "
                    "`last_discussed` is the thread the conversation was just about, when there is one.")
    if answering:
        state = {"request": request, "last_discussed": None, "context": ANSWERING_CONTEXT}
        instructions = ANSWERING_PICK
    return {"model": "d1.2", "state": state,
            "questions": {"pick": {"type": "choice", "instructions": instructions, "criteria": criteria}}}


NAMES_CONTEXT = ("A person on a voice call asked their assistant about one of their coding threads. `request` is a "
                 "transcript of what they said, and may mishear a word. `title` is one thread's title.")
NAMES_QUESTION = ("Do the person's words say this thread's title, whole or nearly whole, as its name? Leaving out a "
                  "word such as \"add\" or \"the\", or one misheard word, still counts. Words such as \"it\" or "
                  "\"that one\", or a description in other words, do not.")
# A pick under PICK_SURE that the words name by title at this or more is as sure as one over it.
NAMES_SURE = 0.8


def names_request(request: str, thread: Listed) -> dict:
    return {"model": "d1.2", "state": {"request": request, "title": thread.title, "context": NAMES_CONTEXT},
            "questions": {"names": {"type": "noul", "instructions": NAMES_QUESTION,
                                    "criteria": {"true": "Yes.", "false": "No."}}}}


# A free turn after a line about a thread is told where that line came from only when its words are about it. Told on
# every such turn, the voice said the line again to a doubt, a remark and a goodbye (2026-11-22 call, 3 of 3 turns).
ABOUT_LINE_CONTEXT = ("A person on a voice call with their assistant heard it say something about one of their coding "
                      "threads; `said` holds the facts it said. `words` is a transcript of what the person said next.")
ABOUT_LINE = ("Do `words` doubt or question what the assistant said about the thread, or ask about it or why it was "
              "said? Thanks, an acknowledgement, a goodbye, or talk about something else do not count.")
ABOUT_LINE_YES = 0.5


def about_line_request(words: str, said: dict) -> dict:
    return {"model": "d1.2", "state": {"said": said, "words": words, "context": ABOUT_LINE_CONTEXT},
            "questions": {"about": {"type": "noul", "instructions": ABOUT_LINE,
                                    "criteria": {"true": "Yes.", "false": "No."}}}}


def judge_pick(answers: Any, threads: tuple[Listed, ...], sure: float = PICK_SURE) -> Picked:
    """The decider's answer as a Picked. A label that is not an option, or shares that are not numbers from 0 to 1,
    is no pick at all."""
    ids = {f"thread_{i:02d}": t.thread_id for i, t in enumerate(threads, 1)}
    answer = answers.get("pick") if isinstance(answers, dict) else None
    scores = _scores(answer, {*ids, NONE, UNCLEAR})
    if scores is None:
        return Picked(None, error="the decider gave no pick")
    return Picked(ids.get(answer["choice"], answer["choice"]), {ids.get(k, k): v for k, v in scores.items()},
                  sure_threshold=sure)


READ_CRITERIA = {
    "yes": ("It agrees to exactly what the assistant asked, with nothing added, changed or conditional. A short "
            "agreement on its own, such as okay, alright, sure, mm-hmm or please do, counts. Words that thank, say "
            "goodbye or note what was said, such as got it or cool, are not agreement, even after okay."),
    "no": "It declines what the assistant asked, or takes it back.",
    "question": "It asks the assistant something, or asks about what was proposed, instead of answering.",
    "other": "It agrees but changes the project, thread or terms, adds a condition, or names something else.",
    "unrelated": "It does not answer what the assistant asked.",
}
# A yes is read from two questions of its own beside the choice, because the choice splits a bare "Yeah?" between yes
# and question (0.48 and 0.33 on the hosted d1.2) and so never acts on it: does the reply say yes, and does it, in
# words, ask for anything. A real question scores 0.89 or more on the second, a bare word of agreement with a rising
# voice 0.53 or less. The choice still catches a no, a condition and another target. Without the last sentence of the
# first question a hesitant "hmm, maybe?" was a yes at 0.66.
AGREES = ("Do the person's words say yes to what the assistant asked? A bare yeah, sure, okay or alright on its own "
          "counts, however it is said. Words that thank, say goodbye or note what was said, such as got it or cool, are "
          "not yes, even after okay. A condition, a change, or another target does not. A reply that is unsure, undecided or "
          "non-committal, such as maybe, perhaps or I don't know, is not yes. The words must answer this very "
          "question: repeating the question back, agreeing with something else, or a word cut off before it is a whole "
          "word is not yes.")
# "Okay, thanks." was a yes to every kind of confirmation (5 of 5 samples each), since the text above said okay agrees
# and nothing said a thanks does not. Told so, the choice and `agrees` refuse it (voice/t3/eval/record_consent.py).
# This question tells a thanks from other replies that answer nothing, so a confirmation is asked once more instead of
# dropped. It is no part of a yes: as a veto it refused real yeses, and the local d1-2B answers it backwards (0.89 for
# "Yes.").
CLOSES = ("Instead of answering what the assistant asked, do the person's words thank the assistant, say goodbye, or "
          "only note what it said? A bare okay, alright or sure on its own answers the question.")
ADDS = ("Beyond agreeing, do the person's words add a remark, a fact or an opinion about something other than what the "
        "assistant asked? Words that only agree, however hesitantly or politely, add nothing.")
ASKS = ("Do the person's words ask the assistant for information, an explanation or a different choice before "
        "deciding? A bare word of agreement is not asking, even said with a rising voice.")
WITHDRAWS = ("Does the person take back what they asked about altogether, so that they no longer want any thread "
             "looked up? Saying this is not the thread they mean, or naming another one, does not.")
AGREES_CONFIDENCE = 0.5
ADDS_CEILING = 0.5
# On the hosted seen replies every real yes scored 0.49 or less and every thanks or goodbye 0.58 or more.
CLOSES_CEILING = 0.55
# A reply that may be a yes needs this share of yes in the choice: the recorded rising yeses the question floor refused
# gave it 0.23 and 0.27, and a question back with no yes in it is no maybe.
UNSURE_YES_FLOOR = 0.15
ASKS_CEILING = 0.7
YES_OR_QUESTION = 0.5
# A choice that says question counts for a yes the transcript marked as one only when the choice still gives yes a real
# share. The recorded question-labelled replies that are taken give yes 0.32 to 0.42 (0.34 and up on the seen rows).
QUESTION_LABEL_YES_FLOOR = 0.3
WITHDRAWS_CONFIDENCE = 0.5
# A reply with any real share of a no or of another target is not an unconditional yes, however much of it says yes.
OTHER_CEILING = 0.2
OPTION_CONFIDENCE = 0.6
# The recorded replies that ask for something else give it 0.82 and up. One that changes the request, not the target,
# gave it 0.41 in one recording and 0.57 in the next.
ELSEWHERE_CONFIDENCE = 0.7
KEEPS, ELSEWHERE = "keeps", "elsewhere"
YES_NO = {"true": "Yes.", "false": "No."}


def reading_request(said: Act, reply: str, options: tuple[str, ...] = ()) -> dict:
    """One decisions body asking what the person's reply does about the question `said` asked them. `reply` is the
    transcript, whose punctuation need not match how they said it: a rising "please do" arrives with a question mark.
    `options` are the other things the reply could be asking for, by name, and add a question of whether it keeps
    what was proposed, changes to one of them, or changes without saying which."""
    state = {"assistant_asked": _fill(MEANS[said.kind], said.slots), "reply": reply,
             "context": "A voice assistant asked the person a yes or no question about their coding threads. `reply` "
                        "is a speech recognition transcript of what they said back: its punctuation is guessed, so a "
                        "question mark may only mean their voice rose."}
    questions = {"reading": {"type": "choice", "instructions": "What does the person's reply do about what the "
                                                               "assistant asked?", "criteria": READ_CRITERIA}}
    if said.kind is not Kind.ASK_WHICH:
        questions["agrees"] = {"type": "noul", "instructions": AGREES, "criteria": YES_NO}
        questions["asks"] = {"type": "noul", "instructions": ASKS, "criteria": YES_NO}
        questions["adds"] = {"type": "noul", "instructions": ADDS, "criteria": YES_NO}
        questions["closes"] = {"type": "noul", "instructions": CLOSES, "criteria": YES_NO}
    if said.kind is Kind.CONFIRM_THREAD or (said.kind is Kind.ASK_WHICH and said.slots.get("of") == "thread"):
        questions["withdraws"] = {"type": "noul", "instructions": WITHDRAWS, "criteria": YES_NO}
    if options and said.kind is Kind.ASK_WHICH:
        # The assistant listed the options aloud, so "the second one" answers by place as well as by name.
        state["listed"] = list(options)
        criteria = {KEEPS: "It does not choose one of them."}
        criteria.update({f"option_{i}": f"It chooses {name!r}, number {i} of the {len(options)} listed."
                         for i, name in enumerate(options, 1)})
        criteria[ELSEWHERE] = "It asks for something that is not one of those listed."
        questions["target"] = {"type": "choice", "criteria": criteria,
                               "instructions": "The assistant read out `listed`, in that order, and asked which one "
                                               "the person means. Which does the reply choose?"}
    elif options:
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
    `elsewhere` says it changes to another option without making clear which. `agrees`, `asks` and `withdraws` are the
    shares the decider gave the questions of that name, None when one was not asked or not answered."""

    label: str | None
    probabilities: dict[str, float] = field(default_factory=dict)
    error: str | None = None
    target: str | None = None
    elsewhere: bool = False
    agrees: float | None = None
    asks: float | None = None
    withdraws: float | None = None
    adds: float | None = None
    closes: float | None = None
    complete: bool = False
    kept: bool = False

    @property
    def yes(self) -> bool:
        return reads_yes(self.label, self.probabilities if self.complete else None, self.agrees, self.asks, self.adds,
                         self.kept)

    @property
    def withdrawing(self) -> bool:
        """A reply that takes the whole request back, as against one that says this is not the one and asks for
        another. Its choice is a no, or unrelated for a "never mind" that declines nothing in particular."""
        return (self.label in ("no", "unrelated") and self.withdraws is not None
                and self.withdraws >= WITHDRAWS_CONFIDENCE)

    @property
    def unsure(self) -> bool:
        """A reply that may be a yes and is not read as one: the choice says yes or question and gives yes a real share,
        the words say yes, and nothing declines, changes, or asks in words. A rising "Okay?" reads so."""
        if self.yes or self.label not in ("yes", "question") or self.agrees is None or self.asks is None:
            return False
        return (self.probabilities.get("yes", 0.0) >= UNSURE_YES_FLOOR
                and self.agrees >= AGREES_CONFIDENCE and self.asks < ASKS_CEILING
                and self.probabilities.get("no", 1.0) <= OTHER_CEILING
                and self.probabilities.get("other", 1.0) <= OTHER_CEILING)

    @property
    def acknowledges(self) -> bool:
        """A reply that thanks, says goodbye or notes what was said, and so neither answers the question nor declines,
        asks, or changes it."""
        return (self.closes is not None and self.closes >= CLOSES_CEILING
                and self.label not in (None, "no", "question", "other") and not self.yes)

    @property
    def kind(self) -> str | None:
        """"yes", "no", or None for a reply that neither agrees outright nor declines."""
        return "yes" if self.yes else "no" if self.label == "no" else None


def reads_yes(label: str | None, shares: dict[str, float] | None, agrees: float | None, asks: float | None,
              adds: float | None, kept: bool) -> bool:
    """Whether the decider's answers together are an unconditional yes, the one function that can authorise an action.
    Every answer it rests on must be there: `shares` is the choice's complete map (None when it is missing, partial or
    not shares of one whole), `agrees`, `asks` and `adds` are probabilities, and `kept` says the target answer was present and
    says the proposal is kept (or none was asked). Then all of these must hold: the choice's own label is yes, or
    question, which counts only when yes still has at least QUESTION_LABEL_YES_FLOOR of the choice; the shares of yes
    and question together are at least half; a no and another target each have at most OTHER_CEILING; the words say yes (`agrees`), do not ask
    for anything in words (`asks`) and add nothing but the agreement (`adds`). Anything missing, malformed or in
    disagreement is not a yes."""
    if (shares is None or agrees is None or asks is None or adds is None or not kept
            or label not in ("yes", "question")):
        return False
    if label == "question" and shares.get("yes", 0.0) < QUESTION_LABEL_YES_FLOOR:
        return False
    return (shares.get("yes", 0.0) + shares.get("question", 0.0) >= YES_OR_QUESTION
            and shares.get("no", 1.0) <= OTHER_CEILING and shares.get("other", 1.0) <= OTHER_CEILING
            and agrees >= AGREES_CONFIDENCE and asks < ASKS_CEILING and adds < ADDS_CEILING)


def _complete(shares: dict[str, float]) -> bool:
    """The choice's shares name every label and are shares of one whole, within the decider's rounding."""
    return set(shares) == set(READ_CRITERIA) and 0.95 <= sum(shares.values()) <= 1.05


def _noul(answers: dict, name: str) -> float | None:
    """The P(yes) of the question `name` in a decider's answers, None for a missing answer or a value that is not a
    probability."""
    answer = answers.get(name)
    value = answer.get("noul") if isinstance(answer, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
        return None
    return float(value)


def _scores(answer: Any, labels) -> dict[str, float] | None:
    """A choice answer's shares when its label is one of `labels`, every share is a number from 0 to 1, and together
    they are shares of one whole."""
    probabilities = answer.get("probabilities") if isinstance(answer, dict) else None
    choice = answer.get("choice") if isinstance(answer, dict) else None
    if (not isinstance(probabilities, dict) or not isinstance(choice, str) or choice not in labels
            or not set(probabilities) <= set(labels)):
        return None
    for value in probabilities.values():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0.0 <= value <= 1.0:
            return None
    if sum(probabilities.values()) > 1.05:
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
    # With alternatives on offer the proposal is kept only when the target answer is there and says so.
    kept = not options or (shares is not None and picked["choice"] == KEEPS and share >= OPTION_CONFIDENCE)
    return Reading(answers["reading"]["choice"], scores, target=target, elsewhere=elsewhere,
                   complete=_complete(scores), kept=kept,
                   **{name: _noul(answers, name) for name in ("agrees", "asks", "withdraws", "adds", "closes")})


OWN_WORDS, NO_ANSWER = "own_words", "no_answer"
# An option is the answer only with at least this share of the choice. An answer steers a coding agent, so a lean is not
# enough: below it the question is asked again.
ANSWER_SURE = 0.7
# The caller's own words are the answer with this share, lower because the choice has fewer rivals to separate from: a
# question with no options gave a plain answer ("Call it level cache.") 0.61 to 0.67 and no reply that answers nothing
# more than 0.06 (voice/t3/fixtures/answer_choices.json). The confirmation reads the words back before anything is sent.
WORDS_SURE = 0.5


@dataclass(frozen=True)
class Chosen:
    """The decider's choice among the options a question was told with. `choice` is `option_NN` (NN from 1), OWN_WORDS
    (the reply answers in words no option says), NO_ANSWER (it answers nothing), UNCLEAR (it may answer but does not
    say which option), or None when there was no answer. `shares` is each label's share of the choice."""

    choice: str | None
    shares: dict[str, float] = field(default_factory=dict)
    error: str | None = None
    sure_threshold: float = ANSWER_SURE

    @property
    def share(self) -> float:
        return self.shares.get(self.choice, 0.0) if self.choice is not None else 0.0

    @property
    def option(self) -> int | None:
        """The 0-based index of the option the reply chooses, only when the decider is sure of it."""
        if self.choice is None or not self.choice.startswith("option_") or self.share < self.sure_threshold:
            return None
        return int(self.choice[len("option_"):]) - 1

    @property
    def own_words(self) -> bool:
        return self.choice == OWN_WORDS and self.share >= WORDS_SURE

    @property
    def declined(self) -> bool:
        """The reply answers nothing: another request, a refusal, or talk about something else."""
        return self.choice == NO_ANSWER and self.share >= PICK_CONFIRM


def choice_request(question: str, options: tuple[tuple[str, str], ...], reply: str, own_words: bool) -> dict:
    """One decisions body asking which option a reply chooses for a question a thread asked. The options are listed in
    the order the voice read them out, so "the second one" answers by place as well as by name."""
    criteria = {f"option_{i:02d}": f"It picks {label!r}" + (f" ({note})" if note and note != label else "") +
                f", number {i} of the {len(options)} read out."
                for i, (label, note) in enumerate(options, 1)}
    if own_words:
        criteria[OWN_WORDS] = "It answers the question in its own words, with something none of the options says."
    criteria[NO_ANSWER] = ("It does not answer the question: it asks for something else, declines to answer, takes "
                           "the question back, or talks about something else.")
    criteria[UNCLEAR] = "It may be an answer, but does not say which option, or fits several of them equally well."
    state = {"question": question, "options": [label for label, _ in options], "reply": reply,
             "context": "A voice assistant on a call told the person a question that one of their coding threads is "
                        "waiting on, and read out its options in order. `reply` is a speech recognition transcript of "
                        "what they said back: it may mishear a word, and a rising voice may add a question mark."}
    instructions = "Which option does `reply` choose for `question`? `options` are in the order they were read out."
    return {"model": "d1.2", "state": state,
            "questions": {"choice": {"type": "choice", "instructions": instructions, "criteria": criteria}}}


def judge_choice(answers: Any, count: int, own_words: bool) -> Chosen:
    """The decider's answer as a Chosen. A label that is not offered, or shares that are not numbers from 0 to 1, is no
    choice at all."""
    labels = {f"option_{i:02d}" for i in range(1, count + 1)} | {NO_ANSWER, UNCLEAR} | ({OWN_WORDS} if own_words else set())
    scores = _scores(answers.get("choice") if isinstance(answers, dict) else None, labels)
    if scores is None:
        return Chosen(None, error="the decider gave no choice")
    return Chosen(answers["choice"]["choice"], scores)


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
