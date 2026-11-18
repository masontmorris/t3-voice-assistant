"""The T3 task service: Twin A launches through the project picker, and Twin B asks about, tells, or stops a thread.

T3Tasks lives as long as the voice server. It owns the seam, the journal, and Follow, which runs the
watch lifecycle (voice/t3/lifecycle.py). T3TaskService is the per-turn task service the prototype
already uses, with records whose decider tool answer names a T3 tool sent to T3Tasks. Every
spoken line comes from a seam result, a stored event, or a read, and carries that sequence as its
evidence.

Nothing acts on a pick alone. A lone project match is asked back as "Start it in Y?", and a lone
thread as "Do you mean the X thread?", "Tell the X thread to ...?" or "Stop the X thread?". Only an
explicit yes in the next turn launches, reads, sends, or stops. A follow-up to a thread that is still
working is held in the journal and sent when that thread's run is done.

    python -m voice.live.t3_tasks drive "Add a clamp helper in the sandbox project" \\
        --ask "How is the clamp helper thread going?"

drives one launch, its watch to done, and one Twin B ask against the fork environment
(voice/t3/fork_env.py), printing each line with its evidence. `smoke` drives the whole dialogue:
a launch, a held follow-up, an ask, and a stop said twice.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import threading
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

from voice.prototype.tasks import Execution, MockTaskService, Run
from voice.t3 import lifecycle as L
from voice.t3 import types as T
from voice.t3.fork_env import FORK_PORT, TOKEN_FILE, default_voice_home
from voice.t3.journal import Kind, Status, T3Journal
from voice.t3.pick.decide import pick as pick_thread
from voice.t3.pick.decide import prefilter_pick
from voice.t3.pick.model import LiveClient
from voice.t3.pick.pick import question_for
from voice.t3.pick.prefilter import content_words
from voice.t3.pick.project import pick_project, question_for_projects
from voice.t3.seam import SeamConnectionEnded, SeamRefused, SeamUnavailable, T3Seam

START, ASK, TELL, STOP = "t3_start", "t3_ask", "t3_tell", "t3_stop"
T3_TOOLS = {
    START: "Start a new T3 coding thread that does the work the speaker asks for, in a project they name. "
           "Use it when they ask to start, open, or kick off a thread or a coding task.",
    ASK: "Report how an existing T3 coding thread is going. Use it when the speaker asks about a thread's "
         "progress, state, or result.",
    TELL: "Pass a message or more work to an existing T3 coding thread. Use it when the speaker asks to tell, "
          "remind, or ask a thread to do something, or adds to the work a thread already has.",
    STOP: "Stop the work an existing T3 coding thread is doing now. Use it when the speaker asks to stop, halt, "
          "or interrupt a thread.",
}
STATUS_WORDS = {Status.UNSTARTED: "running", Status.LIVE: "running", Status.HELD: "held",
                Status.COMPLETED: "completed", Status.FAILED: "failed", Status.CANCELLED: "cancelled",
                Status.LOST: "lost"}

_CONFIRMATION_TEXT = re.compile(r"[a-zA-Z.,!\s]+")
_WORD = re.compile(r"[a-z']+")
_QUESTION = re.compile(r"\?|\b(?:what|when|where|which|who|whom|whose|why|how)\b", re.IGNORECASE)
_OTHER_PROJECT = re.compile(r"\b(?:in|for) (?:the )?other project(?:,? please)?[.! ]*$", re.IGNORECASE)
YES = frozenset({"yes", "yeah", "yep", "yup", "sure", "ok", "okay", "affirmative", "correct", "right", "exactly"})
YES_PHRASES = {("do", "it"), ("go", "ahead"), ("that", "one"), ("that's", "it"), ("that's", "right"),
               ("that's", "the", "one")}
# The words that carry out each op. "stop" refuses a launch or a follow-up, but it is the yes to "Stop the X thread?".
OP_PHRASES = {START: {("start", "it")}, ASK: set(), TELL: {("send", "it"), ("tell", "it"), ("pass", "it", "on")},
              STOP: {("stop",), ("stop", "it"), ("stop", "that")}}
NO = frozenset({"no", "nope", "nah", "don't", "dont", "cancel", "stop", "wait", "never", "not"})
START_CANCELLED = "Okay, I won't start it."
TELL_ACK = "I've asked T3 to pass that on."
STOP_DROPPED = " I've dropped the message I was holding for it."

_LEAD = r"^\W*(?:(?:ok(?:ay)?|so|and|also|please|hey|actually|now)[,\s]+)*(?:(?:can|could|would) you\s+)?(?:please\s+)?"
_TELL = re.compile(_LEAD + r"(?:tell|remind|ask|let)\s+(?P<ref>.+?)\s+(?:know\s+)?(?P<link>to|that)\s+"
                   r"(?P<message>.+?)[.!?\s]*$", re.IGNORECASE)
_STOP_VERBS = re.compile(r"\b(?:stop|halt|interrupt|abort|kill|cancel)\b", re.IGNORECASE)
# Mason takes back a held follow-up: by naming it at any time, or with a bare "never mind" right after the hold.
_DROP_NAMED = re.compile(r"\b(?:don'?t|do not|no need to)\s+(?:pass|send|tell|give)\b|\b(?:cancel|drop|forget|"
                         r"scrap|scratch|never ?mind)\s+(?:about\s+)?(?:the|that|my)\s+(?:message|follow[- ]?up|"
                         r"note|request)\b", re.IGNORECASE)
_DROP_BARE = re.compile(r"^\W*(?:no[,\s]+)?(?:never ?mind|cancel (?:that|it)|forget (?:it|that)|scratch that|"
                        r"don'?t bother)\b", re.IGNORECASE)


def _plain(text: str) -> str:
    return (text or "").replace("’", "'")


def reply_kind(text: str, op: str = START) -> str | None:
    """"yes" only for an explicit confirmation of `op`, "no" for any refusal word, else None."""
    text = _plain(text)
    words = tuple(_WORD.findall(text.lower()))
    if (NO - {"stop"} if op == STOP else NO).intersection(words):
        return "no"
    if not words or not _CONFIRMATION_TEXT.fullmatch(text) or _QUESTION.search(text):
        return None
    if words == ("please",):
        return "yes"
    affirmative = words[0] in YES
    rest = words[1:] if affirmative else words
    if rest[-1:] == ("please",):
        rest = rest[:-1]
    if (affirmative and not rest) or rest in YES_PHRASES or rest in OP_PHRASES[op]:
        return "yes"
    return None


def tell_parts(instruction: str) -> tuple[str, str, str | None]:
    """(the words that name the thread, the message to send, "to" or "that" or None when the words have no split)."""
    match = _TELL.match(_plain(instruction).strip())
    if match is None:
        return instruction, instruction.strip(), None
    return match["ref"], match["message"], match["link"].lower()


def speak_title(title: str) -> str:
    return L.speakable(title, sentences=1, limit=80).rstrip(".!?:")


def confirm_line(op: str, title: str, message: str | None = None, link: str | None = None) -> str:
    """The confirming question for one picked thread. Only a yes to it acts."""
    title = speak_title(title)
    if op == TELL:
        return f"Tell the {title} thread {link} {message}?" if link else f"Pass this to the {title} thread: {message}?"
    if op == STOP:
        return f"Stop the {title} thread?"
    return f"Do you mean the {title} thread?"


CANCELLED_LINES = {ASK: "Okay, never mind.", TELL: L.DROPPED_LINE, STOP: "Okay, I won't stop it."}


def speak_state(state: T.ThreadState) -> str:
    """One short line from a read, in the thread's own words where it has them."""
    title = speak_title(state.title)
    activity = state.activity
    said = L.speakable(state.last_reply.text) if state.last_reply is not None else ""
    if activity is T.ThreadActivity.WAITING_ON_YOU and state.pending is not None:
        return f"{title} is waiting for you: {state.pending.prompt}"
    line = {
        T.ThreadActivity.WORKING: f"{title} is still working.",
        T.ThreadActivity.WAITING_ON_YOU: f"{title} is waiting for you in the app.",
        T.ThreadActivity.WAITING_ON_BACKGROUND: f"{title} is waiting on background work.",
        T.ThreadActivity.QUEUED: f"{title} is queued.",
        T.ThreadActivity.IDLE: f"{title} hasn't started yet.",
        T.ThreadActivity.FINISHED: f"{title} finished.",
        T.ThreadActivity.FAILED: f"{title} failed.",
        T.ThreadActivity.INTERRUPTED: f"{title} was stopped.",
        T.ThreadActivity.CANCELLED: f"{title} was cancelled.",
        T.ThreadActivity.ROLLED_BACK: f"{title} had its work rolled back.",
    }[activity]
    if said and activity in (T.ThreadActivity.WORKING, T.ThreadActivity.FINISHED, T.ThreadActivity.FAILED):
        line += f" Its last reply said: {said}"
    return line


