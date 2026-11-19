"""The T3 task service: Twin A launches through the project picker, and Twin B asks about, tells, or stops the
thread the decider picks.

T3Tasks lives as long as the voice server. It owns the seam, the journal, and Follow, which runs the
watch lifecycle (voice/t3/lifecycle.py). T3TaskService is the per-turn task service the prototype
already uses, with records whose decider tool answer names a T3 tool sent to T3Tasks. Everything the
voice says comes out as typed acts (voice/t3/acts.py) built from a seam result, a stored event, or a
read, and carries that sequence as its evidence. `T3Tasks.say` has the voice's text model word the
acts, off the T3 lock (voice/t3/speech.py); this file holds no sentence the voice speaks.

Nothing acts on a pick alone. A lone project match is asked back as a confirm_launch, and a lone
thread as a confirm_thread, confirm_tell or confirm_stop. Only an explicit yes in the next turn
launches, reads, sends, or stops. A follow-up to a thread that is still working is held in the
journal and sent when that thread's run is done.

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
from pathlib import Path

from voice.prototype.tasks import Execution, MockTaskService, Run
from voice.t3 import lifecycle as L
from voice.t3 import types as T
from voice.t3.acts import Act, act
from voice.t3.acts import Kind as Say
from voice.t3.fork_env import FORK_PORT, TOKEN_FILE, default_voice_home
from voice.t3.journal import Kind, Status, T3Journal
from voice.t3.pick.model import LiveClient
from voice.t3.pick.project import pick_project
from voice.t3.realtime_writer import RealtimeWriter
from voice.t3.seam import SeamConnectionEnded, SeamRefused, SeamUnavailable, T3Seam
from voice.t3.speech import Lane, LocalText, Rendered, Speech, text_model
from voice.t3.verify import NONE, Decider, Listed, Picked, Reading, default_decider

START, ASK, TELL, STOP = "t3_start", "t3_ask", "t3_tell", "t3_stop"
# The newest threads the decider chooses among. Its picks held, at about 185 ms, with 40 listed.
PICK_LIMIT = 40
MAX_ALTERNATIVES = 8
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

OPS = {START: "start", ASK: "ask", TELL: "tell", STOP: "stop"}

_LEAD = r"^\W*(?:(?:ok(?:ay)?|so|and|also|please|hey|actually|now)[,\s]+)*(?:(?:can|could|would) you\s+)?(?:please\s+)?"
_TELL = re.compile(_LEAD + r"(?:tell|remind|ask|let)\s+(?P<ref>.+?)\s+(?:know\s+)?(?P<link>to|that)\s+"
                   r"(?P<message>.+?)[.!?\s]*$", re.IGNORECASE)
_STOP_VERBS = re.compile(r"\b(?:stop|halt|interrupt|abort|kill|cancel)\b", re.IGNORECASE)
# Mason takes back a held follow-up by naming it at any time, or with a bare "never mind" right after the hold.
_DROP_NAMED = re.compile(r"\b(?:don'?t|do not|no need to)\s+(?:pass|send|tell|give)\b|\b(?:cancel|drop|forget|"
                         r"scrap|scratch|never ?mind)\s+(?:about\s+)?(?:the|that|my)\s+(?:message|follow[- ]?up|"
                         r"note|request)\b", re.IGNORECASE)
_DROP_BARE = re.compile(r"^\W*(?:no[,\s]+)?(?:never ?mind|cancel (?:that|it)|forget (?:it|that)|scratch that|"
                        r"don'?t bother)\b", re.IGNORECASE)


def _plain(text: str) -> str:
    return (text or "").replace("’", "'")


def tell_parts(instruction: str) -> tuple[str, str, str | None]:
    """(the words that name the thread, the message to send, "to" or "that" or None when the words have no split)."""
    match = _TELL.match(_plain(instruction).strip())
    if match is None:
        return instruction, instruction.strip(), None
    return match["ref"], match["message"], match["link"].lower()


def confirm_act(op: str, title: str, message: str | None = None) -> Act:
    """The confirming question for one picked thread. Only a yes to it acts."""
    if op == TELL:
        return act(Say.CONFIRM_TELL, thread=title, message=message)
    if op == STOP:
        return act(Say.CONFIRM_STOP, thread=title)
    return act(Say.CONFIRM_THREAD, thread=title)


def status_act(state: T.ThreadState, asked: str | None = None) -> Act:
    """A read as an act. Its status and need come from the read; `say` adds the summary before it is spoken."""
    need = state.pending
    return act(Say.THREAD_STATUS, thread=state.title, status=state.activity.value,
               need=L.need_of(need.kind, need.source) if need is not None else None,
               pending=need.prompt if need is not None else None,
               waiting_for=need.kind.value if need is not None else None, asked=asked)


@dataclass
class Question:
    """The open question the next utterance may answer.

    `kind` is the state: "project" (which project), "confirm" (start it in this project?), "thread" (which
    thread), or "thread_confirm" (one thread, asked back for `op`). `candidates` are project or thread ids, and
    `titles` names each thread. `message` is a follow-up's words, from `tell_parts`.

    An answer authorizes only this question: asked in `call`, heard (its audio was sent), and answered on the next
    turn. `owner` is the turn that asked it and `expired` says a later turn ended without answering it.
    """

    key: str
    kind: str
    instruction: str
    candidates: tuple[str, ...]
    op: str = START
    titles: dict[str, str] = field(default_factory=dict)
    projects: dict[str, str] = field(default_factory=dict)
    message: str | None = None
    evidence: tuple[int, ...] = ()
    call: int = 0
    owner: object = None
    heard: bool = False
    heard_at: float = 0.0
    asked_at: float = 0.0
    expired: bool = False


CONFIRM_TTL_S = 90.0  # How long a heard question can be answered.
MAX_IDLE_DELIVERY_FAILURES = 2  # The first attempt and one retry before waiting for a person turn.


@dataclass(frozen=True)
class Lookup:
    """A reply that asks for another thread than the one or ones offered: `question`'s request is looked up again
    by the reply's words, off the lock, among every thread but `without`."""

    question: Question
    without: str | None = None


