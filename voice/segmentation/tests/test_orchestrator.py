import unittest

from voice.segmentation.contract import (Correction, CorrectionMode, Decisions, Kind, Readiness, SegmentDecision,
                             TranscriptUpdate)
from voice.segmentation.spans import clause_segments
from voice.segmentation.tasks import IllegalTransition, Orchestrator, Status


def decide(sid, kind="request", ready="dispatch_now", depends=None, corrects=None, side_effect=False):
    correction = None
    if corrects:
        mode, _, target = corrects.partition("_")
        correction = Correction(target, CorrectionMode(mode))
    return SegmentDecision(sid, Kind(kind), Readiness(ready), depends, correction, side_effect)


def step(orch, revision, text, final=False, delegates=None, **by_sid):
    update = TranscriptUpdate(revision, text, final)
    segments = clause_segments(text, final)
    orch.apply(update, segments, Decisions({sid: decide(sid, **kw) for sid, kw in by_sid.items()},
                                           delegates or {}, []))
    return {t.segment_text: t for t in orch.tasks.values()}


def statuses(orch):
    return [(t.segment_text, t.status) for t in orch.tasks.values()]


class DispatchPolicy(unittest.TestCase):
    def test_trailing_segment_waits_for_more_speech(self):
        orch = Orchestrator()
        step(orch, 1, "look up the weather in denver", s1={})
        self.assertEqual(statuses(orch), [("look up the weather in denver", Status.PROPOSED)])
        self.assertEqual(orch.dispatch_log, [])

    def test_closed_segment_dispatches_before_the_utterance_ends(self):
        orch = Orchestrator()
        step(orch, 1, "look up the weather in denver and find", s1={}, s2={})
        self.assertEqual([d["instruction"] for d in orch.dispatch_log], ["look up the weather in denver"])
        self.assertFalse(orch.dispatch_log[0]["is_final"])

    def test_side_effect_waits_for_final_and_sends_the_revised_text(self):
        orch = Orchestrator()
        step(orch, 1, "email dana that the meeting moved to tuesday and then check",
             s1={"side_effect": True}, s2={})
        self.assertEqual(orch.dispatch_log, [])
        step(orch, 2, "Email Dana that the meeting moved to Thursday, and then check if room 4B is free.", final=True,
             s1={"side_effect": True}, s2={})
        email = [d for d in orch.dispatch_log if "Dana" in d["instruction"]]
        self.assertEqual(len(email), 1)
        self.assertTrue(email[0]["is_final"])
        self.assertIn("Thursday", email[0]["instruction"])

    def test_dependent_request_stays_pending_on_its_dependency(self):
        orch = Orchestrator()
        tasks = step(orch, 1, "Find the sales report and then summarize it.", final=True,
                     s1={}, s2={"depends": "s1"})
        summary = tasks["and then summarize it."]
        self.assertEqual(summary.status, Status.READY)
        self.assertEqual(summary.depends_on, [tasks["Find the sales report"].task_id])
        snapshot = orch.snapshot("c", "u")
        self.assertEqual([t["status"] for t in snapshot["tasks"]], ["running", "pending"])

    def test_wait_on_the_final_transcript_asks_for_clarification(self):
        orch = Orchestrator()
        step(orch, 1, "Book it.", final=True, s1={"ready": "wait"})
        self.assertEqual(statuses(orch), [("Book it.", Status.NEEDS_CLARIFICATION)])
        self.assertEqual(orch.snapshot("c", "u")["clarification_needed"], ["Book it."])

    def test_non_request_segments_create_no_task(self):
        orch = Orchestrator()
        step(orch, 1, "My sister says I should email the landlord, but can you look up his number?", final=True,
             s1={"kind": "description"}, s2={})
        self.assertEqual([d["instruction"] for d in orch.dispatch_log], ["but can you look up his number?"])
        self.assertEqual(len(orch.tasks), 1)

    def test_intentional_repeat_creates_two_tasks(self):
        orch = Orchestrator()
        step(orch, 1, "Roll a die for me. Okay, now roll it again.", final=True, s1={}, s2={})
        self.assertEqual(len(orch.dispatch_log), 2)


