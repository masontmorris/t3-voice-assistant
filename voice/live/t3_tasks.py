"""The T3 task service: Twin A launches through the project picker, Twin B asks about a thread.

T3Tasks lives as long as the voice server. It owns the seam, the journal, and Follow, which runs the
watch lifecycle (voice/t3/lifecycle.py). T3TaskService is the per-turn task service the prototype
already uses, with records whose decider tool answer names t3_start or t3_ask sent to T3Tasks. Every
spoken line comes from a seam result, a stored event, or a read, and carries that sequence as its
evidence.

Twin A never launches on the pick alone. A lone project match is asked back as "Start it in Y?", and
only a yes in the next turn launches. Twin B is read-only: list, pick, read, speak.

    python -m voice.live.t3_tasks drive "Add a clamp helper in the sandbox project" \
        --ask "How is the clamp helper thread going?"

drives one launch, its watch to done, and one Twin B ask against the fork environment
(voice/t3/fork_env.py), printing each line with its evidence.
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
from voice.t3.pick.pick import question_for
from voice.t3.pick.project import pick_project, question_for_projects
from voice.t3.seam import SeamConnectionEnded, SeamRefused, SeamUnavailable, T3Seam

START, ASK = "t3_start", "t3_ask"
T3_TOOLS = {
    START: "Start a new T3 coding thread that does the work the speaker asks for, in a project they name. "
           "Use it when they ask to start, open, or kick off a thread or a coding task.",
    ASK: "Report how an existing T3 coding thread is going. Use it when the speaker asks about a thread's "
         "progress, state, or result.",
}
STATUS_WORDS = {Status.UNSTARTED: "running", Status.LIVE: "running", Status.COMPLETED: "completed",
                Status.FAILED: "failed", Status.CANCELLED: "cancelled", Status.LOST: "lost"}

_CONFIRMATION_TEXT = re.compile(r"[a-zA-Z.,!\s]+")
_WORD = re.compile(r"[a-z']+")
_QUESTION = re.compile(r"\?|\b(?:what|when|where|which|who|whom|whose|why|how)\b", re.IGNORECASE)
_OTHER_PROJECT = re.compile(r"\b(?:in|for) (?:the )?other project(?:,? please)?[.! ]*$", re.IGNORECASE)
YES = frozenset({"yes", "yeah", "yep", "yup", "sure", "ok", "okay", "affirmative"})
YES_ACTIONS = {("do", "it"), ("go", "ahead"), ("start", "it")}
NO = frozenset({"no", "nope", "nah", "don't", "dont", "cancel", "stop", "wait", "never", "not"})
START_CANCELLED = "Okay, I won't start it."


def reply_kind(text: str) -> str | None:
    text = text or ""
    words = tuple(_WORD.findall(text.lower().replace("\u2019", "'")))
    if NO.intersection(words):
        return "no"
    if not words or not _CONFIRMATION_TEXT.fullmatch(text) or _QUESTION.search(text):
        return None
    if words == ("please",):
        return "yes"
    affirmative = words[0] in YES
    rest = words[1:] if affirmative else words
    if rest[-1:] == ("please",):
        rest = rest[:-1]
    if (affirmative and not rest) or rest in YES_ACTIONS:
        return "yes"
    return None


def speak_state(state: T.ThreadState) -> str:
    """One short line from a read, in the thread's own words where it has them."""
    title = L.speakable(state.title, sentences=1, limit=80).rstrip(".!?:")
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
    """The open question the next utterance may answer: which project, "Start it in Y?", or which thread."""

    key: str
    kind: str
    instruction: str
    candidates: tuple[str, ...]
    reasked: bool = False