@dataclass
class T3Tasks:
    """The summarizer uses `model`. A live `writer` overrides the text model only for drafting acts.
    The decider checks both model outputs before the voice can send audio."""

    follow: L.Follow
    model: LiveClient | LocalText | None = None
    decider: Decider | None = None
    question: Question | None = None
    keys: dict[str, str] = field(default_factory=dict)
    recent: str | None = None
    just_held: bool = False
    lock: threading.RLock = field(default_factory=threading.RLock)
    unasked: L.Update | None = field(default=None, init=False)
    call: int = field(default=0, init=False)
    lane: Lane | None = field(default=None, init=False)
    writer: RealtimeWriter | None = field(default=None, kw_only=True)
    speech: Speech = field(init=False)

    def __post_init__(self) -> None:
        self.lane = Lane(self.model) if self.model is not None else None
        self.speech = Speech(self.lane, self._read_quiet, self.decider, Lane(self.writer) if self.writer else None)

    @classmethod
    def open(cls, origin: str, token_file: str | Path, journal_path: str | Path,
             model: LiveClient | LocalText | None = None, decider: Decider | None = None, *,
             writer: RealtimeWriter | None = None) -> T3Tasks:
        follow = L.Follow(T3Journal(journal_path), lambda: T3Seam.connect(origin, token_file))
        return cls(follow, model, decider, writer=writer)

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

    def has_news(self, *, idle=False) -> bool:
        with self.lock:
            return any((not idle or update.delivery_failures < MAX_IDLE_DELIVERY_FAILURES) and self.askable(update)
                       for update in self.follow.outbox)

    def askable(self, update: L.Update) -> bool:
        """A question is news only while it is the open one nobody has heard: once heard, answered or dropped, saying
        it would ask something no reply can answer."""
        return update.status != "needs_clarification" or (update is self.unasked and self.live_question() is not None)

    def reserve_news(self, *, idle=False) -> list[L.Update]:
        """Claim the news for a turn. The journal keeps it pending until audio is sent."""
        with self.lock:
            stale = [update for update in self.follow.outbox if not self.askable(update)]
            if stale:
                self.follow.outbox = [update for update in self.follow.outbox if self.askable(update)]
                self.follow.journal.remove_lines(stale)
            if not idle:
                updates, self.follow.outbox = self.follow.outbox, []
                return updates
            updates, deferred = [], []
            for update in self.follow.outbox:
                if update.delivery_failures < MAX_IDLE_DELIVERY_FAILURES:
                    updates.append(update)
                else:
                    deferred.append(update)
            self.follow.outbox = deferred
            return updates

    def news_entry(self, update: L.Update, conversation_id: str) -> dict:
        return update_entry(replace(update, task_id=self.keys.get(update.task_id, update.task_id)), conversation_id)

    def ack_news(self, update: L.Update) -> None:
        with self.lock:
            self.follow.journal.remove_lines([update])

    def retry_news(self, updates: list[L.Update]) -> None:
        with self.lock:
            pending = self.follow.journal.pending_lines
            saved = set(map(id, pending))
            for update in updates:
                update.delivery_failures = min(update.delivery_failures + 1, MAX_IDLE_DELIVERY_FAILURES)
                if id(update) not in saved:
                    pending.append(update)
            self.follow.outbox[:0] = [u for u in updates if not self.follow.overtaken(u)]
            self.follow.journal.save()

    def superseded(self, update: L.Update) -> bool:
        """Whether a newer update about `update`'s thread has replaced it, so it must never be said."""
        with self.lock:
            if not update.superseded and self.follow.overtaken(update):
                self.follow.journal.save()
            return update.superseded

    def give_back(self, updates: list[L.Update]) -> None:
        """Updates a turn took and never spoke, for the next one to take."""
        with self.lock:
            self.follow.give_back(updates)

    def holding(self) -> bool:
        return bool(self.follow.journal.held())

    def _down(self, key: str, now: float) -> L.Update | None:
        seam = self.follow.ensure(now)
        if self.follow.dead is not None:
            return L.Update(key, (L.row_act(L.ROWS[self.follow.dead]),), "failed", "ask")
        if seam is None:
            return L.Update(key, (act(Say.UNREACHABLE),), "failed", "ask")
        return None

    def _read_quiet(self, thread_id: str) -> T.ThreadState | None:
        """A read off the T3 lock, for a summary. The seam's calls are safe from any thread."""
        seam = self.follow.seam
        if seam is None:
            return None
        try:
            return seam.read_thread(thread_id)
        except (SeamRefused, SeamUnavailable, SeamConnectionEnded):
            return None

    def begin_call(self) -> None:
        """A new call starts: nothing asked on an earlier one can be answered on it."""
        with self.lock:
            self.call += 1
            self._drop_question()

    def end_call(self) -> None:
        with self.lock:
            self._drop_question()

    def _drop_question(self) -> None:
        self.question = self.unasked = None
        self.just_held = False

    def _ask(self, question: Question) -> Question:
        """Open `question` as a new prompt: this call's, not yet heard."""
        self.question = question = replace(question, call=self.call, owner=None, heard=False, heard_at=0.0,
                                           asked_at=time.monotonic(), expired=False)
        return question

    def live_question(self) -> Question | None:
        """The open question when an answer may still authorize it, else None, and the stale one is dropped: one from
        another call, one a later turn ended without answering, or one past CONFIRM_TTL_S since it was heard."""
        with self.lock:
            question = self.question
            if question is None:
                return None
            since = question.heard_at if question.heard else question.asked_at
            if question.call != self.call or question.expired or time.monotonic() - since > CONFIRM_TTL_S:
                self._drop_question()
                return None
            return question

    def spoke(self, update: L.Update) -> None:
        """`update` was said aloud, whole. If it is the open question, the person has heard it and the next turn may
        answer it. Anything else said after it moves the conversation on, so the question is dropped."""
        with self.lock:
            question = self.question
            if question is None:
                return
            if update.task_id == question.key and update.status == "needs_clarification":
                question.heard, question.heard_at = True, time.monotonic()
                if self.unasked is update:
                    self.unasked = None
            elif question.heard:
                self._drop_question()

    def turn_closed(self, owner: object) -> None:
        """A turn ended. A question that an earlier turn asked and this one did not answer can no longer be."""
        with self.lock:
            question = self.question
            if question is not None and question.owner is not None and question.owner is not owner:
                question.expired = True

    def bind_request(self, prefix: str, transcript: str) -> None:
        """The launch carries everything the person said in the turn that asked, not the fragment that asked."""
        with self.lock:
            question = self.question
            if (question is not None and question.op == START and question.key.startswith(prefix)
                    and (transcript or "").strip()):
                question.instruction = transcript.strip()

    def claim(self, update: L.Update, owner: object) -> None:
        """The turn `owner` asked the question `update` says."""
        with self.lock:
            question = self.question
            if question is not None and question.key == update.task_id:
                question.owner = owner

    def asking_start(self, prefix: str) -> bool:
        """Whether a launch question that a task with this key prefix (one turn's) asked is still open."""
        with self.lock:
            question = self.question
            return (question is not None and question.op == START and question.kind in ("confirm", "project")
                    and question.key.startswith(prefix))

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
            candidates = (pick.project_id,) if pick.kind == "one" else tuple(pick.candidates or names)
            if not candidates:
                self._drop_question()
                return L.Update(key, (act(Say.NO_PROJECT),), "failed", "pick", sequences=hello_seq)
            if len(candidates) == 1:
                return self._confirm(Question(key, "confirm", instruction, candidates), names)
            self._ask(Question(key, "project", instruction, candidates))
            return L.Update(key, (which_project(candidates, names),), "needs_clarification", "pick",
                            sequences=hello_seq)

    def _confirm(self, question: Question, names: dict[str, str]) -> L.Update:
        question = self._ask(question)
        project_id = question.candidates[0]
        said = act(Say.CONFIRM_LAUNCH, project=names[project_id])
        return L.Update(question.key, (said,), "needs_clarification", "pick",
                        sequences=(self.follow.seam.hello.latest_sequence,), project_id=project_id)

    def pending_act(self, question: Question) -> Act | None:
        """The question the voice asked, as the act it said."""
        names = self.project_names() if self.follow.seam is not None else {}
        if question.kind == "confirm":
            name = names.get(question.candidates[0])
            return act(Say.CONFIRM_LAUNCH, project=name) if name else None
        if question.kind == "project":
            return which_project(question.candidates, names)
        if question.kind == "thread_confirm":
            return confirm_act(question.op, question.titles[question.candidates[0]], question.message)
        return act(Say.ASK_WHICH, of="thread", candidates=[question.titles[t] for t in question.candidates])

    def alternatives(self, question: Question) -> dict[str, str]:
        """What a reply could be choosing, as name to id: the other projects for a launch question, and the listed
        threads for "which thread". Empty for "do you mean this thread", whose no is matched against thread titles."""
        if question.kind == "thread":
            return {question.titles[t]: t for t in question.candidates}
        if question.kind not in ("confirm", "project") or self.follow.seam is None:
            return {}
        names = self.project_names()
        ids = ([pid for pid in names if pid != question.candidates[0]] if question.kind == "confirm"
               else [pid for pid in question.candidates if pid in names])
        return {names[pid]: pid for pid in ids[:MAX_ALTERNATIVES]}

    def read_reply(self, text: str, question: Question | None) -> Reading | None:
        """What a reply does about the open question, by the decider: the one reading of it there is. Only a confident,
        unconditional yes acts, and a reply nobody could read (no decider, a timeout) answers nothing. Off the lock,
        since the decider is a network call."""
        reads = getattr(self.decider, "reads", None)
        said = self.pending_act(question) if question is not None and reads is not None else None
        if said is None:
            return None
        return reads(said, _plain(text), tuple(self.alternatives(question)))

    def answer(self, text: str, now: float, reading: Reading | None = None, question: Question | None = None
               ) -> L.Update | None:
        """The next utterance, read as the answer to the open question, or as taking back a held follow-up.

        `question` and `reading` are the open question and the reading of the reply when the caller made them off the
        lock. Left out, they are read here. If the open question is no longer that one when the reading is back, the
        reply was for a prompt that has gone, and it answers nothing."""
        if question is None:
            question = self.live_question()
            reading = self.read_reply(text, question) if question is not None else None
        kind = reading.kind if reading is not None else None
        update = self._answer(text, now, reading, kind, question)
        if not isinstance(update, Lookup):
            return update
        again = self._refer(update.question, _plain(text), now, without=update.without, recent=False)
        if update.without is not None or again.acts != (self.pending_act(update.question),):
            return again
        # The same "which thread" again: it is never asked twice. Only that question is dropped, not one another
        # request opened while the decider was picking.
        with self.lock:
            if self.question is not None and (self.question.key, self.question.kind) == (update.question.key, "thread"):
                self._drop_question()
        return None

    def _answer(self, text: str, now: float, reading: Reading | None, kind: str | None, question: Question | None):
        with self.lock:
            if self.question is not question:
                return None
            unheard = self.question is not None and self.question.owner is not None and not self.question.heard
            if self.question is not None and (unheard or (self.unasked is not None
                                                           and self.question.key == self.unasked.task_id)):
                # The person never heard this question, so nothing they said answers it: it is asked again.
                if reading is not None and reading.label not in (None, "unrelated") and self.unasked is not None:
                    return self.unasked
                self._drop_question()
                return None
            question, self.question = self.question, None
            just_held, self.just_held = self.just_held, False
            if question is None:
                return self._drop_by_words(text, just_held, now)
            if question.kind == "thread":
                return self._answer_thread(question, text, reading, now)
            if question.kind == "thread_confirm":
                return self._answer_thread_confirm(question, text, kind, now)
            down = self._down(question.key, now) if kind != "no" else None
            if down is not None:
                return down
            return self._answer_start(question, text, reading, now)

    def _answer_start(self, question: Question, text: str, reading: Reading | None, now: float) -> L.Update | None:
        projects = self.projects()
        names = {p["projectId"]: p["name"] for p in projects}
        if reading is None or reading.label in (None, "question", "unrelated"):
            return None
        if question.kind == "confirm" and reading.yes and question.candidates[0] in names:
            return self._launch(question, question.candidates[0], names[question.candidates[0]], now)
        # The reply asks for another project: the one it names, else the one the decider read it as changing to.
        others = self.alternatives(question)
        if question.kind == "project":
            projects = [p for p in projects if p["projectId"] in question.candidates]
        pick = pick_project(f"in {text}", projects)
        target = pick.project_id if pick.kind == "one" else others.get(reading.target)
        if target is None and reading.elsewhere and question.kind == "confirm":
            # It moves off the proposed project without naming one: the only other, or a question of which.
            if len(others) == 1:
                target = next(iter(others.values()))
            elif others:
                question = self._ask(replace(question, kind="project", candidates=tuple(others.values())))
                return L.Update(question.key, (which_project(question.candidates, names),), "needs_clarification",
                                "pick", sequences=(self.follow.seam.hello.latest_sequence,))
        if target is not None and (question.kind == "project" or (target,) != question.candidates):
            return self._confirm(replace(question, kind="confirm", candidates=(target,)), names)
        if reading.kind == "no":
            return L.Update(question.key, (act(Say.CANCELLED, op="start"),), "cancelled", "pick")
        # A reply that answers nothing drops the question. It is never asked again, and the turn goes on as talk.
        return None

    def _spoken(self, key: str, task, ack: Act, row: str) -> L.Update:
        """What a write's own call made the lifecycle say, as the turn's answer. `ack` when it has said nothing yet."""
        mine = self.follow.take(task.task_id)
        if not mine:
            self.follow.acked.add(task.task_id)
            return L.Update(key, (L.about(task, ack),), STATUS_WORDS[task.status], row, task.link.thread_id)
        update = L.Update(key, tuple(a for u in mine for a in u.acts), STATUS_WORDS[task.status], mine[0].row,
                          task.link.thread_id, tuple(s for u in mine for s in u.sequences), n=max(u.n for u in mine))
        self.follow.journal.replace_lines(mine, update)
        return update

    def _launch(self, question: Question, project_id: str, name: str, now: float) -> L.Update:
        task = self.follow.journal.begin(Kind.START, question.instruction, project_id, name)
        self.keys[task.task_id] = question.key
        self.follow.launch(task, now)
        self.recent = task.link.thread_id or self.recent
        return self._spoken(question.key, task, act(Say.LAUNCH_PENDING), "launch-unavailable")

    def about(self, key: str, op: str, instruction: str, now: float) -> L.Update:
        """List threads, pick a target, and ask the speaker to confirm or choose it."""
        reference, message, _ = (tell_parts(instruction) if op == TELL else
                                 (_STOP_VERBS.sub(" ", instruction) if op == STOP else instruction, None, None))
        return self._refer(Question(key, "thread_confirm", instruction, (), op=op, message=message), reference, now)

    def _refer(self, asked: Question, reference: str, now: float, without: str | None = None,
               recent: bool = True) -> L.Update:
        """The thread `reference` means, for the request `asked` carries, by the decider's choice among the listed
        threads. One it is sure of answers a question at once, one it leans to is confirmed back, and words that fit
        several are asked about, by the titles they fit. `without` is a thread the person just said no to, and
        `recent` lets "it" mean the thread the call last spoke about."""
        key = asked.key
        with self.lock:
            down = self._down(key, now)
            if down is not None:
                return down
            listing = self._list(key)
            if isinstance(listing, L.Update):
                return listing
            listed = [t for t in listing.threads if t.thread_id != without]
            base = replace(asked, kind="thread_confirm", candidates=(),
                           titles={t.thread_id: t.title for t in listed},
                           projects={t.thread_id: t.project_id for t in listed})
            evidence = (listing.as_of_sequence,)
            options, shared = self._options(listed)
            # The decider saw only `options`, so "not listed" is true of those: a thread past them is not ruled out.
            partial = listing.truncated or len({t.title.casefold() for t in listed}) > len(options)
            none = L.Update(key, (act(Say.NO_THREAD, other=True) if without is not None else
                                  act(Say.NO_THREAD, recent=len(options) if partial else None),),
                            "completed", "ask", sequences=evidence)
            if not listed:
                return none
            last = base.titles.get(self._recent(base.titles)) if recent else None
            call = self.call

        picked = self._picked(reference, options, last)

        with self.lock:
            if self.call != call:
                # The call ended while the decider was picking: nothing is asked or read for a call that has gone.
                return L.Update(key, (L.row_act(L.ROWS["ask"]),), "failed", "ask")
            if picked.choice == NONE:
                return none
            thread = picked.thread
            if thread is None:
                candidates = picked.candidates[:3] or tuple(t.thread_id for t in listed[:3])
                return self._which(replace(base, kind="thread", candidates=candidates), evidence)
            if asked.op == ASK and picked.sure and thread not in shared:
                return self._act(base, thread, now)
            return self._ask_back(replace(base, candidates=(thread,)), evidence)

    def names_thread(self, instruction: str, now: float) -> bool:
        """Whether a message's words mean a thread there is, or may mean several, by the decider. Off the lock."""
        with self.lock:
            if self._down("", now) is not None:
                return False
            listing = self._list("")
            if isinstance(listing, L.Update) or listing is None or not listing.threads:
                return False
            options, _ = self._options(listing.threads)
        picked = self._picked(tell_parts(instruction)[0], options, None)
        return picked.thread is not None or bool(picked.candidates)

    def _options(self, listed) -> tuple[tuple[Listed, ...], frozenset[str]]:
        """The threads the decider chooses among, newest first, and those whose title another thread has too. Threads
        that share a title cannot be told apart by voice, so the newest stands for the title."""
        names, options, shared = self.project_names(), {}, set()
        for t in listed:
            title = t.title.casefold()
            if title in options:
                shared.add(options[title].thread_id)
            elif len(options) < PICK_LIMIT:
                options[title] = Listed(t.thread_id, t.title, names.get(t.project_id, ""), t.activity.value)
        return tuple(options.values()), frozenset(shared)

    def _picked(self, reference: str, options: tuple[Listed, ...], last: str | None) -> Picked:
        """The decider's pick, off the lock since it is a network call. With no decider nothing is picked."""
        picks = getattr(self.decider, "picks", None)
        return picks(reference, options, last) if picks is not None else Picked(None)

    def _list(self, key: str):
        try:
            return self.follow.seam.list_threads()
        except (SeamRefused, SeamUnavailable, SeamConnectionEnded):
            return L.Update(key, (L.row_act(L.ROWS["ask"]),), "failed", "ask")

    def _recent(self, listed: dict[str, str]) -> str | None:
        """The thread "it" means: the one this voice last spoke about, else the newest one it started or sent to."""
        if self.recent in listed:
            return self.recent
        return next((t.link.thread_id for t in reversed(self.follow.journal.tasks.values())
                     if t.link.thread_id in listed), None)

    def _ask_back(self, question: Question, evidence=()) -> L.Update:
        """Confirm the one candidate back. A message or a stop for a title that another listed thread shares is not
        offered at all: the question could only name the title, and a yes to it could act on the wrong thread."""
        thread = question.candidates[0]
        title = question.titles[thread]
        if question.op != ASK and sum(t.casefold() == title.casefold() for t in question.titles.values()) > 1:
            return L.Update(question.key, (act(Say.SAME_TITLE, thread=title, op=OPS[question.op]),), "completed",
                            "ask", sequences=tuple(evidence) or question.evidence)
        question = self._ask(replace(question, evidence=tuple(evidence) or question.evidence))
        said = confirm_act(question.op, question.titles[thread], question.message)
        return L.Update(question.key, (said,), "needs_clarification", "pick", thread, question.evidence)

    def _which(self, question: Question, evidence) -> L.Update:
        """Ask which of the candidates. Threads that share a title cannot be told apart by voice, so the newest of
        them stands for the title (`titles` is newest first), and one title left is confirmed, not asked about."""
        newest: dict[str, str] = {}
        for thread in sorted(question.candidates, key=list(question.titles).index):
            newest.setdefault(question.titles[thread].casefold(), thread)
        question = replace(question, candidates=tuple(t for t in question.candidates if t in newest.values()))
        if len(question.candidates) == 1:
            return self._ask_back(replace(question, kind="thread_confirm"), evidence)
        question = self._ask(question)
        said = act(Say.ASK_WHICH, of="thread", candidates=[question.titles[t] for t in question.candidates])
        return L.Update(question.key, (said,), "needs_clarification", "pick", sequences=evidence)

    def _answer_thread_confirm(self, question: Question, text: str, kind: str | None, now: float):
        thread = question.candidates[0]
        if kind == "yes":
            down = self._down(question.key, now)
            return down if down is not None else self._act(question, thread, now)
        cancelled = act(Say.CANCELLED, op=OPS[question.op], thread=question.titles.get(thread))
        if kind == "no" and question.op != ASK:
            return L.Update(question.key, (cancelled,), "cancelled", "pick", thread)
        return Lookup(question, thread) if kind == "no" else None

    def _answer_thread(self, question: Question, text: str, reading: Reading | None, now: float):
        """A reply to "which thread": the one the decider reads it as choosing, by name or by place. A reply that
        asks for a thread that was not offered is looked up among every thread. Any other drops the question, which
        is never asked twice."""
        kind = reading.kind if reading is not None else None
        if question.op != ASK and kind == "no":
            return L.Update(question.key, (act(Say.CANCELLED, op=OPS[question.op]),), "cancelled", "pick")
        if self._down(question.key, now) is not None:
            return L.Update(question.key, (L.row_act(L.ROWS["ask"]),), "failed", "ask")
        listing = self._list(question.key)
        if isinstance(listing, L.Update):
            return listing
        threads = [t for t in listing.threads if t.thread_id in question.candidates]
        chosen = self.alternatives(question).get(reading.target) if reading is not None else None
        if chosen not in {t.thread_id for t in threads}:
            return Lookup(question) if reading is not None and reading.elsewhere else None
        question = replace(question, titles={t.thread_id: t.title for t in listing.threads},
                           projects={t.thread_id: t.project_id for t in listing.threads})
        if question.op == ASK:
            return self._act(question, chosen, now)
        return self._ask_back(replace(question, kind="thread_confirm", candidates=(chosen,)),
                              (listing.as_of_sequence,))

    def _act(self, question: Question, thread: str, now: float) -> L.Update:
        self.recent = thread
        if question.op == ASK:
            return self._read(question.key, thread, question.instruction)
        project = question.projects.get(thread)
        name = self.project_names().get(project)
        title = question.titles.get(thread)
        if question.op == TELL:
            task = self.follow.journal.begin(Kind.TELL, _sentence(question.message), project, name, thread_id=thread,
                                             title=title)
            self.keys[task.task_id] = question.key
            self.follow.send(task, now)
            self.just_held = task.status is Status.HELD
            return self._spoken(question.key, task, act(Say.TELL_PENDING), "tell-unavailable")
        dropped = self.follow.journal.held(thread)
        for held in dropped:
            self.follow.drop(held, now)
            self.follow.ack(self.follow.take(held.task_id))
        task = self.follow.journal.begin(Kind.STOP, question.instruction, project, name, thread_id=thread, title=title)
        self.keys[task.task_id] = question.key
        self.follow.stop(task, now)
        update = self._spoken(question.key, task, act(Say.STOP_UNCONFIRMED), "stop")
        if dropped:
            update.acts += (L.about(task, act(Say.DROPPED_FOLLOWUP)),)
        return update

    def _read(self, key: str, thread_id: str, asked: str | None = None) -> L.Update:
        try:
            state = self.follow.seam.read_thread(thread_id)
        except (SeamRefused, SeamUnavailable, SeamConnectionEnded):
            return L.Update(key, (L.row_act(L.ROWS["ask"]),), "failed", "ask")
        self.speech.remember(state)
        return L.Update(key, (status_act(state, asked),), "completed", "ask", thread_id, (state.as_of_sequence,))

    def prefetch(self, instruction: str) -> None:
        """The speculative start of a t3_ask the decider marked mid-utterance: when the words so far already pick one
        thread, read it and start its summary on the lane, so the answer after the yes need not wait for it."""
        seam = self.follow.seam
        if seam is None or self.lane is None:
            return

        def run():
            try:
                listing = seam.list_threads()
            except (SeamRefused, SeamUnavailable, SeamConnectionEnded):
                return
            titles = {t.thread_id: t.title for t in listing.threads}
            with self.lock:
                options, _ = self._options(listing.threads)
                last = titles.get(self._recent(titles))
            thread = self._picked(instruction, options, last).thread
            if thread is not None:
                self.speech.prefetch(thread, instruction)

        threading.Thread(target=run, name="t3-prefetch", daemon=True).start()

    def known_names(self) -> frozenset[str]:
        seam = self.follow.seam
        projects = {p.name for p in seam.hello.projects} if seam is not None else set()
        with self.lock:
            threads = {task.title for task in self.follow.journal.tasks.values() if task.title}
        if seam is not None:
            try:
                listing = seam.list_threads()
                if listing is not None:
                    threads.update(thread.title for thread in listing.threads)
            except (SeamRefused, SeamUnavailable, SeamConnectionEnded):
                pass
        return frozenset(projects | threads)

    def say(self, update: L.Update, turns=()) -> Rendered:
        """The text model's words for `update`'s acts, fitted to `turns`, off the T3 lock. A summarised act gets its
        thread's summary first, and the read it came from joins the update's evidence. A line that fails the slot
        check is not returned, and its problems go into the next attempt."""
        acts, read = self.speech.fill(update.acts, update.thread_id, update.sequences)
        update.acts = acts
        update.sequences = tuple(dict.fromkeys((*update.sequences, *read)))
        rendered = self.speech.render(acts, turns, update.problems, update.refused, update.tries, known_names=self.known_names())
        refuse(update, rendered)
        return rendered

    def say_entry(self, update: L.Update, entry: dict, turns=()) -> Rendered:
        """Draft the entry with its updated acts and evidence."""
        rendered = self.say(update, turns)
        entry["acts"] = [a.to_json() for a in update.acts]
        entry["evidence"] = update.evidence()
        return rendered

    def _drop_by_words(self, text: str, just_held: bool, now: float) -> L.Update | None:
        held = self.follow.journal.held()
        text = _plain(text)
        if not held or not (_DROP_NAMED.search(text) or (just_held and _DROP_BARE.search(text))):
            return None
        task = held[-1]
        self.follow.drop(task, now)
        return self._spoken(self.keys.get(task.task_id, task.task_id), task, act(Say.DROPPED_FOLLOWUP), "held-dropped")