class Lacks(unittest.TestCase):
    """A task whose call would have nothing to look up waits for the person's answer, however ready it reads."""

    PLACE = {"tool": "weather", "arguments": {"place": None, "when": "today"}, "missing": ("place",)}

    def test_a_task_that_lacks_an_argument_asks_for_it(self):
        orch = Orchestrator()
        orch.waits = lambda task: dict(self.PLACE)
        step(orch, 1, "What's the weather like?", final=True, s1={})
        self.assertEqual(statuses(orch), [("What's the weather like?", Status.NEEDS_CLARIFICATION)])
        self.assertEqual(orch.dispatch_log, [])
        task, = orch.snapshot("c", "u")["tasks"]
        self.assertEqual((task["status"], task["needs"]), ("needs_clarification", ["place"]))
        self.assertEqual(orch.snapshot("c", "u")["clarification_needed"], ["What's the weather like?"])

    def test_a_place_in_the_words_lets_the_same_task_run(self):
        from voice.tools import lookups
        orch = Orchestrator()
        orch.waits = lambda task: None if lookups.find_place(task.instruction) else dict(self.PLACE)
        step(orch, 1, "What's the weather like", s1={})
        self.assertEqual(statuses(orch), [("What's the weather like", Status.NEEDS_CLARIFICATION)])
        step(orch, 2, "What's the weather like in Denver?", final=True, s1={})
        self.assertEqual([d["instruction"] for d in orch.dispatch_log], ["What's the weather like in Denver?"])

    def test_the_answered_task_runs_and_replaces_the_waiting_one(self):
        orch = Orchestrator()
        orch.waits = lambda task: dict(self.PLACE)
        step(orch, 1, "What's the weather like?", final=True, s1={})
        waiting = orch.tasks["task_1"]
        answered = orch.answer_earlier("turn_001:task_1", waiting.instruction, False)
        self.assertEqual((answered.status, answered.revises), (Status.DISPATCHED, "turn_001:task_1"))
        self.assertEqual([d["instruction"] for d in orch.dispatch_log], ["What's the weather like?"])
        self.assertIsNone(answered.waiting, "the answered task has nothing left to ask")


class Corrections(unittest.TestCase):
    def test_correction_before_dispatch_supersedes_and_never_sends_the_old_version(self):
        orch = Orchestrator()
        step(orch, 1, "book a table for four no wait for six", s1={"side_effect": True},
             s2={"kind": "correction", "corrects": "replace_s1"})
        step(orch, 2, "Book a table for four, no wait, for six, at Luigi's.", final=True, s1={"side_effect": True},
             s2={"kind": "correction", "corrects": "replace_s1"})
        old, new = orch.tasks.values()
        self.assertEqual(old.status, Status.SUPERSEDED)
        self.assertEqual(new.status, Status.DISPATCHED)
        self.assertEqual(new.revises, old.task_id)
        self.assertEqual(len(orch.dispatch_log), 1)
        self.assertIn("for four", orch.dispatch_log[0]["instruction"])
        self.assertIn("for six", orch.dispatch_log[0]["instruction"])
        self.assertTrue(orch.dispatch_log[0]["is_final"])

    def test_correction_after_dispatch_requests_cancel_and_sends_the_new_version(self):
        orch = Orchestrator()
        step(orch, 1, "find flights on friday and check hotels", s1={}, s2={})
        step(orch, 2, "find flights on friday and check hotels actually make the flights saturday", s1={}, s2={},
             s3={"kind": "correction", "corrects": "replace_s1"})
        step(orch, 3, "Find flights on Friday and check hotels. Actually, make the flights Saturday.", final=True,
             s1={}, s2={}, s3={"kind": "correction", "corrects": "replace_s1"})
        flights, hotels, saturday = orch.tasks.values()
        self.assertEqual(flights.status, Status.CANCEL_REQUESTED)
        self.assertEqual(hotels.status, Status.DISPATCHED)
        self.assertEqual(saturday.status, Status.DISPATCHED)
        self.assertEqual(saturday.revises, flights.task_id)
        self.assertEqual([t["status"] for t in orch.snapshot("c", "u")["tasks"]],
                         ["cancel_requested", "running", "running"])

    def test_cancel_correction_withdraws_without_a_replacement(self):
        orch = Orchestrator()
        step(orch, 1, "Book a table for four. Actually never mind.", final=True, s1={"side_effect": True},
             s2={"kind": "correction", "corrects": "cancel_s1"})
        self.assertEqual(statuses(orch), [("Book a table for four.", Status.WITHDRAWN)])
        self.assertEqual(orch.dispatch_log, [])