@dataclass
class Question:
    """The open question the next utterance may answer.

    `kind` is the state: "project" (which project), "confirm" ("Start it in Y?"), "thread" (which thread), or
    "thread_confirm" (one thread, asked back for `op`). `candidates` are project or thread ids, and `titles` names
    each thread. `message` and `link` are a follow-up's words, from `tell_parts`.
    """

    key: str
    kind: str
    instruction: str
    candidates: tuple[str, ...]
    reasked: bool = False
    op: str = START
    titles: dict[str, str] = field(default_factory=dict)
    projects: dict[str, str] = field(default_factory=dict)
    message: str | None = None
    link: str | None = None
    evidence: tuple[int, ...] = ()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="minutes")


@dataclass
class T3Tasks:
    """`model` is the Twin B model arm, asked only when the prefilter misses. None keeps the prefilter alone."""

    follow: L.Follow
    model: LiveClient | None = None
    question: Question | None = None
    keys: dict[str, str] = field(default_factory=dict)
    recent: str | None = None
    just_held: bool = False
    lock: threading.RLock = field(default_factory=threading.RLock)

    @classmethod
    def open(cls, origin: str, token_file: str | Path, journal_path: str | Path,
             model: LiveClient | None = None) -> T3Tasks:
        follow = L.Follow(T3Journal(journal_path), lambda: T3Seam.connect(origin, token_file))
        return cls(follow, model)

    def resume(self, now: float) -> None:
        with self.lock:
            self.follow.resume(now)

    def wait(self, wait_s: float):
        """Block up to `wait_s` for the next seam item, outside the lock. Only the polling thread calls it."""
        seam = self.follow.seam
        if seam is None:
            return None
        return seam, next(seam.drain(timeout=wait_s), None)

    def poll(self, now: float, waited=None) -> None:
        with self.lock:
            if waited is not None:
                seam, item = waited
                if item is not None and self.follow.seam is seam:
                    self.follow.apply(item, now)
            self.follow.tick(now)

    def take(self) -> list[L.Update]:
        """Updates from the lifecycle since the last take, under the voice task key that started each thread."""
        with self.lock:
            out = self.follow.take()
        for update in out:
            update.task_id = self.keys.get(update.task_id, update.task_id)
        return out

    def holding(self) -> bool:
        return bool(self.follow.journal.held())

    def _down(self, key: str, now: float) -> L.Update | None:
        seam = self.follow.ensure(now)
        if self.follow.dead is not None:
            return L.Update(key, L.ROWS[self.follow.dead].line, "failed", "ask")
        if seam is None:
            return L.Update(key, L.UNREACHABLE_LINE, "failed", "ask")
        return None

    def projects(self) -> list[dict]:
        return [{"projectId": p.project_id, "name": p.name, "spokenNames": list(p.spoken_names)}
                for p in self.follow.seam.hello.projects]

    def project_names(self) -> dict[str, str]:
        return {p.project_id: p.name for p in self.follow.seam.hello.projects}

    def start(self, key: str, instruction: str, now: float) -> L.Update:
        """Twin A, first turn: pick the project and ask. Nothing is launched until a yes."""
        with self.lock:
            down = self._down(key, now)
            if down is not None:
                return down
            projects = self.projects()
            names = {p["projectId"]: p["name"] for p in projects}
            hello_seq = (self.follow.seam.hello.latest_sequence,)
            pick = pick_project(instruction, projects)
            if pick.kind == "one":
                return self._confirm(Question(key, "confirm", instruction, (pick.project_id,)), names)
            if pick.kind == "ask":
                candidates = pick.candidates or tuple(names)
                self.question = Question(key, "project", instruction, tuple(candidates))
                return L.Update(key, pick.question, "needs_clarification", "pick", sequences=hello_seq)
            self.question = None
            return L.Update(key, f"I don't see that project. I can start threads in {', '.join(names.values())}.",
                            "failed", "pick", sequences=hello_seq)

    def _confirm(self, question: Question, names: dict[str, str]) -> L.Update:
        self.question = question
        return L.Update(question.key, f"Start it in {names[question.candidates[0]]}?", "needs_clarification",
                        "pick", sequences=(self.follow.seam.hello.latest_sequence,))

    def answer(self, text: str, now: float) -> L.Update | None:
        """The next utterance, read as the answer to the open question, or as taking back a held follow-up."""
        with self.lock:
            question, self.question = self.question, None
            just_held, self.just_held = self.just_held, False
            if question is None:
                return self._drop_by_words(text, just_held, now)
            if question.kind == "thread":
                return self._answer_thread(question, text, now)
            if question.kind == "thread_confirm":
                return self._answer_thread_confirm(question, text, now)
            kind = reply_kind(text)
            if kind == "no":
                return L.Update(question.key, START_CANCELLED, "cancelled", "pick")
            down = self._down(question.key, now)
            if down is not None:
                return down
            return self._answer_start(question, text, kind, now)

    def _answer_start(self, question: Question, text: str, kind: str | None, now: float) -> L.Update:
        projects = self.projects()
        names = {p["projectId"]: p["name"] for p in projects}
        is_question = _QUESTION.search(text) is not None
        if question.kind == "confirm":
            current = question.candidates[0]
            if kind == "yes" and current in names:
                return self._launch(question, current, names[current], now)
            if not is_question and _OTHER_PROJECT.search(text):
                others = tuple(pid for pid in names if pid != current)
                if len(others) == 1:
                    return self._confirm(replace(question, candidates=others, reasked=False), names)
                if others:
                    question = replace(question, kind="project", candidates=others)
        if question.kind == "project":
            projects = [p for p in projects if p["projectId"] in question.candidates]

        if not is_question:
            pick = pick_project(f"in {text}", projects)
            if pick.kind == "one" and (question.kind == "project" or (pick.project_id,) != question.candidates):
                return self._confirm(replace(question, kind="confirm", candidates=(pick.project_id,),
                                             reasked=False), names)

        if question.reasked:
            return L.Update(question.key, START_CANCELLED, "cancelled", "pick")
        question = replace(question, reasked=True)
        if question.kind == "confirm" and question.candidates[0] in names:
            return self._confirm(question, names)
        candidates = tuple(pid for pid in question.candidates if pid in names) or tuple(names)
        self.question = replace(question, kind="project", candidates=candidates)
        return L.Update(question.key, question_for_projects([names[pid] for pid in candidates]),
                        "needs_clarification", "pick", sequences=(self.follow.seam.hello.latest_sequence,))

    def _spoken(self, key: str, task, ack: str, row: str) -> L.Update:
        """What a write's own call made the lifecycle say, as the turn's answer. `ack` when it has said nothing yet."""
        mine = self.follow.take(task.task_id)
        if not mine:
            self.follow.acked.add(task.task_id)
            return L.Update(key, ack, STATUS_WORDS[task.status], row, task.link.thread_id)
        return L.Update(key, " ".join(u.line for u in mine), STATUS_WORDS[task.status], mine[0].row,
                        task.link.thread_id, tuple(s for u in mine for s in u.sequences))

    def _launch(self, question: Question, project_id: str, name: str, now: float) -> L.Update:
        task = self.follow.journal.begin(Kind.START, question.instruction, project_id, name)
        self.keys[task.task_id] = question.key
        self.follow.launch(task, now)
        self.recent = task.link.thread_id or self.recent
        return self._spoken(question.key, task, L.LAUNCH_ACK, "launch-unavailable")

    def about(self, key: str, op: str, instruction: str, now: float) -> L.Update:
        """Twin B for `op` (ask, tell or stop): list, pick, and ask back. Nothing reads, sends or stops until a yes."""
        with self.lock:
            down = self._down(key, now)
            if down is not None:
                return down
            listing = self._list(key)
            if isinstance(listing, L.Update):
                return listing
            reference, message, link = (tell_parts(instruction) if op == TELL else
                                        (_STOP_VERBS.sub(" ", instruction) if op == STOP else instruction, None, None))
            base = Question(key, "thread_confirm", instruction, (), op=op, message=message, link=link,
                            titles={t.thread_id: t.title for t in listing.threads},
                            projects={t.thread_id: t.project_id for t in listing.threads})
            evidence = (listing.as_of_sequence,)
            if not content_words(reference):
                recent = self._recent(base.titles)
                if recent is not None:
                    return self._ask_back(replace(base, candidates=(recent,)), evidence)
                newest = tuple(t.thread_id for t in listing.threads[:3])
                return self._which(replace(base, kind="thread", candidates=newest), evidence)
            pick, _ = pick_thread(reference, self._threads(listing), self.project_names(), _now_iso(),
                                  client=self.model)
            if pick.kind == "none":
                line = (f"I don't see it among your {len(listing.threads)} most recent threads." if listing.truncated
                        else "I don't see an active thread like that.")
                return L.Update(key, line, "completed", "ask", sequences=evidence)
            if pick.kind == "ask":
                return self._which(replace(base, kind="thread", candidates=pick.candidates), evidence,
                                   pick.question)
            return self._ask_back(replace(base, candidates=(pick.thread_id,)), evidence)

    def _list(self, key: str):
        try:
            return self.follow.seam.list_threads()
        except (SeamRefused, SeamUnavailable, SeamConnectionEnded):
            return L.Update(key, L.ROWS["ask"].line, "failed", "ask")

    @staticmethod
    def _threads(listing: T.ThreadList, among=None) -> list[dict]:
        return [{"threadId": t.thread_id, "title": t.title, "activity": t.activity.value, "projectId": t.project_id,
                 "lastActivityAt": t.last_activity_at}
                for t in listing.threads if among is None or t.thread_id in among]

    def _recent(self, listed: dict[str, str]) -> str | None:
        """The thread "it" means: the one this voice last spoke about, else the newest one it started or sent to."""
        if self.recent in listed:
            return self.recent
        return next((t.link.thread_id for t in reversed(self.follow.journal.tasks.values())
                     if t.link.thread_id in listed), None)

    def _ask_back(self, question: Question, evidence=()) -> L.Update:
        self.question = question = replace(question, evidence=tuple(evidence) or question.evidence)
        thread = question.candidates[0]
        line = confirm_line(question.op, question.titles[thread], question.message, question.link)
        return L.Update(question.key, line, "needs_clarification", "pick", thread, question.evidence)

    def _which(self, question: Question, evidence, line: str | None = None) -> L.Update:
        self.question = question
        line = line or question_for([question.titles[t] for t in question.candidates])
        return L.Update(question.key, line, "needs_clarification", "pick", sequences=evidence)

    def _answer_thread_confirm(self, question: Question, text: str, now: float) -> L.Update | None:
        kind = reply_kind(text, question.op)
        thread = question.candidates[0]
        if kind == "yes":
            down = self._down(question.key, now)
            return down if down is not None else self._act(question, thread, now)
        if kind == "no" and question.op != ASK:
            return L.Update(question.key, CANCELLED_LINES[question.op], "cancelled", "pick", thread)
        if kind == "no":
            return self._declined(question, thread, text, now)
        if question.reasked:
            return L.Update(question.key, CANCELLED_LINES[question.op], "cancelled", "pick", thread)
        return self._ask_back(replace(question, reasked=True))

    def _declined(self, question: Question, declined: str, text: str, now: float) -> L.Update | None:
        """A no to "Do you mean the X thread?": the thread the reply names, else which one, by up to three titles."""
        down = self._down(question.key, now)
        if down is not None:
            return down
        listing = self._list(question.key)
        if isinstance(listing, L.Update):
            return listing
        others = [t for t in self._threads(listing) if t["threadId"] != declined]
        question = replace(question, titles={t["threadId"]: t["title"] for t in others},
                           projects={t["threadId"]: t["projectId"] for t in others}, reasked=False)
        evidence = (listing.as_of_sequence,)
        words = " ".join(w for w in _WORD.findall(_plain(text).lower()) if w not in NO)
        pick = prefilter_pick(words, others, self.project_names()) if content_words(words) else None
        if pick is not None and pick.kind == "one":
            return self._ask_back(replace(question, candidates=(pick.thread_id,)), evidence)
        candidates = (pick.candidates if pick is not None and pick.kind == "ask" and pick.candidates
                      else tuple(t["threadId"] for t in others[:3]))
        if not candidates:
            return L.Update(question.key, "I don't see another active thread.", "completed", "ask",
                            sequences=evidence)
        return self._which(replace(question, kind="thread", candidates=tuple(candidates)[:3]), evidence)

    def _answer_thread(self, question: Question, text: str, now: float) -> L.Update | None:
        if question.op != ASK and reply_kind(text, question.op) == "no":
            return L.Update(question.key, CANCELLED_LINES[question.op], "cancelled", "pick")
        if self._down(question.key, now) is not None:
            return L.Update(question.key, L.ROWS["ask"].line, "failed", "ask")
        listing = self._list(question.key)
        if isinstance(listing, L.Update):
            return listing
        threads = self._threads(listing, among=question.candidates)
        pick = prefilter_pick(text, threads, self.project_names())
        if pick.kind != "one":
            return None
        if question.op == ASK:
            self.recent = pick.thread_id
            return self._read(question.key, pick.thread_id)
        question = replace(question, kind="thread_confirm", candidates=(pick.thread_id,), reasked=False,
                           titles={t["threadId"]: t["title"] for t in threads},
                           projects={t["threadId"]: t["projectId"] for t in threads})
        return self._ask_back(question, (listing.as_of_sequence,))

    def _act(self, question: Question, thread: str, now: float) -> L.Update:
        self.recent = thread
        if question.op == ASK:
            return self._read(question.key, thread)
        project = question.projects.get(thread)
        name = self.project_names().get(project)
        if question.op == TELL:
            task = self.follow.journal.begin(Kind.TELL, _sentence(question.message), project, name, thread_id=thread)
            self.keys[task.task_id] = question.key
            self.follow.send(task, now)
            self.just_held = task.status is Status.HELD
            return self._spoken(question.key, task, TELL_ACK, "tell-unavailable")
        dropped = self.follow.journal.held(thread)
        for held in dropped:
            self.follow.drop(held, now)
            self.follow.take(held.task_id)
        task = self.follow.journal.begin(Kind.STOP, question.instruction, project, name, thread_id=thread)
        self.keys[task.task_id] = question.key
        self.follow.stop(task, now)
        update = self._spoken(question.key, task, L.STOP_UNAVAILABLE, "stop")
        if dropped:
            update.line += STOP_DROPPED
        return update

    def _read(self, key: str, thread_id: str) -> L.Update:
        try:
            state = self.follow.seam.read_thread(thread_id)
        except (SeamRefused, SeamUnavailable, SeamConnectionEnded):
            return L.Update(key, L.ROWS["ask"].line, "failed", "ask")
        return L.Update(key, speak_state(state), "completed", "ask", thread_id, (state.as_of_sequence,))

    def _drop_by_words(self, text: str, just_held: bool, now: float) -> L.Update | None:
        held = self.follow.journal.held()
        text = _plain(text)
        if not held or not (_DROP_NAMED.search(text) or (just_held and _DROP_BARE.search(text))):
            return None
        task = held[-1]
        self.follow.drop(task, now)
        update = self._spoken(self.keys.get(task.task_id, task.task_id), task, L.DROPPED_LINE, "held-dropped")
        return update