@dataclass(frozen=True)
class Words:
    """Draft and check the call's reply against the same update."""

    t3: T3Tasks
    update_for: object

    def say(self, entry, turns=()) -> Rendered:
        return self.t3.say_entry(self.update_for(entry), entry, turns)

    def accept(self, entry, text, draft) -> Rendered:
        return self.t3.speech.recite(draft, text)

    def refuse(self, entry, rendered) -> None:
        refuse(self.update_for(entry), rendered)


def refuse(update: L.Update, rendered: Rendered) -> None:
    """Keep a failed wording's problems and the wording itself on the update, for the next attempt to avoid."""
    update.problems = tuple(rendered.problems or ([rendered.error] if rendered.error else []))
    if rendered.line is None:
        update.tries += 1
        if rendered.raw and rendered.raw not in update.refused:
            update.refused += (rendered.raw,)


def which_project(candidates, names: dict[str, str]) -> Act:
    return act(Say.ASK_WHICH, of="project", candidates=[names[pid] for pid in candidates if pid in names])


def _sentence(text: str | None) -> str:
    text = (text or "").strip()
    return text[:1].upper() + text[1:]


def update_entry(update: L.Update, conversation_id: str) -> dict:
    """One lifecycle update as a task update: its acts, and the sequences they came from. `spoken` joins it once the
    voice has worded it."""
    return {"type": "task_update", "conversation_id": conversation_id, "task_id": update.task_id,
            "description": "a T3 thread", "status": update.status, "result_summary": None,
            "acts": [a.to_json() for a in update.acts], "row": update.row, "evidence": update.evidence()}


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
        self.news: dict[int, L.Update] = {}
        self.delivered: set[str] = set()
        self.prefetched: set[str] = set()

    def _threshold(self) -> float:
        return getattr(self.tools, "tool_threshold", 0.5) if self.tools is not None else 0.5

    def _start(self, task_id, instruction, side_effect, now, choice=None):
        if choice is None or choice.tool not in T3_TOOLS or choice.confidence < self._threshold():
            return super()._start(task_id, instruction, side_effect, now, choice)
        key = f"{self.conversation_id}:{task_id}"
        if (choice.tool in (START, TELL) and self.t3.asking_start(f"{self.conversation_id}:")
                and not (choice.tool == TELL and self.t3.names_thread(instruction, now))):
            # One long request is cut into several segments. The first already asked, and the launch carries the whole
            # turn, so another launch, or an instruction that names no thread there is, is part of it.
            return Execution(task_id, Run.COMPLETED, now, None, result_summary=None, finished_at=now, mode="t3",
                             tool={"tool": choice.tool, "arguments": {}})
        update = (self.t3.start(key, instruction, now) if choice.tool == START
                  else self.t3.about(key, choice.tool, instruction, now))
        return self._execution(task_id, update, now, choice.tool)

    def _execution(self, task_id, update: L.Update, now, tool):
        with self.t3.lock:
            self.spoken[task_id] = update
            if update.status == "needs_clarification":
                self.t3.unasked = update
                self.t3.claim(update, self)
        state = Run.FAILED if update.status in ("failed", "lost") else Run.COMPLETED
        return Execution(task_id, state, now, None, result_summary=None, finished_at=now, mode="t3",
                         tool={"tool": tool, "arguments": {}})

    def sync(self, now):
        """The parent's sync, plus the speculative start of each t3_ask the decider has marked, even mid-utterance."""
        events = super().sync(now)
        for record in self.segmenter.records():
            tool = record.get("tool") or {}
            if (tool.get("tool") == ASK and tool.get("confidence", 0) >= self._threshold()
                    and record["task_id"] not in self.prefetched):
                self.prefetched.add(record["task_id"])
                self.t3.prefetch(record["instruction"])
        return events

    def update_for(self, entry) -> L.Update:
        if entry.get("type") == "task_update":
            return self.news[id(entry)]
        return self.spoken[entry["task_id"]]

    def say(self, entry, turns=()) -> Rendered:
        return Words(self.t3, self.update_for).say(entry, turns)

    def accept(self, entry, text, draft) -> Rendered:
        return Words(self.t3, self.update_for).accept(entry, text, draft)

    def refuse(self, entry, rendered) -> None:
        Words(self.t3, self.update_for).refuse(entry, rendered)

    def open_question(self):
        """T3 reads the next words when it asked a question, or while it holds a follow-up Mason may take back."""
        return T3_WAITING if self.t3.live_question() is not None or self.t3.holding() else None

    def answer(self, waiting, instruction, now):
        if waiting is not T3_WAITING:
            return super().answer(waiting, instruction, now)
        # A request dispatched mid-utterance may already have asked its own confirmation.
        with self.t3.lock:
            if any(update is self.t3.unasked for update in self.spoken.values()):
                return []
        question = self.t3.live_question()
        reading = self.t3.read_reply(instruction, question) if question is not None else None
        tool = question.op if question is not None else TELL
        update = self.t3.answer(instruction, now, reading, question)
        if update is None:
            return []
        task_id = f"t3_{len(self.extra) + 1}"
        self.extra[task_id] = {"task_id": task_id, "instruction": instruction, "status": "dispatched",
                               "depends_on": [], "side_effect": True}
        execution = self._execution(task_id, update, now, tool)
        self.executions[task_id] = execution
        return [{"task_id": task_id, "event": execution.state.value, "instruction": instruction}]

    def snapshot(self, utterance_id, final_transcript, unreviewed=None, notes=False):
        self.t3.bind_request(f"{self.conversation_id}:", final_transcript)
        snapshot = super().snapshot(utterance_id, final_transcript, unreviewed, notes)
        for entry in snapshot["tasks"]:
            update = self.spoken.get(entry["task_id"])
            if update is not None:
                entry["acts"] = [a.to_json() for a in update.acts]
                entry["evidence"] = update.evidence()
        return snapshot

    def take_task_updates(self):
        """The ordinary tasks' updates only. T3 news is said on its own, when the call is quiet."""
        return super().take_updates()

    def take_updates(self):
        entries = super().take_updates()
        for update in self.t3.reserve_news():
            entry = self.t3.news_entry(update, self.conversation_id)
            self.news[id(entry)] = update
            entries.append(entry)
        return entries

    def record_delivery(self, entry, state):
        """Only a line whose whole audio was sent is delivered. One cut off part way stays pending, and `close`
        gives it back to be said again."""
        if state != "sent":
            return
        if entry.get("type") == "task_update":
            self.t3.ack_news(self.news.pop(id(entry)))
        else:
            task_id = entry["task_id"]
            self.delivered.add(task_id)
            self.executions[task_id].result_summary = entry.get("spoken")
            with self.t3.lock:
                self.t3.ack_news(self.spoken[task_id])
                self.t3.spoke(self.spoken[task_id])

    def close(self):
        super().close()
        self.t3.turn_closed(self)
        # A question nobody heard goes back too, to be asked when the call is quiet: no reply can answer it unheard.
        with self.t3.lock:
            unsent = [update for task_id, update in self.spoken.items()
                      if task_id not in self.delivered and self.t3.askable(update)]
        unsent.extend(self.news.values())
        if unsent:
            self.t3.retry_news(unsent)

    def update_entries(self, updates):
        mine = [u for u in updates if "acts" in u]
        return [*super().update_entries([u for u in updates if "acts" not in u]), *mine]


