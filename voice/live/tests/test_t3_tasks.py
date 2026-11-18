"""The T3 task service: Twin A asks before it launches, Twin B reads, and every line carries its evidence."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from voice.live.t3_tasks import START, T3_WAITING, T3Registry, T3TaskService, T3Tasks, reply_kind, speak_state
from voice.prototype.run import task_note
from voice.t3 import fakes as F
from voice.t3 import lifecycle as L
from voice.t3 import types as T
from voice.t3.journal import T3Journal

TWO_PROJECTS = (("p1", "Voice sandbox", ("sandbox", "voice sandbox")),
                ("p2", "Voice server", ("voice server", "the server")))


def tasks_over(test: unittest.TestCase, seam: F.FakeSeam) -> T3Tasks:
    tmp = tempfile.TemporaryDirectory()
    test.addCleanup(tmp.cleanup)
    return T3Tasks(L.Follow(T3Journal(Path(tmp.name) / "journal.json"), F.Connector(seam)))


def listing(*rows, truncated=False, as_of=77):
    threads = tuple(T.ThreadSummary(tid, "p1", title, T.ThreadActivity.WORKING, "2026-11-17T10:00:00Z", False)
                    for tid, title in rows)
    return T.ThreadList(threads, len(threads) + (5 if truncated else 0), truncated, as_of)


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
    def test_a_lone_pick_is_read_and_spoken_with_the_read_sequence(self):
        seam = F.FakeSeam(read=F.state(thread="t5", title="Add a clamp helper", as_of=140,
                                       activity=T.ThreadActivity.FINISHED,
                                       last_reply=T.LastReply("Done. I added clamp. Tests pass.", "m1", None,
                                                              "r1", "2026-11-17T10:00:00Z")))
        seam.listing = listing(("t5", "Add a clamp helper"), ("t6", "Fix the flaky ASR test"))
        update = tasks_over(self, seam).ask_about("k2", "How is the clamp helper thread going?", 0)
        self.assertEqual(update.line, "Add a clamp helper finished. Its last reply said: Done. I added clamp.")
        self.assertEqual((update.sequences, update.status), ((140,), "completed"))
        self.assertEqual(seam.ops("watch"), [])

    def test_none_depends_on_truncation(self):
        for truncated, line in ((False, "I don't see an active thread like that."),
                                (True, "I don't see it among your 1 most recent threads.")):
            seam = F.FakeSeam()
            seam.listing = listing(("t6", "Fix the flaky ASR test"), truncated=truncated)
            update = tasks_over(self, seam).ask_about("k2", "How is the billing thread going?", 0)
            self.assertEqual((update.line, update.sequences), (line, (77,)))

    def test_a_tie_asks_and_the_answer_reads(self):
        seam = F.FakeSeam(read=F.state(thread="t6", title="Fix the flaky ASR test", as_of=150))
        seam.listing = listing(("t5", "Fix the flaky UI test"), ("t6", "Fix the flaky ASR test"))
        t3 = tasks_over(self, seam)
        asked = t3.ask_about("k2", "How is the flaky test thread going?", 0)
        self.assertEqual(asked.status, "needs_clarification")
        self.assertIn("Fix the flaky ASR test", asked.line)
        answered = t3.answer("The ASR one.", 1)
        self.assertEqual((answered.line, answered.sequences), ("Fix the flaky ASR test is still working.", (150,)))

    def test_speak_state_names_a_pending_need(self):
        state = F.state(activity=T.ThreadActivity.WAITING_ON_YOU, pending=F.need(prompt="Run the tests?"))
        self.assertEqual(speak_state(state), "Fix the flaky test is waiting for you: Run the tests?")


class Service(unittest.TestCase):
    def service(self, t3, records):
        segmenter = SimpleNamespace(records=lambda: records,
                                    dispatch_log=[{"task_id": r["task_id"], "instruction": r["instruction"]}
                                                  for r in records])
        return T3TaskService(segmenter, "conv_turn_001", 0.0, None, holds_side_effects=False, t3=t3)

    def record(self, task_id, instruction, tool=None):
        return {"task_id": task_id, "instruction": instruction, "status": "dispatched", "depends_on": [],
                "side_effect": tool == START, "sources": ["orchestrator"], "history": [],
                "tool": {"tool": tool, "confidence": 0.9} if tool else None}

    def test_t3_records_go_to_t3_and_others_to_the_mock(self):
        seam = F.FakeSeam()
        t3 = tasks_over(self, seam)
        records = [self.record("task_1", "Add a clamp helper in the sandbox project", START),
                   self.record("task_2", "What's the weather in Lisbon?")]
        service = self.service(t3, records)
        service.sync(0)
        snapshot = service.snapshot("utt_1", "...")
        by_id = {t["task_id"]: t for t in snapshot["tasks"]}
        self.assertEqual(by_id["task_1"]["spoken"], "Start it in Voice sandbox?")
        self.assertEqual(by_id["task_1"]["evidence"], {"thread_id": None, "sequences": [1]})
        self.assertEqual(by_id["task_2"]["status"], "running")
        self.assertNotIn("spoken", by_id["task_2"])
        self.assertIs(service.open_question(), T3_WAITING)
        self.assertEqual(service.executions["task_1"].state.value, "completed")
        self.assertEqual(seam.ops("launch"), [])

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

    def test_without_an_open_question_the_answer_path_is_the_mock_s(self):
        t3 = tasks_over(self, F.FakeSeam())
        self.assertIsNone(self.service(t3, []).open_question())

    def test_the_registry_offers_both_t3_tools_before_none(self):
        base = SimpleNamespace(choices=lambda: {"weather": "w", "agent": "a", "none": "n"}, tool_threshold=0.6)
        registry = T3Registry(base)
        self.assertEqual(list(registry.choices()), ["weather", "agent", "t3_start", "t3_ask", "none"])
        self.assertEqual(registry.tool_threshold, 0.6)


class Replies(unittest.TestCase):
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
            "No, don't.": "no",
            "wait": "no",
            "never mind": "no",
            "not that one": "no",
            "please don't": "no",
            "yes, but don’t start it": "no",
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
        }
        for text, kind in cases.items():
            with self.subTest(text=text):
                self.assertEqual(reply_kind(text), kind)


if __name__ == "__main__":
    unittest.main()