def _sentence(text: str | None) -> str:
    text = (text or "").strip()
    return text[:1].upper() + text[1:]


T3_WAITING = {"t3": True}


class T3Registry:
    """The live registry with the T3 tools added to the decider's tool question."""

    def __init__(self, base):
        self._base = base

    def __getattr__(self, name):
        return getattr(self._base, name)

    def choices(self):
        base = self._base.choices()
        return {**{k: v for k, v in base.items() if k != "none"}, **T3_TOOLS, "none": base["none"]}


class T3TaskService(MockTaskService):
    """MockTaskService, with T3 records sent to `t3`. Other tasks keep the parent's behaviour.

    A T3 execution in a turn is never left running, so the turn never waits on a thread: the turn's own
    job (asking, starting, reading, sending, stopping) is done when it answers, and the thread's later news
    arrives as task updates at the next turn boundary.
    """

    def __init__(self, segmenter, conversation_id, *args, t3: T3Tasks, **kwargs):
        super().__init__(segmenter, conversation_id, *args, **kwargs)
        self.t3 = t3
        self.spoken: dict[str, L.Update] = {}

    def _threshold(self) -> float:
        return getattr(self.tools, "tool_threshold", 0.5) if self.tools is not None else 0.5

    def _start(self, task_id, instruction, side_effect, now, choice=None):
        if choice is None or choice.tool not in T3_TOOLS or choice.confidence < self._threshold():
            return super()._start(task_id, instruction, side_effect, now, choice)
        key = f"{self.conversation_id}:{task_id}"
        update = (self.t3.start(key, instruction, now) if choice.tool == START
                  else self.t3.about(key, choice.tool, instruction, now))
        return self._execution(task_id, update, now, choice.tool)

    def _execution(self, task_id, update: L.Update, now, tool):
        self.spoken[task_id] = update
        state = Run.FAILED if update.status in ("failed", "lost") else Run.COMPLETED
        return Execution(task_id, state, now, None, result_summary=update.line, finished_at=now, mode="t3",
                         tool={"tool": tool, "arguments": {}})

    def open_question(self):
        """T3 reads the next words when it asked a question, or while it holds a follow-up Mason may take back."""
        return T3_WAITING if self.t3.question is not None or self.t3.holding() else None

    def answer(self, waiting, instruction, now):
        if waiting is not T3_WAITING:
            return super().answer(waiting, instruction, now)
        tool = self.t3.question.op if self.t3.question is not None else TELL
        update = self.t3.answer(instruction, now)
        if update is None:
            return []
        task_id = f"t3_{len(self.extra) + 1}"
        self.extra[task_id] = {"task_id": task_id, "instruction": instruction, "status": "dispatched",
                               "depends_on": [], "side_effect": True}
        execution = self._execution(task_id, update, now, tool)
        self.executions[task_id] = execution
        return [{"task_id": task_id, "event": execution.state.value, "instruction": instruction}]

    def snapshot(self, utterance_id, final_transcript, unreviewed=None, notes=False):
        snapshot = super().snapshot(utterance_id, final_transcript, unreviewed, notes)
        for entry in snapshot["tasks"]:
            update = self.spoken.get(entry["task_id"])
            if update is not None:
                entry["spoken"] = update.line
                entry["evidence"] = update.evidence()
        return snapshot

    def take_updates(self):
        updates = super().take_updates()
        for update in self.t3.take():
            updates.append({"type": "task_update", "conversation_id": self.conversation_id,
                            "task_id": update.task_id, "description": "a T3 thread", "status": update.status,
                            "result_summary": update.line, "spoken": update.line, "evidence": update.evidence()})
        return updates

    def update_entries(self, updates):
        mine = [u for u in updates if "spoken" in u]
        return [*super().update_entries([u for u in updates if "spoken" not in u]), *mine]