def default_origin() -> str:
    return os.environ.get("VOICE_T3_ORIGIN", f"http://127.0.0.1:{FORK_PORT}")


def default_token_file() -> Path:
    return Path(os.environ.get("VOICE_T3_TOKEN_FILE", default_voice_home() / "t3-fork-home" / TOKEN_FILE))


def default_journal() -> Path:
    return Path(os.environ.get("VOICE_T3_JOURNAL", default_voice_home() / "state" / "t3-journal.json"))


def live_writer(url: str) -> RealtimeWriter | None:
    """The local session that words T3 lines, unless VOICE_RENDERER hands that to the text model. A paid model named
    in VOICE_PAID_TEXT_MODEL only writes the thread summaries: on a live call it once worded the lines too, timed out
    once, and its cooldown left nine turns with no T3 line."""
    if os.environ.get("VOICE_RENDERER"):
        return None
    return RealtimeWriter(url)


def default_model() -> LiveClient | LocalText | None:
    def cloud() -> LiveClient | None:
        if not (os.environ.get("OPENROUTER_API_KEY") or os.environ.get("VOICE_ENV_FILE")):
            return None
        return LiveClient(cache_dir=default_voice_home() / "state" / "t3-pick-cache")

    return text_model(cloud=cloud)


class Poller:
    """Keeps the lifecycle current between turns on its own thread: it waits on seam events, not a timer. `notify`
    is called on this thread after a poll that leaves news to speak."""

    def __init__(self, t3: T3Tasks, wait_s: float = 0.5, notify=None):
        self.t3, self.wait_s, self.notify = t3, wait_s, notify
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._run, name="t3-follow", daemon=True)

    def start(self) -> Poller:
        self.thread.start()
        return self

    def _run(self) -> None:
        while not self.stopped.is_set():
            deadline = time.monotonic() + self.wait_s
            waited = self.t3.wait(self.wait_s)
            if waited is None or waited[1] is None:
                self.stopped.wait(max(0.0, deadline - time.monotonic()))
            self.t3.poll(time.monotonic(), waited)
            if self.notify is not None and self.t3.has_news(idle=True):
                self.notify()

    def stop(self) -> None:
        self.stopped.set()
        self.thread.join(timeout=5)