@dataclass
class T3Tasks:
    follow: L.Follow
    question: Question | None = None
    keys: dict[str, str] = field(default_factory=dict)
    lock: threading.RLock = field(default_factory=threading.RLock)

    @classmethod
    def open(cls, origin: str, token_file: str | Path, journal_path: str | Path) -> T3Tasks:
        follow = L.Follow(T3Journal(journal_path), lambda: T3Seam.connect(origin, token_file))
        return cls(follow)

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
        with self.lock:
            question, self.question = self.question, None
            if question is None:
                return None
            if question.kind == "thread":
                return self._answer_thread(question, text, now)
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

    def _launch(self, question: Question, project_id: str, name: str, now: float) -> L.Update:
        task = self.follow.journal.begin(Kind.START, question.instruction, project_id, name)
        self.keys[task.task_id] = question.key
        self.follow.launch(task, now)
        mine = self.follow.take(task.task_id)
        if not mine:
            self.follow.acked.add(task.task_id)
            return L.Update(question.key, L.LAUNCH_ACK, STATUS_WORDS[task.status], "launch-unavailable",
                            task.link.thread_id)
        return L.Update(question.key, " ".join(u.line for u in mine), STATUS_WORDS[task.status], mine[0].row,
                        task.link.thread_id, tuple(s for u in mine for s in u.sequences))

    def ask_about(self, key: str, instruction: str, now: float) -> L.Update:
        """Twin B: list, pick, read, speak. No watch starts (row ask)."""
        with self.lock:
            down = self._down(key, now)
            if down is not None:
                return down
            seam = self.follow.seam
            try:
                listing = seam.list_threads()
            except (SeamRefused, SeamUnavailable, SeamConnectionEnded):
                return L.Update(key, L.ROWS["ask"].line, "failed", "ask")
            threads = [{"threadId": t.thread_id, "title": t.title, "activity": t.activity.value,
                        "projectId": t.project_id, "lastActivityAt": t.last_activity_at} for t in listing.threads]
            pick, _ = pick_thread(instruction, threads, self.project_names(), datetime.now(timezone.utc), client=None)
            evidence = (listing.as_of_sequence,)
            if pick.kind == "none":
                line = (f"I don't see it among your {len(listing.threads)} most recent threads." if listing.truncated
                        else "I don't see an active thread like that.")
                return L.Update(key, line, "completed", "ask", sequences=evidence)
            if pick.kind == "ask":
                self.question = Question(key, "thread", instruction, pick.candidates)
                return L.Update(key, pick.question, "needs_clarification", "ask", sequences=evidence)
            return self._read(key, pick.thread_id)

    def _answer_thread(self, question: Question, text: str, now: float) -> L.Update | None:
        seam = self.follow.ensure(now)
        if seam is None:
            return L.Update(question.key, L.ROWS["ask"].line, "failed", "ask")
        try:
            listing = seam.list_threads()
        except (SeamRefused, SeamUnavailable, SeamConnectionEnded):
            return L.Update(question.key, L.ROWS["ask"].line, "failed", "ask")
        threads = [{"threadId": t.thread_id, "title": t.title, "activity": t.activity.value,
                    "projectId": t.project_id} for t in listing.threads if t.thread_id in question.candidates]
        pick, _ = pick_thread(text, threads, self.project_names(), datetime.now(timezone.utc), client=None)
        if pick.kind != "one":
            return None
        return self._read(question.key, pick.thread_id)

    def _read(self, key: str, thread_id: str) -> L.Update:
        try:
            state = self.follow.seam.read_thread(thread_id)
        except (SeamRefused, SeamUnavailable, SeamConnectionEnded):
            return L.Update(key, L.ROWS["ask"].line, "failed", "ask")
        return L.Update(key, speak_state(state), "completed", "ask", thread_id, (state.as_of_sequence,))

    def stop(self, key: str, now: float) -> L.Update | None:
        """Interrupt the thread a voice task started. A retry reports the first saved result."""
        with self.lock:
            task_id = next((t for t, k in self.keys.items() if k == key), None)
            task = self.follow.journal.tasks.get(task_id) if task_id else None
            if task is None or task.link.thread_id is None:
                return None
            line = self.follow.stop(task, now)
            stop = task.link.stop or {}
            return L.Update(key, line, STATUS_WORDS[task.status], "stop", task.link.thread_id,
                            (stop["sequence"],) if stop.get("sequence") is not None else ())


T3_WAITING = {"t3": True}


class T3Registry:
    """The live registry with the two T3 tools added to the decider's tool question."""

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
    job (asking, starting, or reading) is done when it answers, and the thread's later news arrives as
    task updates at the next turn boundary.
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
                  else self.t3.ask_about(key, instruction, now))
        return self._execution(task_id, update, now, choice.tool)

    def _execution(self, task_id, update: L.Update, now, tool):
        self.spoken[task_id] = update
        state = Run.FAILED if update.status in ("failed", "lost") else Run.COMPLETED
        return Execution(task_id, state, now, None, result_summary=update.line, finished_at=now, mode="t3",
                         tool={"tool": tool, "arguments": {}})

    def open_question(self):
        return T3_WAITING if self.t3.question is not None else None

    def answer(self, waiting, instruction, now):
        if waiting is not T3_WAITING:
            return super().answer(waiting, instruction, now)
        update = self.t3.answer(instruction, now)
        if update is None:
            return []
        task_id = f"t3_{len(self.extra) + 1}"
        self.extra[task_id] = {"task_id": task_id, "instruction": instruction, "status": "dispatched",
                               "depends_on": [], "side_effect": True}
        execution = self._execution(task_id, update, now, START if update.row != "ask" else ASK)
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


def drive(args) -> int:
    """One Twin A launch to done and one Twin B ask, through T3Tasks, printing each line with its evidence."""
    t3 = T3Tasks.open(args.origin, args.token_file, args.journal)
    clock = time.monotonic

    def show(label, update):
        if update is not None:
            print(json.dumps({"turn": label, "line": update.line, "status": update.status, "row": update.row,
                              "evidence": update.evidence()}), flush=True)

    t3.resume(clock())
    show("start", t3.start("a1", args.instruction, clock()))
    show("yes", t3.answer("Yes, start it.", clock()))
    deadline = clock() + args.timeout_s
    finished = False
    while clock() < deadline and not finished:
        waited = t3.wait(1.0)
        if waited is None:
            threading.Event().wait(1.0)
        t3.poll(clock(), waited)
        for update in t3.take():
            show("watch", update)
            finished = finished or update.row in ("done-event", "gone-event") or update.status == "lost"
    if args.ask:
        show("ask", t3.ask_about("b1", args.ask, clock()))
    t3.poll(clock() + L.DONE_WINDOW_S)
    for update in t3.take():
        show("watch", update)
    if t3.follow.seam is not None:
        t3.follow.seam.close()
    return 0 if finished else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("drive", help="launch, watch to done, then ask about a thread")
    run.add_argument("instruction")
    run.add_argument("--ask", help="a Twin B question to ask after the thread is done")
    run.add_argument("--origin", default=default_origin())
    run.add_argument("--token-file", type=Path, default=default_token_file())
    run.add_argument("--journal", type=Path, default=default_journal())
    run.add_argument("--timeout-s", type=float, default=180.0)
    args = parser.parse_args(argv)
    return drive(args)


if __name__ == "__main__":
    raise SystemExit(main())