def default_origin() -> str:
    return os.environ.get("VOICE_T3_ORIGIN", f"http://127.0.0.1:{FORK_PORT}")


def default_token_file() -> Path:
    return Path(os.environ.get("VOICE_T3_TOKEN_FILE", default_voice_home() / "t3-fork-home" / TOKEN_FILE))


def default_journal() -> Path:
    return Path(os.environ.get("VOICE_T3_JOURNAL", default_voice_home() / "state" / "t3-journal.json"))


def default_model() -> LiveClient | None:
    """The Twin B model arm, when a key is configured. Without one the prefilter answers alone."""
    if not (os.environ.get("OPENROUTER_API_KEY") or os.environ.get("VOICE_ENV_FILE")):
        return None
    return LiveClient(cache_dir=default_voice_home() / "state" / "t3-pick-cache")


class Poller:
    """Keeps the lifecycle current between turns on its own thread: it waits on seam events, not a timer."""

    def __init__(self, t3: T3Tasks, wait_s: float = 0.5):
        self.t3, self.wait_s = t3, wait_s
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._run, name="t3-follow", daemon=True)

    def start(self) -> Poller:
        self.thread.start()
        return self

    def _run(self) -> None:
        while not self.stopped.is_set():
            waited = self.t3.wait(self.wait_s)
            if waited is None:
                self.stopped.wait(self.wait_s)
            self.t3.poll(time.monotonic(), waited)

    def stop(self) -> None:
        self.stopped.set()
        self.thread.join(timeout=5)