class Script:
    """Drives T3Tasks the way the voice does, printing each spoken line with its acts and evidence.

    There is no LFM3 here, so an update whose words fail waits, and is worded again before the next turn."""

    def __init__(self, t3: T3Tasks):
        self.t3, self.clock = t3, time.monotonic
        self.turns: list[dict] = []
        self.waiting: list[tuple[str, L.Update]] = []

    def show(self, label, said, update):
        if said is not None:
            self.turns.append({"user": said})
            self.retry()
        if update is None:
            print(json.dumps({"turn": label, "said": said, "line": None}), flush=True)
            return update
        self.speak(label, said, update)
        return update

    def speak(self, label, said, update, retry=False) -> bool:
        rendered = self.t3.say(update, self.turns)
        verdict = rendered.verdict
        row = {"turn": label, "said": said, "line": rendered.text, "acts": [a.to_json() for a in update.acts],
               "status": update.status, "row": update.row, "evidence": update.evidence(),
               "decider": None if verdict is None else {"passed": verdict.passed, "ms": verdict.ms,
                                                        "failed": list(verdict.failed), "error": verdict.error}}
        if retry:
            row["retried"] = True
        if rendered.line is None:
            row["waiting"] = rendered.problems or rendered.error
            row["refused"] = rendered.raw
            self.waiting.append((label, update))
        else:
            self.turns.append({"assistant": rendered.line.text})
            self.t3.ack_news(update)
            self.t3.spoke(update)
        print(json.dumps(row), flush=True)
        return rendered.line is not None

    def retry(self) -> None:
        waiting, self.waiting = self.waiting, []
        for label, update in waiting:
            if self.t3.superseded(update):
                print(json.dumps({"turn": label, "superseded": [a.to_json() for a in update.acts],
                                  "evidence": update.evidence()}), flush=True)
                continue
            self.speak(label, None, update, retry=True)

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
    t3 = T3Tasks.open(args.origin, args.token_file, args.journal, default_model(), default_decider())
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
    t3 = T3Tasks.open(args.origin, args.token_file, args.journal, default_model(), default_decider())
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
    script.retry()
    print(json.dumps({"titles": launched_titles(t3)}), flush=True)
    print(json.dumps({"model_calls": t3.model.calls if t3.model else 0,
                      "lane_requests": t3.lane.calls if t3.lane else 0,
                      "decider_calls": t3.decider.calls if t3.decider else 0, "still_waiting": len(script.waiting)}),
          flush=True)
    if t3.follow.seam is not None:
        t3.follow.seam.close()
    return 0 if ok and not script.waiting else 1