class Revisions(unittest.TestCase):
    def test_asr_revision_keeps_the_same_task(self):
        orch = Orchestrator()
        step(orch, 1, "um can you find the reports from tuesday", s1={})
        step(orch, 2, "Um, can you find the report from Tuesday?", final=True, s1={})
        self.assertEqual(len(orch.tasks), 1)
        self.assertEqual(orch.dispatch_log[0]["instruction"], "Um, can you find the report from Tuesday?")

    def test_reclassified_segment_withdraws_and_can_come_back(self):
        orch = Orchestrator()
        step(orch, 1, "look up the score and", s1={})
        step(orch, 2, "look up the score and", s1={"kind": "chitchat"})
        self.assertEqual(statuses(orch), [("look up the score", Status.WITHDRAWN)])
        step(orch, 3, "Look up the score.", final=True, s1={})
        self.assertEqual(statuses(orch), [("Look up the score.", Status.DISPATCHED)])

    def test_segment_without_a_decision_keeps_its_task_unchanged(self):
        orch = Orchestrator()
        step(orch, 1, "look up the score and find", s1={"side_effect": True}, s2={})
        step(orch, 2, "Look up the score and find the schedule.", final=True)
        self.assertEqual([t.status for t in orch.tasks.values()], [Status.READY, Status.PROPOSED])

    def test_dispatched_task_cannot_return_to_proposed(self):
        orch = Orchestrator()
        step(orch, 1, "look up the score and", s1={}, s2={})
        step(orch, 2, "look up the score and find", s1={}, s2={})
        task = next(iter(orch.tasks.values()))
        self.assertEqual(task.status, Status.DISPATCHED)
        with self.assertRaises(IllegalTransition):
            orch._move(task, Status.PROPOSED, "test")


class Delegates(unittest.TestCase):
    def test_delegate_after_dispatch_links_without_a_second_dispatch(self):
        orch = Orchestrator()
        step(orch, 1, "find the budget spreadsheet and tell me", s1={}, s2={"depends": "s1"})
        did = orch.submit_delegate("Find the budget spreadsheet")
        step(orch, 1, "find the budget spreadsheet and tell me", delegates={did: "s1"}, s1={}, s2={"depends": "s1"})
        self.assertEqual(len(orch.dispatch_log), 1)
        first = next(iter(orch.tasks.values()))
        self.assertEqual(first.sources, {"orchestrator", "voice_delegate"})

    def test_delegate_before_the_segment_closes_dispatches_once(self):
        orch = Orchestrator()
        step(orch, 1, "look up tomorrow's weather in seattle", s1={})
        did = orch.submit_delegate("Look up tomorrow's weather in Seattle")
        step(orch, 1, "look up tomorrow's weather in seattle", delegates={did: "s1"}, s1={})
        step(orch, 2, "Look up tomorrow's weather in Seattle, and, um, what time does the ferry leave?", final=True,
             s1={}, s2={})
        self.assertEqual(len(orch.dispatch_log), 2)
        self.assertEqual(orch.dispatch_log[0]["sources"], ["orchestrator", "voice_delegate"])
        self.assertEqual(orch.dispatch_log[0]["revision"], 1)
        weather = next(iter(orch.tasks.values()))
        self.assertEqual(weather.sources, {"orchestrator", "voice_delegate"})

    def test_unmatched_delegate_is_its_own_task(self):
        orch = Orchestrator()
        did = orch.submit_delegate("Play some jazz")
        step(orch, 1, "look up the score", delegates={did: None}, s1={})
        self.assertEqual([d["instruction"] for d in orch.dispatch_log], ["Play some jazz"])


if __name__ == "__main__":
    unittest.main()
