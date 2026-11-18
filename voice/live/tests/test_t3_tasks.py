"""The T3 task service confirms writes, resolves reads, and attaches evidence to thread updates."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from voice.live.t3_tasks import (ASK, START, STOP, T3_TOOLS, T3_WAITING, TELL, Script, T3Registry, T3TaskService,
                                 T3Tasks, reply_kind, status_act, tell_parts, transcript_rows)
from voice.prototype.run import say_acts, task_note
from voice.prototype.tests.test_run_shared_session import FakeSession
from voice.t3 import fakes as F
from voice.t3 import lifecycle as L
from voice.t3 import types as T
from voice.t3.acts import QUESTIONS, RULES, Act, act, check
from voice.t3.acts import Kind as S
from voice.t3.journal import Kind, Status, T3Journal
from voice.t3.speech import RENDER_SYSTEM, Lane, Speech, kept_exact
from voice.segmentation.gate import PLAN_A
from voice.tools import registry as tool_registry

TWO_PROJECTS = (("p1", "Voice sandbox", ("sandbox", "voice sandbox")),
                ("p2", "Voice server", ("voice server", "the server")))
THREADS = (("t5", "Add a clamp helper"), ("t6", "Fix the flaky ASR test"), ("t7", "Upgrade the toolchain"),
           ("t8", "Write the release notes"))


def tasks_over(test: unittest.TestCase, seam: F.FakeSeam, model=None, path: Path | None = None) -> T3Tasks:
    if path is None:
        tmp = tempfile.TemporaryDirectory()
        test.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "journal.json"
    return T3Tasks(L.Follow(T3Journal(path), F.Connector(seam)), model)


def said(update) -> list[tuple]:
    """Each act as its kind, the named facts its line must say, and its status, op or reason."""
    return [(a.kind.value, *[a.slots[n] for n in RULES[a.kind].named if n in a.slots],
             *[a.slots[n] for n in ("status", "op", "reason") if n in a.slots]) for a in update.acts]


class FakeText:
    """The voice's text model: a line built from the acts' exact facts, which passes the slot check."""

    model = "fake/text:free"
    GIST = "It added the helper."

    def __init__(self, line=None):
        self.line, self.calls = line, 0
        self.summarised = threading.Event()

    def complete(self, messages):
        self.calls += 1
        if messages[0]["content"] != RENDER_SYSTEM:
            self.summarised.set()
            return {"text": self.GIST, "ms": 1, "tokens": {}}
        body = json.loads(messages[1]["content"])
        if self.line is not None:
            return {"text": self.line, "ms": 1, "tokens": {}}
        return {"text": self.wording(body["keep_exact"], body["ask"]), "ms": 1, "tokens": {}}

    @staticmethod
    def wording(exact, ask):
        facts = ", ".join(exact) or "your threads"
        return f"About {facts}, any thoughts?" if ask else f"About {facts}, noted."


def worded(t3: T3Tasks, text: FakeText) -> T3Tasks:
    t3.lane = Lane(text)
    t3.speech = Speech(t3.lane, t3._read_quiet)
    return t3


def listing(*rows, truncated=False, as_of=77):
    threads = tuple(T.ThreadSummary(tid, "p1", title, T.ThreadActivity.WORKING, "2026-11-17T10:00:00Z", False)
                    for tid, title in rows)
    return T.ThreadList(threads, len(threads) + (5 if truncated else 0), truncated, as_of)


class FakeModel:
    """The model arm's client: one scripted answer, or an error, per call."""

    model = "fake/pick:free"

    def __init__(self, choice=None, error=None):
        self.answer, self.error, self.calls = {"choice": choice, "candidates": []}, error, 0

    def complete(self, messages):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return {"text": json.dumps(self.answer), "ms": 12, "tokens": {}}


class BlockingModel(FakeModel):
    def __init__(self):
        super().__init__("t5")
        self.started = 0
        self.entered = threading.Event()
        self.release = threading.Event()

    def complete(self, messages):
        self.started += 1
        self.entered.set()
        self.release.wait()
        return super().complete(messages)


def busy_seam(read_as_of=150) -> F.FakeSeam:
    seam = F.FakeSeam(read=F.state(thread="t5", title="Add a clamp helper", as_of=read_as_of))
    seam.listing = listing(*THREADS)
    return seam.answer("send", F.busy())


def held_follow_up(test, seam, path=None) -> T3Tasks:
    t3 = tasks_over(test, seam, path=path)
    t3.about("c1", TELL, "Tell the clamp helper thread to also add a test.", 0)
    test.assertEqual(said(t3.answer("Yes, send it.", 1)), [("held_followup", "Add a clamp helper")])
    return t3


