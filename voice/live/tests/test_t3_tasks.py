"""The T3 task service confirms writes, resolves reads, and attaches evidence to thread updates."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from voice.live.t3_tasks import (ASK, START, STOP, T3_WAITING, TELL, T3Registry, T3TaskService, T3Tasks, reply_kind,
                                 speak_state, tell_parts)
from voice.prototype.run import task_note
from voice.t3 import fakes as F
from voice.t3 import lifecycle as L
from voice.t3 import types as T
from voice.t3.journal import Kind, Status, T3Journal
from voice.t3.pick.pick import question_for

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


def listing(*rows, truncated=False, as_of=77):
    threads = tuple(T.ThreadSummary(tid, "p1", title, T.ThreadActivity.WORKING, "2026-11-17T10:00:00Z", False)
                    for tid, title in rows)
    return T.ThreadList(threads, len(threads) + (5 if truncated else 0), truncated, as_of)


class FakeModel:
    """The model arm's client: one scripted answer, or an error, per call."""

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
    test.assertEqual(t3.answer("Yes, send it.", 1).line, L.HELD_LINE)
    return t3


class TwinA(unittest.TestCase):
    def test_a_lone_match_is_asked_back_before_any_launch(self):
        seam = F.FakeSeam()
        t3 = tasks_over(self, seam)
        asked = t3.start("k1", "Add a clamp helper in the sandbox project", 0)
        self.assertEqual((asked.line, asked.status), ("Start it in Voice sandbox?", "needs_clarification"))
        self.assertEqual(asked.sequences, (1,))
        self.assertEqual(seam.ops("launch"), [])
        started = t3.answer("Yes, go ahead.", 1)
        self.assertEqual(started.line, "Started a thread in Voice sandbox.")
        self.assertEqual((started.status, started.sequences, started.thread_id), ("running", (93,), "t1"))
        self.assertEqual(seam.ops("launch")[0][2:], ("p1", "Add a clamp helper in the sandbox project"))
        self.assertEqual(seam.ops("watch"), [("watch", "t1", 93, "r1")])
        self.assertEqual(t3.take(), [])

    def test_no_starts_nothing(self):
        seam = F.FakeSeam()
        t3 = tasks_over(self, seam)
        t3.start("k1", "Add a clamp helper in the sandbox project", 0)
        self.assertEqual(t3.answer("No, don't.", 1).line, "Okay, I won't start it.")
        self.assertEqual(seam.ops("launch"), [])
        self.assertIsNone(t3.question)

    def test_an_unclear_reply_reasks_once_then_drops_the_request(self):
        seam = F.FakeSeam()
        t3 = tasks_over(self, seam)
        t3.start("k1", "Add a clamp helper in the sandbox project", 0)

        again = t3.answer("What do you mean?", 1)
        self.assertEqual((again.line, again.status), ("Start it in Voice sandbox?", "needs_clarification"))
        self.assertEqual(seam.ops("launch"), [])
        dropped = t3.answer("I'm thinking about it.", 2)
        self.assertEqual((dropped.line, dropped.status), ("Okay, I won't start it.", "cancelled"))
        self.assertIsNone(t3.answer("yes", 3))
        self.assertEqual(seam.ops("launch"), [])

    def test_a_confirmation_can_be_retargeted_but_still_needs_a_yes(self):
        for reply in ("start it in the other project", "yes, start it in the voice server"):
            with self.subTest(reply=reply):
                seam = F.FakeSeam(projects=TWO_PROJECTS)
                t3 = tasks_over(self, seam)
                t3.start("k1", "Add a clamp helper in the sandbox project", 0)

                again = t3.answer(reply, 1)
                self.assertEqual((again.line, again.status), ("Start it in Voice server?", "needs_clarification"))
                self.assertEqual(seam.ops("launch"), [])
                self.assertEqual(t3.answer("yes please", 2).line, "Started a thread in Voice server.")
                self.assertEqual(seam.ops("launch")[0][2], "p2")

    def test_a_project_named_in_the_answer_is_still_confirmed(self):
        seam = F.FakeSeam(projects=TWO_PROJECTS)
        t3 = tasks_over(self, seam)
        asked = t3.start("k1", "Start a thread that fixes the flaky test", 0)
        self.assertEqual(asked.line, "Which project do you mean?")
        again = t3.answer("The voice server.", 1)
        self.assertEqual(again.line, "Start it in Voice server?")
        self.assertEqual(seam.ops("launch"), [])
        self.assertEqual(t3.answer("yes", 2).line, "Started a thread in Voice server.")
        self.assertEqual(seam.ops("launch")[0][2], "p2")

    def test_a_refused_launch_says_why(self):
        seam = F.FakeSeam().answer("launch", F.refusal("refused", "T3 refused it.", reason="Model is off."))
        t3 = tasks_over(self, seam)
        t3.start("k1", "Add a clamp helper in the sandbox project", 0)
        failed = t3.answer("yes", 1)
        self.assertEqual((failed.line, failed.status), ("T3 refused to start it: Model is off.", "failed"))

    def test_a_dead_seam_says_so_and_launches_nothing(self):
        t3 = tasks_over(self, F.FakeSeam())
        t3.follow = L.Follow(t3.follow.journal, F.Connector(T.UPGRADE_404))
        update = t3.start("k1", "Add a clamp helper in the sandbox project", 0)
        self.assertEqual((update.line, update.status), (L.ROWS["upgrade-404"].line, "failed"))




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
        self.assertEqual((asked.line, asked.sequences), ("Do you mean the Add a clamp helper thread?", (77,)))
        self.assertEqual((seam.ops("read"), model.calls), ([], 0))
        update = t3.answer("yes", 1)
        self.assertEqual(update.line, "Add a clamp helper finished. Its last reply said: Done. I added clamp.")
        self.assertEqual((update.sequences, update.status), ((140,), "completed"))
        self.assertEqual(seam.ops("watch"), [])

    def test_none_depends_on_truncation(self):
        for truncated, line in ((False, "I don't see an active thread like that."),
                                (True, "I don't see it among your 1 most recent threads.")):
            seam = F.FakeSeam()
            seam.listing = listing(("t6", "Fix the flaky ASR test"), truncated=truncated)
            update = tasks_over(self, seam).about("k2", ASK, "How is the billing thread going?", 0)
            self.assertEqual((update.line, update.sequences), (line, (77,)))

    def test_a_tie_asks_and_the_answer_reads(self):
        seam = F.FakeSeam(read=F.state(thread="t6", title="Fix the flaky ASR test", as_of=150))
        seam.listing = listing(("t5", "Fix the flaky UI test"), ("t6", "Fix the flaky ASR test"))
        t3 = tasks_over(self, seam, FakeModel("t5"))
        asked = t3.about("k2", ASK, "How is the flaky test thread going?", 0)
        self.assertEqual(asked.status, "needs_clarification")
        self.assertIn("Fix the flaky ASR test", asked.line)
        answered = t3.answer("The ASR one.", 1)
        self.assertEqual((answered.line, answered.sequences), ("Fix the flaky ASR test is still working.", (150,)))
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
                self.assertEqual((again.line, again.status), (asked.line, "needs_clarification"))
                self.assertEqual(seam.ops("read"), [])
                self.assertEqual(t3.answer("The ASR one.", 2).thread_id, "t6")

    def test_speak_state_names_a_pending_need(self):
        state = F.state(activity=T.ThreadActivity.WAITING_ON_YOU, pending=F.need(prompt="Run the tests?"))
        self.assertEqual(speak_state(state), "Fix the flaky test is waiting for you: Run the tests?")


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
        self.assertEqual((asked.line, asked.status), ("Do you mean the Add a clamp helper thread?",
                                                      "needs_clarification"))
        self.assertEqual((model.calls, seam.ops("read")), (1, []))
        self.assertEqual(t3.answer("Yes.", 1).line, "Upgrade the toolchain is still working.")
        self.assertEqual(seam.ops("read"), [("read", "t5")])

    def test_a_no_asks_which_one_by_up_to_three_other_titles(self):
        seam = self.seam()
        t3 = tasks_over(self, seam, FakeModel("t5"))
        t3.about("k3", ASK, self.MISS, 0)
        which = t3.answer("No.", 1)
        self.assertEqual(which.line, question_for(["Fix the flaky ASR test", "Upgrade the toolchain",
                                                   "Write the release notes"]))
        self.assertEqual(seam.ops("read"), [])
        self.assertEqual(t3.answer("The toolchain one.", 2).line, "Upgrade the toolchain is still working.")
        self.assertEqual(seam.ops("read"), [("read", "t7")])

    def test_a_no_that_names_another_thread_asks_that_one_back(self):
        t3 = tasks_over(self, self.seam(), FakeModel("t5"))
        t3.about("k3", ASK, self.MISS, 0)
        self.assertEqual(t3.answer("No, the toolchain one.", 1).line, "Do you mean the Upgrade the toolchain thread?")

    def test_a_lifecycle_event_is_saved_while_the_model_is_blocked(self):
        seam, model = self.seam(), BlockingModel()
        t3 = tasks_over(self, seam, model)
        t3.start("k1", "Add a clamp helper in the sandbox project", 0)
        t3.answer("yes", 1)
        running = next(iter(t3.follow.journal.tasks.values()))

        with ThreadPoolExecutor(max_workers=2) as workers:
            pick = workers.submit(t3.about, "k3", ASK, self.MISS, 2)
            try:
                self.assertTrue(model.entered.wait(2))
                seam.push(F.done(160))
                workers.submit(t3.poll, 3).result(timeout=2)
                saved = T3Journal(t3.follow.journal.path)
                self.assertEqual(saved.tasks[running.task_id].status, Status.COMPLETED)
                self.assertEqual(saved.pending_lines[0].line, "It finished. It said: I added clamp. The tests pass.")
                self.assertFalse(pick.done())
            finally:
                model.release.set()
            self.assertEqual(pick.result(timeout=2).line, "Do you mean the Add a clamp helper thread?")

    def test_a_blocked_model_times_out_and_asks_with_the_prefilter_s_candidates(self):
        seam, model = self.seam(), BlockingModel()
        t3 = tasks_over(self, seam, model)
        with patch("voice.live.t3_tasks.PICK_TIMEOUT_S", 0.05), ThreadPoolExecutor() as workers:
            pick = workers.submit(t3.about, "k3", ASK, self.MISS, 0)
            try:
                self.assertTrue(model.entered.wait(2))
                asked = pick.result(timeout=2)
                self.assertEqual(asked.line, question_for([title for _, title in THREADS[:3]]))
                self.assertEqual(t3.question.candidates, ("t5", "t6", "t7"))
                self.assertEqual(seam.ops("read"), [])
                self.assertEqual(t3.about("k4", ASK, self.MISS, 1).line, asked.line)
                self.assertEqual(model.started, 1)
            finally:
                model.release.set()

    def test_a_model_failure_or_timeout_asks_with_the_prefilter_s_candidates(self):
        for error in (TimeoutError("timed out"), OSError("unreachable")):
            with self.subTest(error=type(error).__name__):
                seam = self.seam()
                t3 = tasks_over(self, seam, FakeModel(error=error))
                asked = t3.about("k3", ASK, self.MISS, 0)
                self.assertEqual(asked.line, question_for([title for _, title in THREADS[:3]]))
                self.assertEqual(t3.question.candidates, ("t5", "t6", "t7"))
                self.assertEqual(seam.ops("read"), [])