class Script:
    """Drives T3Tasks the way the voice does, printing each spoken line with its evidence."""

    def __init__(self, t3: T3Tasks):
        self.t3, self.clock = t3, time.monotonic

    def show(self, label, said, update):
        line = {"line": None} if update is None else {"line": update.line, "status": update.status, "row": update.row,
                                                      "evidence": update.evidence()}
        print(json.dumps({"turn": label, "said": said, **line}), flush=True)
        return update

    def tool(self, label, tool, words):
        update = (self.t3.start(label, words, self.clock()) if tool == START
                  else self.t3.about(label, tool, words, self.clock()))
        return self.show(label, words, update)

    def reply(self, label, words):
        return self.show(label, words, self.t3.answer(words, self.clock()))

    def follow(self, until, timeout_s):
        """Apply seam items and print each update until `until(update)` holds or the time runs out."""
        deadline = self.clock() + timeout_s
        while self.clock() < deadline:
            waited = self.t3.wait(1.0)
            if waited is None:
                threading.Event().wait(1.0)
            self.t3.poll(self.clock(), waited)
            done = False
            for update in self.t3.take():
                self.show(f"watch {update.task_id}", None, update)
                done = done or until(update)
            if done:
                return True
        return False


def _done_for(key):
    return lambda u: u.task_id == key and (u.row in ("done-event", "gone-event") or u.status == "lost")