class TwinA(unittest.TestCase):
    def test_a_lone_match_is_asked_back_before_any_launch(self):
        seam = F.FakeSeam()
        t3 = tasks_over(self, seam)
        asked = t3.start("k1", "Add a clamp helper in the sandbox project", 0)
        self.assertEqual((said(asked), asked.status), ([("confirm_launch", "Voice sandbox")], "needs_clarification"))
        self.assertEqual(asked.sequences, (1,))
        self.assertEqual(seam.ops("launch"), [])
        started = t3.answer("Yes, go ahead.", 1)
        self.assertEqual(said(started), [("launched", "Voice sandbox")])
        self.assertEqual((started.status, started.sequences, started.thread_id), ("running", (93,), "t1"))
        self.assertEqual(seam.ops("launch")[0][2:], ("p1", "Add a clamp helper in the sandbox project"))
        self.assertEqual(seam.ops("watch"), [("watch", "t1", 93, "r1")])
        self.assertEqual(t3.take(), [])

    def test_no_starts_nothing(self):
        seam = F.FakeSeam()
        t3 = tasks_over(self, seam)
        t3.start("k1", "Add a clamp helper in the sandbox project", 0)
        self.assertEqual(said(t3.answer("No, don't.", 1)), [("cancelled", "start")])
        self.assertEqual(seam.ops("launch"), [])
        self.assertIsNone(t3.question)

    def test_an_unclear_reply_reasks_once_then_drops_the_request(self):
        seam = F.FakeSeam()
        t3 = tasks_over(self, seam)
        t3.start("k1", "Add a clamp helper in the sandbox project", 0)

        again = t3.answer("What do you mean?", 1)
        self.assertEqual((said(again), again.status), ([("confirm_launch", "Voice sandbox")], "needs_clarification"))
        self.assertEqual(seam.ops("launch"), [])
        dropped = t3.answer("I'm thinking about it.", 2)
        self.assertEqual((said(dropped), dropped.status), ([("cancelled", "start")], "cancelled"))
        self.assertIsNone(t3.answer("yes", 3))
        self.assertEqual(seam.ops("launch"), [])

    def test_a_confirmation_can_be_retargeted_but_still_needs_a_yes(self):
        for reply in ("start it in the other project", "yes, start it in the voice server"):
            with self.subTest(reply=reply):
                seam = F.FakeSeam(projects=TWO_PROJECTS)
                t3 = tasks_over(self, seam)
                t3.start("k1", "Add a clamp helper in the sandbox project", 0)

                again = t3.answer(reply, 1)
                self.assertEqual((said(again), again.status), ([("confirm_launch", "Voice server")],
                                                               "needs_clarification"))
                self.assertEqual(seam.ops("launch"), [])
                self.assertEqual(said(t3.answer("yes please", 2)), [("launched", "Voice server")])
                self.assertEqual(seam.ops("launch")[0][2], "p2")

    def test_a_project_named_in_the_answer_is_still_confirmed(self):
        seam = F.FakeSeam(projects=TWO_PROJECTS)
        t3 = tasks_over(self, seam)
        asked = t3.start("k1", "Start a thread that fixes the flaky test", 0)
        self.assertEqual(said(asked), [("ask_which", ["Voice sandbox", "Voice server"])])
        again = t3.answer("The voice server.", 1)
        self.assertEqual(said(again), [("confirm_launch", "Voice server")])
        self.assertEqual(seam.ops("launch"), [])
        self.assertEqual(said(t3.answer("yes", 2)), [("launched", "Voice server")])
        self.assertEqual(seam.ops("launch")[0][2], "p2")

    def test_a_refused_launch_says_why(self):
        seam = F.FakeSeam().answer("launch", F.refusal("refused", "T3 refused it.", reason="Model is off."))
        t3 = tasks_over(self, seam)
        t3.start("k1", "Add a clamp helper in the sandbox project", 0)
        failed = t3.answer("yes", 1)
        self.assertEqual((said(failed), failed.status), ([("refused", "start", "Model is off.")], "failed"))

    def test_a_dead_seam_says_so_and_launches_nothing(self):
        t3 = tasks_over(self, F.FakeSeam())
        t3.follow = L.Follow(t3.follow.journal, F.Connector(T.UPGRADE_404))
        update = t3.start("k1", "Add a clamp helper in the sandbox project", 0)
        self.assertEqual((said(update), update.status), ([("seam_down", "seam_off")], "failed"))




class TwinB(unittest.TestCase):
    def test_a_lone_pick_is_asked_back_and_a_yes_reads_it(self):
        seam = F.FakeSeam(read=F.state(thread="t5", title="Add a clamp helper", as_of=140,
                                       activity=T.ThreadActivity.FINISHED,
                                       last_reply=T.LastReply("Done. I added clamp. Tests pass.", "m1", None,
                                                              "r1", "2026-11-17T10:00:00Z")))
        seam.listing = listing(("t5", "Add a clamp helper"), ("t6", "Fix the flaky ASR test"))
        model = FakeModel("t6")
        t3 = tasks_over(self, seam, model)
        asked = t3.about("k2", ASK, "How is the clamp helper thread going?", 0)
        self.assertEqual((said(asked), asked.sequences), ([("confirm_thread", "Add a clamp helper")], (77,)))
        self.assertEqual((seam.ops("read"), model.calls), ([], 0))
        update = t3.answer("yes", 1)
        self.assertEqual(said(update), [("thread_status", "Add a clamp helper", "finished")])
        self.assertEqual((update.sequences, update.status), ((140,), "completed"))
        self.assertEqual(seam.ops("watch"), [])

    def test_none_depends_on_truncation(self):
        for truncated, expected in ((False, act(S.NO_THREAD)), (True, act(S.NO_THREAD, recent=1))):
            seam = F.FakeSeam()
            seam.listing = listing(("t6", "Fix the flaky ASR test"), truncated=truncated)
            update = tasks_over(self, seam).about("k2", ASK, "How is the billing thread going?", 0)
            self.assertEqual((update.acts, update.sequences), ((expected,), (77,)))

    def test_a_tie_asks_and_the_answer_reads(self):
        seam = F.FakeSeam(read=F.state(thread="t6", title="Fix the flaky ASR test", as_of=150))
        seam.listing = listing(("t5", "Fix the flaky UI test"), ("t6", "Fix the flaky ASR test"))
        t3 = tasks_over(self, seam, FakeModel("t5"))
        asked = t3.about("k2", ASK, "How is the flaky test thread going?", 0)
        self.assertEqual(asked.status, "needs_clarification")
        self.assertIn("Fix the flaky ASR test", asked.acts[0].slots["candidates"])
        answered = t3.answer("The ASR one.", 1)
        self.assertEqual((said(answered), answered.sequences),
                         ([("thread_status", "Fix the flaky ASR test", "working")], (150,)))
        self.assertEqual(t3.model.calls, 0)

    def test_ambiguous_or_unmatched_title_answer_asks_again_without_reading(self):
        for reply in ("The flaky test.", "The billing one."):
            with self.subTest(reply=reply):
                seam = F.FakeSeam(read=F.state(thread="t6", title="Fix the flaky ASR test", as_of=150))
                seam.listing = listing(("t5", "Fix the flaky UI test"), ("t6", "Fix the flaky ASR test"))
                t3 = tasks_over(self, seam)
                asked = t3.about("k2", ASK, "How is the flaky test thread going?", 0)

                again = t3.answer(reply, 1)

                self.assertIsNotNone(again)
                self.assertEqual((again.acts, again.status), (asked.acts, "needs_clarification"))
                self.assertEqual(seam.ops("read"), [])
                self.assertEqual(t3.answer("The ASR one.", 2).thread_id, "t6")

    def test_a_read_names_a_pending_need(self):
        state = F.state(activity=T.ThreadActivity.WAITING_ON_YOU, pending=F.need(prompt="Run the tests?"))
        self.assertEqual(status_act(state), act(S.THREAD_STATUS, thread="Fix the flaky test", status="waiting_on_you",
                                                need="command_approval", pending="Run the tests?",
                                                waiting_for="approval"))