def launched_titles(t3: T3Tasks) -> dict:
    """Each thread this voice launched, with the instruction it started from and the title it has now."""
    titles = {}
    for task in t3.follow.journal.tasks.values():
        if task.kind is Kind.START and task.link.thread_id:
            state = t3._read_quiet(task.link.thread_id)
            titles[task.link.thread_id[-8:]] = {"instruction": task.instruction,
                                                "title": state.title if state is not None else None}
    return titles


def transcript_rows(records: Path) -> list[dict]:
    """Every spoken line of a live call's records, oldest turn first, with the T3 lines it was asked to say and the
    evidence each carries: the stored event sequences, or a read's `asOfSequence`."""
    rows = []
    for path in sorted(records.glob("turn_*.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        tasks = (record.get("snapshot_after_tools") or {}).get("tasks") or []
        t3 = [{"line": t.get("spoken"), "acts": [a["kind"] for a in t["acts"]], **t["evidence"]}
              for t in tasks if t.get("acts")]
        t3 += [{"line": u.get("spoken"), "acts": [a["kind"] for a in u["acts"]], "row": u.get("row"), **u["evidence"]}
               for u in record.get("task_updates") or () if u.get("acts")]
        rows.append({"record": path.name, "idle": bool(record.get("idle")), "heard": record.get("final_transcript"),
                     "t3": t3, "said": [r["text"] for r in record.get("replies") or ()
                                       if r.get("text") and r.get("delivery", "sent") == "sent"],
                     "given_back": bool(record.get("given_back"))})
    return rows


def transcript(args) -> int:
    print("| record | heard | T3 acts: line (thread, sequences) | voice said |")
    print("| --- | --- | --- | --- |")
    for row in transcript_rows(args.records):
        heard = "(quiet: thread news)" if row["idle"] else row["heard"] or ""
        lines = "<br>".join(f"{', '.join(t['acts'])}: {t['line']} ({(t.get('thread_id') or '-')[:8]}, {t['sequences']})"
                            for t in row["t3"])
        said = "<br>".join(row["said"]) or ("(given back, nothing heard)" if row["given_back"] else "")
        print(f"| {row['record']} | {heard} | {lines} | {said} |")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    lines = sub.add_parser("transcript", help="a live call's spoken lines with their T3 evidence, from its records")
    lines.add_argument("records", type=Path, help="the live server's --out directory")
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
    return {"drive": drive, "smoke": smoke, "transcript": transcript}[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