class Tell(unittest.TestCase):
    def test_a_follow_up_is_asked_back_and_a_yes_sends_it(self):
        seam = F.FakeSeam(read=F.state(thread="t5", title="Add a clamp helper", as_of=200))
        seam.listing = listing(*THREADS)
        seam.answer("send", F.sent(thread="t5", run="r2", sequence=190))
        t3 = tasks_over(self, seam)
        asked = t3.about("c1", TELL, "Tell the clamp helper thread to also add a test.", 0)
        self.assertEqual(asked.line, "Tell the Add a clamp helper thread to also add a test?")
        self.assertEqual(seam.ops("send"), [])
        sent = t3.answer("Yes, send it.", 1)
        self.assertEqual((sent.line, sent.sequences, sent.status), ("I've passed that on.", (190,), "running"))
        self.assertEqual(seam.ops("send")[0][2:], ("t5", "Also add a test"))
        self.assertEqual(seam.ops("watch"), [("watch", "t5", 200, "r2")])

    def test_a_no_drops_the_follow_up(self):
        seam = busy_seam()
        t3 = tasks_over(self, seam)
        t3.about("c1", TELL, "Tell the clamp helper thread to also add a test.", 0)
        self.assertEqual(t3.answer("No, don't.", 1).line, L.DROPPED_LINE)
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
        self.assertEqual([(u.task_id, u.line) for u in t3.take()], [("c1", "I've passed that on.")])
        seam.push(F.done(180, thread="t5", run="r2", text="Added the test."))
        t3.poll(4)
        self.assertEqual([u.line for u in t3.take()], ["It finished. It said: Added the test."])

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
        cases = (("never mind", (), L.DROPPED_LINE),
                 ("Don't pass that on.", ("What's the weather in Lisbon?",), L.DROPPED_LINE),
                 ("never mind", ("What's the weather in Lisbon?",), None))
        for words, before, line in cases:
            with self.subTest(words=words, before=before):
                seam = busy_seam()
                t3 = held_follow_up(self, seam)
                for other in before:
                    self.assertIsNone(t3.answer(other, 2))
                dropped = t3.answer(words, 3)
                self.assertEqual(dropped.line if dropped else None, line)
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
        self.assertEqual((asked.line, seam.ops("interrupt")), ("Stop the Add a clamp helper thread?", []))
        first = t3.answer("Yes, stop it.", 1)
        self.assertEqual((first.line, first.sequences, first.thread_id), ("Stopping it.", (95,), "t5"))
        t3.about("s2", STOP, "Stop that thread.", 2)
        second = t3.answer("yes", 3)
        self.assertEqual((second.line, second.sequences), ("It isn't running anything right now.", ()))
        ids = [c[1] for c in seam.ops("interrupt")]
        self.assertEqual((len(ids), len(set(ids)), {c[2] for c in seam.ops("interrupt")}), (2, 2, {"t5"}))

    def test_stopping_a_thread_drops_the_follow_up_held_for_it(self):
        seam = busy_seam().answer("interrupt", F.requested(run="r1", sequence=95))
        t3 = held_follow_up(self, seam)
        t3.about("s1", STOP, "Stop the clamp helper thread.", 2)
        self.assertEqual(t3.answer("yes", 3).line, "Stopping it. I've dropped the message I was holding for it.")
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
        self.assertEqual(t3.about("s1", STOP, "Stop it.", 2).line, "Stop the Add a clamp helper thread?")
        self.assertEqual(t3.question.candidates, ("t1",))
        self.assertEqual(t3.about("c1", TELL, "Tell it to also add a test.", 3).line,
                         "Tell the Add a clamp helper thread to also add a test?")

    def test_it_with_no_thread_of_this_voice_asks_which(self):
        seam = F.FakeSeam()
        seam.listing = listing(*THREADS)
        update = tasks_over(self, seam).about("s1", STOP, "Stop it.", 0)
        self.assertEqual(update.line, question_for([title for _, title in THREADS[:3]]))


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
        self.assertEqual(t3.answer("The ASR one.", 1).line, "Tell the Fix the flaky ASR test thread to rerun?")
        self.assertEqual(seam.ops("send"), [])
        self.assertEqual(t3.answer("Yes, send it.", 2).line, "I've passed that on.")
        self.assertEqual([call[2:] for call in seam.ops("send")], [("t6", "Rerun")])

    def test_a_stop_title_answer_requires_a_yes_before_interrupting(self):
        seam = self.tied_threads().answer("interrupt", F.requested())
        t3 = tasks_over(self, seam)
        t3.about("k1", STOP, "Stop the flaky test thread.", 0)
        self.assertEqual(t3.answer("The ASR one.", 1).line, "Stop the Fix the flaky ASR test thread?")
        self.assertEqual(seam.ops("interrupt"), [])
        self.assertEqual(t3.answer("Yes, stop it.", 2).line, "Stopping it.")
        self.assertEqual([call[2] for call in seam.ops("interrupt")], ["t6"])

    def tied_threads(self):
        seam = F.FakeSeam()
        seam.listing = listing(("t5", "Fix the flaky UI test"), ("t6", "Fix the flaky ASR test"))
        return seam