class ModelArm(unittest.TestCase):
    MISS = "How is the clamping work coming along?"

    def seam(self):
        seam = F.FakeSeam(read=F.state(thread="t7", title="Upgrade the toolchain", as_of=160))
        seam.listing = listing(*THREADS)
        return seam

    def test_a_prefilter_miss_goes_to_the_model_and_its_pick_is_asked_back(self):
        seam, model = self.seam(), FakeModel("t5")
        t3 = tasks_over(self, seam, model)
        asked = t3.about("k3", ASK, self.MISS, 0)
        self.assertEqual((said(asked), asked.status), ([("confirm_thread", "Add a clamp helper")],
                                                       "needs_clarification"))
        self.assertEqual((model.calls, seam.ops("read")), (1, []))
        self.assertEqual(said(t3.answer("Yes.", 1)), [("thread_status", "Upgrade the toolchain", "working")])
        self.assertEqual(seam.ops("read"), [("read", "t5")])

    def test_a_no_asks_which_one_by_up_to_three_other_titles(self):
        seam = self.seam()
        t3 = tasks_over(self, seam, FakeModel("t5"))
        t3.about("k3", ASK, self.MISS, 0)
        which = t3.answer("No.", 1)
        self.assertEqual(said(which), [("ask_which", ["Fix the flaky ASR test", "Upgrade the toolchain",
                                                      "Write the release notes"])])
        self.assertEqual(seam.ops("read"), [])
        self.assertEqual(said(t3.answer("The toolchain one.", 2)),
                         [("thread_status", "Upgrade the toolchain", "working")])
        self.assertEqual(seam.ops("read"), [("read", "t7")])

    def test_a_no_that_names_another_thread_asks_that_one_back(self):
        t3 = tasks_over(self, self.seam(), FakeModel("t5"))
        t3.about("k3", ASK, self.MISS, 0)
        self.assertEqual(said(t3.answer("No, the toolchain one.", 1)), [("confirm_thread", "Upgrade the toolchain")])

    def test_a_lifecycle_event_is_saved_while_the_model_is_blocked(self):
        seam, model = self.seam(), BlockingModel()
        t3 = tasks_over(self, seam, model)
        t3.start("k1", "Add a clamp helper in the sandbox project", 0)
        t3.ack_news(t3.answer("yes", 1))
        running = next(iter(t3.follow.journal.tasks.values()))

        with ThreadPoolExecutor(max_workers=2) as workers:
            pick = workers.submit(t3.about, "k3", ASK, self.MISS, 2)
            try:
                self.assertTrue(model.entered.wait(2))
                seam.push(F.done(160))
                workers.submit(t3.poll, 3).result(timeout=2)
                saved = T3Journal(t3.follow.journal.path)
                self.assertEqual(saved.tasks[running.task_id].status, Status.COMPLETED)
                self.assertEqual(said(saved.pending_lines[0]), [("done", "completed")])
                self.assertFalse(pick.done())
            finally:
                model.release.set()
            self.assertEqual(said(pick.result(timeout=2)), [("confirm_thread", "Add a clamp helper")])

    def test_a_blocked_model_times_out_and_asks_with_the_prefilter_s_candidates(self):
        seam, model = self.seam(), BlockingModel()
        t3 = tasks_over(self, seam, model)
        with patch("voice.live.t3_tasks.PICK_TIMEOUT_S", 0.05), ThreadPoolExecutor() as workers:
            pick = workers.submit(t3.about, "k3", ASK, self.MISS, 0)
            try:
                self.assertTrue(model.entered.wait(2))
                asked = pick.result(timeout=2)
                self.assertEqual(said(asked), [("ask_which", [title for _, title in THREADS[:3]])])
                self.assertEqual(t3.question.candidates, ("t5", "t6", "t7"))
                self.assertEqual(seam.ops("read"), [])
                self.assertEqual(t3.about("k4", ASK, self.MISS, 1).acts, asked.acts)
                self.assertEqual(model.started, 1)
            finally:
                model.release.set()

    def test_a_model_failure_or_timeout_asks_with_the_prefilter_s_candidates(self):
        for error in (TimeoutError("timed out"), OSError("unreachable")):
            with self.subTest(error=type(error).__name__):
                seam = self.seam()
                t3 = tasks_over(self, seam, FakeModel(error=error))
                asked = t3.about("k3", ASK, self.MISS, 0)
                self.assertEqual(said(asked), [("ask_which", [title for _, title in THREADS[:3]])])
                self.assertEqual(t3.question.candidates, ("t5", "t6", "t7"))
                self.assertEqual(seam.ops("read"), [])