def drive(args) -> int:
    """One Twin A launch to done and one Twin B ask, through T3Tasks, printing each line with its evidence."""
    t3 = T3Tasks.open(args.origin, args.token_file, args.journal, default_model())
    script = Script(t3)
    t3.resume(script.clock())
    script.tool("a1", START, args.instruction)
    script.reply("a1", "Yes, start it.")
    finished = script.follow(_done_for("a1"), args.timeout_s)
    if args.ask:
        script.tool("b1", ASK, args.ask)
        script.reply("b1", "yes")
    t3.poll(script.clock() + L.DONE_WINDOW_S)
    for update in t3.take():
        script.show(f"watch {update.task_id}", None, update)
    if t3.follow.seam is not None:
        t3.follow.seam.close()
    return 0 if finished else 1


ASKS = ("How is the clamping work coming along?", "How's the ASR fix coming along?")


def smoke(args) -> int:
    """Launch, hold a follow-up until the run is done, ask through the model arm, and stop a second thread twice."""
    t3 = T3Tasks.open(args.origin, args.token_file, args.journal, default_model())
    script = Script(t3)
    t3.resume(script.clock())
    script.tool("a1", START, args.instruction)
    script.reply("a1", "Yes, start it.")
    script.tool("c1", TELL, args.tell)
    held = script.reply("c1", "Yes, send it.")
    ok = held is not None and held.row == "tell-busy"
    ok = script.follow(_done_for("a1"), args.timeout_s) and ok
    ok = script.follow(_done_for("c1"), args.timeout_s) and ok
    for n, ask in enumerate(args.ask or ASKS, 1):
        calls = t3.model.calls if t3.model else 0
        script.tool(f"b{n}", ASK, ask)
        print(json.dumps({"model_calls_for_the_ask": (t3.model.calls if t3.model else 0) - calls}), flush=True)
        script.reply(f"b{n}", "yes")
    script.tool("a2", START, args.second)
    script.reply("a2", "Yes, start it.")
    for label in ("s1", "s2"):
        script.tool(label, STOP, "Stop it.")
        script.reply(label, "Yes, stop it.")
    ok = script.follow(_done_for("a2"), args.timeout_s) and ok
    print(json.dumps({"model_calls": t3.model.calls if t3.model else 0}), flush=True)
    if t3.follow.seam is not None:
        t3.follow.seam.close()
    return 0 if ok else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("drive", help="launch, watch to done, then ask about a thread")
    run.add_argument("instruction")
    run.add_argument("--ask", help="a Twin B question to ask after the thread is done")
    whole = sub.add_parser("smoke", help="launch, a held follow-up, an ask, and a stop said twice")
    whole.add_argument("instruction")
    whole.add_argument("--tell", default="Tell it to also add a test.")
    whole.add_argument("--ask", action="append", help=f"a Twin B ask, repeatable (default: {ASKS})")
    whole.add_argument("--second", default="Add a sum helper in the sandbox project")
    for command in (run, whole):
        command.add_argument("--origin", default=default_origin())
        command.add_argument("--token-file", type=Path, default=default_token_file())
        command.add_argument("--journal", type=Path, default=default_journal())
        command.add_argument("--timeout-s", type=float, default=180.0)
    args = parser.parse_args(argv)
    return drive(args) if args.command == "drive" else smoke(args)


if __name__ == "__main__":
    raise SystemExit(main())
