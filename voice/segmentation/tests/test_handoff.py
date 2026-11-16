import unittest

from voice.segmentation.contract import TranscriptUpdate
from voice.segmentation.hybrid import HybridSegmenter
from voice.segmentation.tasks import Orchestrator, Status
from voice.segmentation.tests.test_hybrid import FakeDecider


def handoff(partials, final, decider=None):
    decider = decider or FakeDecider()
    seg = HybridSegmenter(decider, "test")
    for i, text in enumerate(partials, 1):
        seg.update(TranscriptUpdate(i, text, False))
    calls = len(decider.bodies)
    point = seg.close(TranscriptUpdate(len(partials) + 1, final, True))
    return seg, point, len(decider.bodies) - calls


def task(seg, word):
    return next(t for t in seg.records() if word in t["instruction"].lower())


class Close(unittest.TestCase):
    def test_the_close_makes_no_call_and_leaves_unread_words_to_the_voice_model(self):
        seg, point, calls = handoff(["email dana", "email dana the q3"], "Email Dana the Q3 numbers.")
        self.assertEqual(calls, 0)
        self.assertEqual(seg.records(), [])
        self.assertEqual((point["covered_words"], point["tail_words"]), (0, 5))
        self.assertEqual(point["unreviewed_speech"], "Email Dana the Q3 numbers.")

    def test_a_side_effect_whose_words_did_not_change_is_released_at_the_close(self):
        partials = ["email dana that the meeting moved and then check the room is free"] * 3
        seg, point, calls = handoff(partials, "Email Dana that the meeting moved, and then check the room is free.")
        email = task(seg, "dana")
        self.assertEqual(calls, 0)
        self.assertEqual(email["status"], "dispatched")
        self.assertTrue(next(d for d in seg.dispatch_log if d["task_id"] == email["task_id"])["is_final"])
        self.assertIsNone(point["unreviewed_speech"])

    def test_a_side_effect_whose_words_changed_after_its_last_decision_stays_held(self):
        seg, point, _ = handoff(["look up the tide at rye and email", "look up the tide at rye and email dana the"],
                                "Look up the tide at Rye and email Dana the tide times.")
        email = task(seg, "dana")
        self.assertEqual(email["status"], "held")
        self.assertNotIn(email["task_id"], {d["task_id"] for d in seg.dispatch_log})
        self.assertEqual(point["unreviewed_speech"], "tide times.")

    def test_a_read_only_request_whose_words_grew_is_held_because_no_decider_read_the_new_words(self):
        seg, _, _ = handoff(["look up the tide at rye and if the", "look up the tide at rye and if the ferry runs"],
                            "Look up the tide at Rye and if the ferry runs book two seats.")
        ferry = task(seg, "ferry")
        self.assertEqual(ferry["status"], "held")
        self.assertIn("book two seats", ferry["instruction"])
        self.assertNotIn(ferry["task_id"], {d["task_id"] for d in seg.dispatch_log})


def orchestrator_with(*tasks):
    """An orchestrator holding dispatched tasks, as (instruction, side_effect) pairs."""
    orch = Orchestrator()
    for instruction, side_effect in tasks:
        t = orch._new_task(instruction, 0, {"orchestrator"}, side_effect=side_effect)
        orch._move(t, Status.DISPATCHED, "test")
    return orch


class VoiceTools(unittest.TestCase):
    def test_a_delegate_that_restates_a_task_links_to_it(self):
        orch = orchestrator_with(("Find last week's sales report", False))
        result = orch.delegate_from_voice("Find the sales report from last week", False, "and email it to Dana")
        self.assertEqual(result["duplicate_of"], "task_1")
        self.assertEqual(len(orch.tasks), 1)
        self.assertEqual(orch.tasks["task_1"].sources, {"orchestrator", "lfm"})

    def test_a_delegate_that_brings_words_from_the_unreviewed_tail_is_a_new_task(self):
        orch = orchestrator_with(("Find last week's sales report", False))
        result = orch.delegate_from_voice("Find last week's sales report and email it to Dana", False,
                                          "and email it to Dana")
        self.assertNotIn("duplicate_of", result)
        self.assertEqual(orch.tasks[result["task_id"]].status, Status.DISPATCHED)

    def test_a_delegate_that_claims_a_side_effect_the_task_lacks_is_not_a_duplicate(self):
        orch = orchestrator_with(("find the sales report", False))
        result = orch.delegate_from_voice("delete the sales report", True, None)
        self.assertNotIn("duplicate_of", result)
        self.assertTrue(orch.tasks[result["task_id"]].side_effect)

    def test_a_read_only_claim_about_a_side_effect_task_links_and_keeps_the_hold(self):
        orch = orchestrator_with(("remind me at 3 to call the dentist", True))
        result = orch.delegate_from_voice("Remind me at 3 o'clock to call the dentist", False, None)
        self.assertEqual(result["duplicate_of"], "task_1")
        self.assertTrue(orch.tasks["task_1"].side_effect)

    def test_cancel_withdraws_an_undispatched_task_and_cancels_a_dispatched_one(self):
        orch = orchestrator_with(("find the sales report", False))
        held = orch._new_task("email dana", 0, {"orchestrator"}, side_effect=True)
        orch._move(held, Status.HELD, "test")
        self.assertTrue(orch.cancel_from_voice(held.task_id)["ok"])
        self.assertTrue(orch.cancel_from_voice("task_1")["ok"])
        self.assertEqual((held.status, orch.tasks["task_1"].status), (Status.WITHDRAWN, Status.CANCEL_REQUESTED))
        self.assertFalse(orch.cancel_from_voice("task_1")["ok"])
        self.assertFalse(orch.cancel_from_voice("task_9")["ok"])

    def test_amend_supersedes_the_task_and_the_new_one_carries_both_requests(self):
        orch = Orchestrator()
        old = orch._new_task("set a reminder for tuesday", 0, {"orchestrator"}, side_effect=True)
        orch._move(old, Status.HELD, "test")
        result = orch.amend_from_voice(old.task_id, "make it wednesday")
        new = orch.tasks[result["task_id"]]
        self.assertEqual((old.status, new.revises, new.side_effect), (Status.SUPERSEDED, old.task_id, True))
        self.assertEqual(new.instruction, "set a reminder for tuesday (corrected: make it wednesday)")
        self.assertFalse(orch.amend_from_voice(old.task_id, "make it thursday")["ok"])


if __name__ == "__main__":
    unittest.main()