class Tell(unittest.TestCase):
    def test_a_follow_up_is_asked_back_and_a_yes_sends_it(self):
        seam = F.FakeSeam(read=F.state(thread="t5", title="Add a clamp helper", as_of=200))
        seam.listing = listing(*THREADS)
        seam.answer("send", F.sent(thread="t5", run="r2", sequence=190))
        t3 = tasks_over(self, seam)
        asked = t3.about("c1", TELL, "Tell the clamp helper thread to also add a test.", 0)
        self.assertEqual(said(asked), [("confirm_tell", "Add a clamp helper", "also add a test")])
        self.assertEqual(seam.ops("send"), [])
        sent = t3.answer("Yes, send it.", 1)
        self.assertEqual((said(sent), sent.sequences, sent.status),
                         ([("sent_followup", "Add a clamp helper")], (190,), "running"))
        self.assertEqual(seam.ops("send")[0][2:], ("t5", "Also add a test"))
        self.assertEqual(seam.ops("watch"), [("watch", "t5", 200, "r2")])

    def test_a_no_drops_the_follow_up(self):
        seam = busy_seam()
        t3 = tasks_over(self, seam)
        t3.about("c1", TELL, "Tell the clamp helper thread to also add a test.", 0)
        self.assertEqual(said(t3.answer("No, don't.", 1)), [("cancelled", "Add a clamp helper", "tell")])
        self.assertEqual(seam.ops("send"), [])

    def test_a_busy_thread_holds_the_follow_up_until_its_run_is_done(self):
        seam = busy_seam()
        t3 = held_follow_up(self, seam)
        task = next(t for t in t3.follow.journal.tasks.values() if t.kind is Kind.TELL)
        refused = seam.ops("send")[0][1]
        self.assertEqual((task.status, len(seam.ops("send"))), (Status.HELD, 1))
        seam.push(F.run_event(151, thread="t5"), F.reply(152, thread="t5"))
        t3.poll(2)
        self.assertEqual((t3.take(), len(seam.ops("send"))), ([], 1))

        seam.answer("send", F.sent(thread="t5", run="r2", sequence=170))
        seam.push(F.done(160, thread="t5"))
        t3.poll(3)
        self.assertEqual([c[1] for c in seam.ops("send")], [refused, task.link.request_id])
        self.assertNotEqual(refused, task.link.request_id)
        self.assertEqual(seam.ops("watch")[-1], ("watch", "t5", 150, "r2"))
        self.assertEqual([(u.task_id, said(u)) for u in t3.take()], [("c1", [("sent_followup", "Add a clamp helper")])])
        seam.push(F.done(180, thread="t5", run="r2", text="Added the test."))
        t3.poll(4)
        self.assertEqual([said(u) for u in t3.take()], [[("done", "Add a clamp helper", "completed")]])

    def test_a_held_follow_up_survives_a_restart(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "journal.json"
        held_follow_up(self, busy_seam(), path)
        saved = next(iter(T3Journal(path).held())).link.request_id
        seam = F.FakeSeam().answer("send", F.sent(thread="t5", run="r2"))
        restarted = tasks_over(self, seam, path=path)
        restarted.resume(0)
        self.assertTrue(restarted.holding())
        seam.push(F.done(160, thread="t5"))
        restarted.poll(1)
        self.assertEqual(seam.ops("send")[0][1:3], (saved, "t5"))
        self.assertFalse(restarted.holding())

    def test_mason_can_take_back_a_held_follow_up(self):
        cases = (("never mind", (), "dropped_followup"),
                 ("Don't pass that on.", ("What's the weather in Lisbon?",), "dropped_followup"),
                 ("never mind", ("What's the weather in Lisbon?",), None))
        for words, before, line in cases:
            with self.subTest(words=words, before=before):
                seam = busy_seam()
                t3 = held_follow_up(self, seam)
                for other in before:
                    self.assertIsNone(t3.answer(other, 2))
                dropped = t3.answer(words, 3)
                self.assertEqual(said(dropped)[0][0] if dropped else None, line)
                self.assertEqual(t3.holding(), line is None)
                seam.push(F.done(160, thread="t5"))
                t3.poll(4)
                self.assertEqual(len(seam.ops("send")), 1 if line else 2)


class Stop(unittest.TestCase):
    def seam(self):
        seam = F.FakeSeam()
        seam.listing = listing(*THREADS)
        return seam.answer("interrupt", F.requested(run="r1", sequence=95), T.NothingRunning())

    def test_stop_says_stopping_then_that_nothing_is_running(self):
        seam = self.seam()
        t3 = tasks_over(self, seam)
        asked = t3.about("s1", STOP, "Stop the clamp helper thread.", 0)
        self.assertEqual((said(asked), seam.ops("interrupt")), ([("confirm_stop", "Add a clamp helper")], []))
        first = t3.answer("Yes, stop it.", 1)
        self.assertEqual((said(first), first.sequences, first.thread_id),
                         ([("stopping", "Add a clamp helper")], (95,), "t5"))
        t3.about("s2", STOP, "Stop that thread.", 2)
        second = t3.answer("yes", 3)
        self.assertEqual((said(second), second.sequences), ([("nothing_running", "Add a clamp helper")], ()))
        ids = [c[1] for c in seam.ops("interrupt")]
        self.assertEqual((len(ids), len(set(ids)), {c[2] for c in seam.ops("interrupt")}), (2, 2, {"t5"}))

    def test_stopping_a_thread_drops_the_follow_up_held_for_it(self):
        seam = busy_seam().answer("interrupt", F.requested(run="r1", sequence=95))
        t3 = held_follow_up(self, seam)
        t3.about("s1", STOP, "Stop the clamp helper thread.", 2)
        self.assertEqual(said(t3.answer("yes", 3)), [("stopping", "Add a clamp helper"),
                                                     ("dropped_followup", "Add a clamp helper")])
        seam.push(F.done(160, thread="t5", status=T.TerminalStatus.INTERRUPTED))
        t3.poll(4)
        self.assertEqual((len(seam.ops("send")), t3.holding(), t3.take()), (1, False, []))


class CrossTool(unittest.TestCase):
    def test_stop_it_right_after_a_start_means_the_thread_just_started(self):
        seam = F.FakeSeam()
        t3 = tasks_over(self, seam)
        t3.start("a1", "Add a clamp helper in the sandbox project", 0)
        t3.answer("yes", 1)
        seam.listing = listing(("t9", "Fix the flaky ASR test"), ("t1", "Add a clamp helper"))
        self.assertEqual(said(t3.about("s1", STOP, "Stop it.", 2)), [("confirm_stop", "Add a clamp helper")])
        self.assertEqual(t3.question.candidates, ("t1",))
        self.assertEqual(said(t3.about("c1", TELL, "Tell it to also add a test.", 3)),
                         [("confirm_tell", "Add a clamp helper", "also add a test")])

    def test_it_with_no_thread_of_this_voice_asks_which(self):
        seam = F.FakeSeam()
        seam.listing = listing(*THREADS)
        update = tasks_over(self, seam).about("s1", STOP, "Stop it.", 0)
        self.assertEqual(said(update), [("ask_which", [title for _, title in THREADS[:3]])])


class ExplicitYes(unittest.TestCase):
    UNCLEAR = ("What do you mean?", "do you mean the other one", "hmm", "yes?", "sure, how long will it take",
               "could you stop it", "I said yes earlier", "maybe")

    def test_nothing_is_sent_or_stopped_without_an_explicit_yes(self):
        for op, words in ((TELL, "Tell the clamp helper thread to also add a test."),
                          (STOP, "Stop the clamp helper thread.")):
            for reply in self.UNCLEAR:
                with self.subTest(op=op, reply=reply):
                    seam = busy_seam().answer("interrupt", F.requested())
                    t3 = tasks_over(self, seam)
                    t3.about("k1", op, words, 0)
                    statuses = [getattr(t3.answer(reply, n), "status", None) for n in (1, 2)]
                    self.assertIn(statuses, (["needs_clarification", "cancelled"], ["cancelled", None]))
                    self.assertIsNone(t3.answer("yes", 3))
                    self.assertEqual((seam.ops("send"), seam.ops("interrupt")), ([], []))

    def test_a_tell_title_answer_requires_a_yes_before_sending(self):
        seam = self.tied_threads().answer("send", F.sent(thread="t6", run="r2"))
        t3 = tasks_over(self, seam)
        t3.about("k1", TELL, "Tell the flaky test thread to rerun.", 0)
        self.assertEqual(said(t3.answer("The ASR one.", 1)), [("confirm_tell", "Fix the flaky ASR test", "rerun")])
        self.assertEqual(seam.ops("send"), [])
        self.assertEqual(said(t3.answer("Yes, send it.", 2)), [("sent_followup", "Fix the flaky ASR test")])
        self.assertEqual([call[2:] for call in seam.ops("send")], [("t6", "Rerun")])

    def test_a_stop_title_answer_requires_a_yes_before_interrupting(self):
        seam = self.tied_threads().answer("interrupt", F.requested())
        t3 = tasks_over(self, seam)
        t3.about("k1", STOP, "Stop the flaky test thread.", 0)
        self.assertEqual(said(t3.answer("The ASR one.", 1)), [("confirm_stop", "Fix the flaky ASR test")])
        self.assertEqual(seam.ops("interrupt"), [])
        self.assertEqual(said(t3.answer("Yes, stop it.", 2)), [("stopping", "Fix the flaky ASR test")])
        self.assertEqual([call[2] for call in seam.ops("interrupt")], ["t6"])

    def tied_threads(self):
        seam = F.FakeSeam()
        seam.listing = listing(("t5", "Fix the flaky UI test"), ("t6", "Fix the flaky ASR test"))
        return seam


class Service(unittest.IsolatedAsyncioTestCase):
    def service(self, t3, records):
        segmenter = SimpleNamespace(records=lambda: records,
                                    dispatch_log=[{"task_id": r["task_id"], "instruction": r["instruction"]}
                                                  for r in records])
        return T3TaskService(segmenter, "conv_turn_001", 0.0, None, holds_side_effects=False, t3=t3)

    def record(self, task_id, instruction, tool=None):
        return {"task_id": task_id, "instruction": instruction, "status": "dispatched", "depends_on": [],
                "side_effect": tool in (START, TELL, STOP), "sources": ["orchestrator"], "history": [],
                "tool": {"tool": tool, "confidence": 0.9} if tool else None}

    def test_t3_records_go_to_t3_and_others_to_the_mock(self):
        seam = F.FakeSeam()
        seam.listing = listing(*THREADS)
        t3 = tasks_over(self, seam)
        records = [self.record("task_1", "Add a clamp helper in the sandbox project", START),
                   self.record("task_2", "What's the weather in Lisbon?"),
                   self.record("task_3", "Stop the toolchain thread.", STOP)]
        service = self.service(t3, records)
        service.sync(0)
        snapshot = service.snapshot("utt_1", "...")
        by_id = {t["task_id"]: t for t in snapshot["tasks"]}
        self.assertEqual(by_id["task_1"]["acts"], [act(S.CONFIRM_LAUNCH, project="Voice sandbox",
                                                       request="Add a clamp helper in the sandbox project").to_json()])
        self.assertEqual(by_id["task_1"]["evidence"], {"thread_id": None, "sequences": [1]})
        self.assertEqual(by_id["task_2"]["status"], "running")
        self.assertNotIn("acts", by_id["task_2"])
        self.assertEqual(by_id["task_3"]["acts"], [act(S.CONFIRM_STOP, thread="Upgrade the toolchain").to_json()])
        self.assertIs(service.open_question(), T3_WAITING)
        self.assertEqual(service.executions["task_1"].state.value, "completed")
        self.assertEqual((seam.ops("launch"), seam.ops("interrupt")), ([], []))

    def test_the_next_turn_answers_and_later_news_arrives_as_updates(self):
        seam = F.FakeSeam()
        t3 = tasks_over(self, seam)
        first = self.service(t3, [self.record("task_1", "Add a clamp helper in the sandbox project", START)])
        first.sync(0)
        first.record_delivery(first.snapshot("utt_1", "...")["tasks"][0], "sent")
        turn2 = self.service(t3, [])
        waiting = turn2.open_question()
        events = turn2.answer(waiting, "yes", 1)
        self.assertEqual(events[0]["event"], "completed")
        entry = next(t for t in turn2.snapshot("utt_1", "yes")["tasks"] if "acts" in t)
        self.assertEqual(([a["kind"] for a in entry["acts"]], entry["evidence"]["sequences"]), (["launched"], [93]))
        seam.push(F.done(160))
        t3.poll(2)
        turn3 = self.service(t3, [])
        updates = turn3.take_updates()
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0]["acts"], [act(S.DONE, status="completed").to_json()])
        self.assertEqual(updates[0]["evidence"], {"thread_id": "t1", "sequences": [160, 161]})
        self.assertEqual(updates[0]["task_id"], "conv_turn_001:task_1")
        self.assertEqual(turn3.update_entries(updates), updates)
        self.assertIsNone(task_note(updates[0]), "a T3 entry is worded by say_acts, never by a task note")

    async def test_unspoken_stop_confirmation_is_reasked_before_a_yes_can_act(self):
        seam = F.FakeSeam()
        seam.listing = listing(*THREADS)
        t3 = worded(tasks_over(self, seam), FakeText())
        first = self.service(t3, [self.record("task_1", "Stop the clamp helper thread.", STOP)])
        first.sync(0)
        entry = first.snapshot("utt_1", "...")["tasks"][0]
        voice = await FakeSession.open("ws://unused", "voice", lambda: 0)
        spoken = []

        def broken_tts(text, rate):
            raise OSError("TTS down")

        def speak(text, pcm, done):
            spoken.append(text)

        await say_acts(voice, 24_000, 0, entry, first, [], lambda *args, **kwargs: None, speak, broken_tts,
                       record_delivery=lambda state: first.record_delivery(entry, state))
        line = entry["spoken"]
        self.assertEqual((spoken, voice.sent), ([], []))
        first.close()

        retry = self.service(t3, [])
        retry.answer(retry.open_question(), "yes", 1)
        self.assertEqual(seam.ops("interrupt"), [])
        self.assertIsNotNone(t3.question)
        entry = retry.snapshot("utt_1", "yes")["tasks"][0]
        self.assertEqual(entry["acts"], [act(S.CONFIRM_STOP, thread="Add a clamp helper").to_json()])
        await say_acts(voice, 24_000, 0, entry, retry, [], lambda *args, **kwargs: None, speak,
                       lambda text, rate: bytes(960),
                       record_delivery=lambda state: retry.record_delivery(entry, state))
        self.assertEqual(spoken, [line])
        third = self.service(t3, [])
        third.answer(third.open_question(), "yes", 2)
        self.assertEqual(len(seam.ops("interrupt")), 1)

    def test_news_a_turn_never_spoke_is_taken_again_even_after_a_restart(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "journal.json"
        seam = F.FakeSeam()
        t3 = tasks_over(self, seam, path=path)
        t3.start("a1", "Add a clamp helper in the sandbox project", 0)
        t3.ack_news(t3.answer("yes", 1))
        seam.push(F.done(160))
        t3.poll(2)
        taken = t3.take()
        self.assertTrue(taken and not t3.has_news())
        t3.give_back(taken)
        self.assertTrue(t3.has_news())
        restarted = tasks_over(self, F.FakeSeam(), path=path)
        again = restarted.take()
        self.assertEqual([(u.acts, u.sequences) for u in again], [(u.acts, u.sequences) for u in taken])
        self.assertEqual(len(T3Journal(path).pending_lines), len(again), "given back once, saved once")

    def journaled(self, t3):
        return [[a.kind.value for a in u.acts] for u in T3Journal(t3.follow.journal.path).pending_lines]

    def test_a_reserved_line_survives_a_crash_before_its_audio_is_sent(self):
        seam = F.FakeSeam()
        t3 = tasks_over(self, seam)
        first = self.service(t3, [self.record("task_1", "Add a clamp helper in the sandbox project", START)])
        first.sync(0)
        first.record_delivery(first.snapshot("utt_1", "...")["tasks"][0], "sent")
        answer = self.service(t3, [])
        answer.answer(answer.open_question(), "yes", 1)
        self.assertEqual(self.journaled(t3), [["launched"]], "the write's own answer is reserved, not removed")
        entry = next(t for t in answer.snapshot("utt_1", "yes")["tasks"] if "acts" in t)
        answer.record_delivery(entry, "sent")
        self.assertEqual(self.journaled(t3), [], "marked delivered once its audio was sent")
        seam.push(F.done(160))
        t3.poll(2)
        turn = self.service(t3, [])
        update, = turn.take_updates()
        self.assertEqual(self.journaled(t3), [["done"]], "a crash here finds the line on restart")
        taken = t3.take()
        self.assertEqual((taken, self.journaled(t3)), ([], [["done"]]))
        turn.record_delivery(update, "sent")
        self.assertEqual(self.journaled(t3), [])

    def test_a_line_cut_off_part_way_is_said_again_once_then_waits_for_a_turn(self):
        seam = F.FakeSeam()
        t3 = tasks_over(self, seam)
        t3.start("a1", "Add a clamp helper in the sandbox project", 0)
        t3.ack_news(t3.answer("yes", 1))
        seam.push(F.done(160))
        t3.poll(2)
        turn = self.service(t3, [])
        update, = turn.take_updates()
        turn.record_delivery(update, "partial")
        turn.close()
        self.assertEqual(self.journaled(t3), [["done"]], "part of a line is not the line")
        self.assertTrue(t3.has_news(idle=True), "the next quiet moment says it again")
        again = t3.reserve_news(idle=True)
        self.assertEqual([[a.kind for a in u.acts] for u in again], [[S.DONE]])
        t3.retry_news(again)
        self.assertFalse(t3.has_news(idle=True), "said again once, then it waits for the person's turn")
        later = self.service(t3, [])
        self.assertEqual([u["acts"][0]["kind"] for u in later.take_updates()], ["done"])

    def test_a_held_follow_up_keeps_the_answer_path_open(self):
        t3 = held_follow_up(self, busy_seam())
        turn = self.service(t3, [])
        self.assertIs(turn.open_question(), T3_WAITING)
        self.assertEqual(turn.answer(T3_WAITING, "What's the weather in Lisbon?", 2), [])
        self.assertEqual(self.service(tasks_over(self, F.FakeSeam()), []).open_question(), None)

    def test_a_mid_utterance_t3_ask_starts_the_read_and_the_summary(self):
        seam = F.FakeSeam(read=F.state(thread="t5", title="Add a clamp helper", as_of=140,
                                       activity=T.ThreadActivity.FINISHED,
                                       last_reply=T.LastReply("Done. Tests pass.", "m1", None, "r1", "2026")))
        seam.listing = listing(*THREADS)
        text = FakeText()
        t3 = worded(tasks_over(self, seam), text)
        t3.follow.ensure(0)
        asking = {**self.record("task_1", "How is the clamp helper thread", ASK), "status": "pending"}
        service = T3TaskService(SimpleNamespace(records=lambda: [asking], dispatch_log=[]), "conv_turn_001", 0.0,
                                None, holds_side_effects=False, t3=t3)
        service.sync(0)
        self.assertTrue(text.summarised.wait(2), "the summary starts before the decider dispatches the ask")
        self.assertEqual(seam.ops("read"), [("read", "t5")])
        self.assertEqual(service.executions, {}, "nothing is dispatched or said yet")
        service.sync(1)
        self.assertEqual(text.calls, 1, "one speculative start per marked ask")

    def test_the_registry_offers_the_four_t3_tools_before_none(self):
        base = SimpleNamespace(choices=lambda: {"weather": "w", "agent": "a", "none": "n"}, tool_threshold=0.6)
        registry = T3Registry(base)
        self.assertEqual(list(registry.choices()),
                         ["weather", "agent", "t3_start", "t3_ask", "t3_tell", "t3_stop", "none"])
        self.assertEqual(registry.tool_threshold, 0.6)
        self.assertEqual(set(T3_TOOLS), tool_registry.T3_TOOLS, "the gate and the kind hint name these tools")
        self.assertEqual(PLAN_A.confirmed_tools, tool_registry.T3_TOOLS)

    def test_the_transcript_names_each_spoken_line_its_evidence_and_its_record(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        records = Path(tmp.name)
        start = {"clip": "turn_001", "final_transcript": "Start a thread in the sandbox.",
                 "snapshot_after_tools": {"tasks": [{"task_id": "task_1", "spoken": "Start it in Voice sandbox?",
                                                     "acts": [act(S.CONFIRM_LAUNCH, project="Voice sandbox").to_json()],
                                                     "evidence": {"thread_id": None, "sequences": [681]}}]},
                 "replies": [{"text": "Should I start it in Voice sandbox?"}]}
        news = {"clip": "turn_002", "idle": True, "final_transcript": None,
                "task_updates": [{"spoken": "It finished.", "row": "done-event",
                                  "acts": [act(S.DONE, status="completed").to_json()],
                                  "evidence": {"thread_id": "t1", "sequences": [750, 751]}}],
                "replies": [{"text": "It finished."}]}
        for record in (start, news):
            (records / f"{record['clip']}.json").write_text(json.dumps(record), encoding="utf-8")
        rows = transcript_rows(records)
        self.assertEqual([r["record"] for r in rows], ["turn_001.json", "turn_002.json"])
        self.assertEqual(rows[0]["t3"], [{"line": "Start it in Voice sandbox?", "acts": ["confirm_launch"],
                                          "thread_id": None, "sequences": [681]}])
        self.assertEqual((rows[1]["idle"], rows[1]["t3"][0]["sequences"], rows[1]["said"]),
                         (True, [750, 751], ["It finished."]))


class WholeDialogue(unittest.TestCase):
    def test_every_spoken_line_is_the_text_model_s_wording_of_its_acts(self):
        seam = F.FakeSeam(read=F.state(thread="t1", title="Add a clamp helper", as_of=210,
                                       activity=T.ThreadActivity.FINISHED,
                                       last_reply=T.LastReply("Added clamp. Tests pass.", "m9", None, "r1", "2026")))
        seam.listing = listing(("t1", "Add a clamp helper"), ("t6", "Fix the flaky ASR test"))
        t3 = worded(tasks_over(self, seam), FakeText())
        script = Script(t3)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            script.tool("a1", START, "Add a clamp helper in the sandbox project")
            script.reply("a1", "Yes, start it.")
            seam.push(F.run_event(100), F.reply(101, text="Working on clamp."), F.needs_you(102),
                      F.resolved(103), F.done(200))
            t3.poll(script.clock())
            for update in t3.take():
                script.show("watch", None, update)
            script.tool("b1", ASK, "How is the clamp helper thread going?")
            script.reply("b1", "yes")
            seam.answer("send", F.sent(thread="t1"))
            script.tool("c1", TELL, "Tell the clamp helper thread to also add a test.")
            script.reply("c1", "Yes, send it.")
            seam.answer("interrupt", F.requested())
            script.tool("s1", STOP, "Stop the clamp helper thread.")
            script.reply("s1", "Yes, stop it.")
        rows = [json.loads(line) for line in out.getvalue().splitlines()]
        spoken = [row for row in rows if row.get("acts")]
        self.assertEqual([row for row in spoken if row["line"] is None], [], "nothing waits")
        self.assertEqual([a["kind"] for row in spoken for a in row["acts"]],
                         ["confirm_launch", "launched", "run_update", "replied", "needs_you", "need_resolved",
                          "done", "confirm_thread", "thread_status", "confirm_tell", "sent_followup", "confirm_stop",
                          "stopping"])
        for row in spoken:
            acts = [Act.from_json(a) for a in row["acts"]]
            with self.subTest(acts=[a.kind.value for a in acts]):
                self.assertEqual(row["line"], FakeText.wording(kept_exact(acts),
                                                               any(a.kind in QUESTIONS for a in acts)))
                self.assertEqual(check(row["line"], acts), [])
        done = next(row for row in spoken if row["acts"][0]["kind"] == "done")
        self.assertEqual((done["acts"][0]["slots"]["summary"], done["evidence"]["sequences"]),
                         (FakeText.GIST, [200, 201, 210]), "the done carries its summary and the read it came from")


class ReplyWords(unittest.TestCase):
    def test_reply_kind(self):
        cases = {
            "Yes, start it.": "yes",
            "yeah": "yes",
            "yep!": "yes",
            "sure, go ahead": "yes",
            "yes please": "yes",
            "please": "yes",
            "do it": "yes",
            "start it": "yes",
            "go ahead": "yes",
            "yes, that one": "yes",
            "No, don't.": "no",
            "wait": "no",
            "never mind": "no",
            "not that one": "no",
            "please don't": "no",
            "yes, but don’t start it": "no",
            "stop it": "no",
            "What do you mean?": None,
            "do you mean the other one": None,
            "yes?": None,
            "sure, how long will it take": None,
            "do": None,
            "could you start it": None,
            "please tell me more": None,
            "I said yes earlier": None,
            "yes, maybe later": None,
            "yes, #2": None,
            "start it in the other project": None,
            "send it": None,
        }
        for text, kind in cases.items():
            with self.subTest(text=text):
                self.assertEqual(reply_kind(text), kind)

    def test_reply_kind_reads_the_op_s_own_words(self):
        cases = {(STOP, "Yes, stop it."): "yes", (STOP, "stop"): "yes", (STOP, "don't stop it"): "no",
                 (STOP, "start it"): None, (TELL, "yes, send it"): "yes", (TELL, "tell it"): "yes",
                 (TELL, "stop"): "no", (ASK, "yes"): "yes", (ASK, "stop it"): "no"}
        for (op, text), kind in cases.items():
            with self.subTest(op=op, text=text):
                self.assertEqual(reply_kind(text, op), kind)

    def test_tell_parts(self):
        cases = {
            "Tell it to also add a test.": ("it", "also add a test", "to"),
            "tell the clamp helper thread to also add a test": ("the clamp helper thread", "also add a test", "to"),
            "Okay, can you ask the PR thread to rebase on main?": ("the PR thread", "rebase on main", "to"),
            "Let it know that the tests are flaky.": ("it", "the tests are flaky", "that"),
            "Also add a test to the clamp thread.": ("Also add a test to the clamp thread.",
                                                     "Also add a test to the clamp thread.", None),
        }
        for text, parts in cases.items():
            with self.subTest(text=text):
                self.assertEqual(tell_parts(text), parts)


if __name__ == "__main__":
    unittest.main()