class Service(unittest.TestCase):
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
        self.assertEqual(by_id["task_1"]["spoken"], "Start it in Voice sandbox?")
        self.assertEqual(by_id["task_1"]["evidence"], {"thread_id": None, "sequences": [1]})
        self.assertEqual(by_id["task_2"]["status"], "running")
        self.assertNotIn("spoken", by_id["task_2"])
        self.assertEqual(by_id["task_3"]["spoken"], "Stop the Upgrade the toolchain thread?")
        self.assertIs(service.open_question(), T3_WAITING)
        self.assertEqual(service.executions["task_1"].state.value, "completed")
        self.assertEqual((seam.ops("launch"), seam.ops("interrupt")), ([], []))

    def test_the_next_turn_answers_and_later_news_arrives_as_updates(self):
        seam = F.FakeSeam()
        t3 = tasks_over(self, seam)
        self.service(t3, [self.record("task_1", "Add a clamp helper in the sandbox project", START)]).sync(0)
        turn2 = self.service(t3, [])
        waiting = turn2.open_question()
        events = turn2.answer(waiting, "yes", 1)
        self.assertEqual(events[0]["event"], "completed")
        entry = next(t for t in turn2.snapshot("utt_1", "yes")["tasks"] if "spoken" in t)
        self.assertEqual((entry["spoken"], entry["evidence"]["sequences"]), ("Started a thread in Voice sandbox.", [93]))
        seam.push(F.done(160))
        t3.poll(2)
        turn3 = self.service(t3, [])
        updates = turn3.take_updates()
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0]["spoken"], "It finished. It said: I added clamp. The tests pass.")
        self.assertEqual(updates[0]["evidence"], {"thread_id": "t1", "sequences": [160, 161]})
        self.assertEqual(updates[0]["task_id"], "conv_turn_001:task_1")
        self.assertEqual(turn3.update_entries(updates), updates)
        self.assertIn("It finished. It said: I added clamp.", task_note(updates[0]))

    def test_a_held_follow_up_keeps_the_answer_path_open(self):
        t3 = held_follow_up(self, busy_seam())
        turn = self.service(t3, [])
        self.assertIs(turn.open_question(), T3_WAITING)
        self.assertEqual(turn.answer(T3_WAITING, "What's the weather in Lisbon?", 2), [])
        self.assertEqual(self.service(tasks_over(self, F.FakeSeam()), []).open_question(), None)

    def test_the_registry_offers_the_four_t3_tools_before_none(self):
        base = SimpleNamespace(choices=lambda: {"weather": "w", "agent": "a", "none": "n"}, tool_threshold=0.6)
        registry = T3Registry(base)
        self.assertEqual(list(registry.choices()),
                         ["weather", "agent", "t3_start", "t3_ask", "t3_tell", "t3_stop", "none"])
        self.assertEqual(registry.tool_threshold, 0.6)


class Words(unittest.TestCase):
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
