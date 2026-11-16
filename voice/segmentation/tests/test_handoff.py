import unittest

from voice.segmentation.contract import TranscriptUpdate
from voice.segmentation.gate import Gate
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
        self.assertEqual(result["duplicate_of"], ["task_1"])
        self.assertEqual(len(orch.tasks), 1)
        self.assertEqual(orch.tasks["task_1"].sources, {"orchestrator", "lfm"})

    def test_a_delegate_that_restates_two_tasks_links_to_both_and_runs_them(self):
        orch = Orchestrator()
        dad = orch._new_task("tell my dad I'm home", 0, {"orchestrator"}, side_effect=True)
        lights = orch._new_task("turn the lights on", 0, {"orchestrator"}, side_effect=True)
        result = orch.delegate_from_voice("Tell my dad I'm home, then turn the lights on", True, None, run_linked=True)
        self.assertEqual(result["task_ids"], [dad.task_id, lights.task_id])
        self.assertEqual(result["duplicate_of"], [dad.task_id, lights.task_id])
        self.assertEqual((dad.status, lights.status), (Status.DISPATCHED, Status.DISPATCHED))
        self.assertEqual(len(orch.tasks), 2)

    def test_a_delegate_that_covers_a_task_and_brings_tail_words_makes_one_task_for_the_tail(self):
        orch = Orchestrator()
        milk = orch._new_task("Add milk to the shopping list.", 0, {"orchestrator"}, side_effect=True)
        result = orch.delegate_from_voice("Add milk to the shopping list. Oh, and how long is the drive to Boulder?",
                                          True, "drive to Boulder?", run_linked=True)
        self.assertEqual(result["duplicate_of"], [milk.task_id])
        self.assertEqual(milk.status, Status.DISPATCHED)
        tail = orch.tasks[result["task_ids"][-1]]
        self.assertEqual((tail.instruction, tail.status), ("drive to Boulder?", Status.DISPATCHED))

    def test_a_delegate_that_covers_no_task_makes_one_new_task(self):
        orch = orchestrator_with(("Find last week's sales report", False))
        result = orch.delegate_from_voice("Book the cheapest flight to Chicago", False, "flight to Chicago")
        self.assertEqual(result["duplicate_of"], [])
        self.assertEqual(orch.tasks[result["task_ids"][0]].instruction, "Book the cheapest flight to Chicago")
        self.assertEqual(orch.tasks[result["task_ids"][0]].status, Status.DISPATCHED)

    def test_a_delegate_that_changes_the_request_does_not_link_to_the_task(self):
        orch = orchestrator_with(("find the report", False))
        result = orch.delegate_from_voice("delete the report", False, None)
        self.assertEqual(result["duplicate_of"], [])
        self.assertEqual(len(orch.tasks), 2)

    def test_a_delegate_that_claims_a_side_effect_the_task_lacks_is_not_a_duplicate(self):
        orch = orchestrator_with(("find the sales report", False))
        result = orch.delegate_from_voice("delete the sales report", True, None)
        self.assertEqual(result["duplicate_of"], [])
        self.assertTrue(orch.tasks[result["task_ids"][0]].side_effect)

    def test_a_read_only_claim_about_a_side_effect_task_links_and_keeps_the_hold(self):
        orch = orchestrator_with(("remind me at 3 to call the dentist", True))
        result = orch.delegate_from_voice("Remind me at 3 o'clock to call the dentist", False, None)
        self.assertEqual(result["duplicate_of"], ["task_1"])
        self.assertTrue(orch.tasks["task_1"].side_effect)

    def test_a_read_only_delegate_does_not_run_a_side_effect_task_it_links(self):
        orch = Orchestrator()
        reminder = orch._new_task("remind me at 3 to call the dentist", 0, {"orchestrator"}, side_effect=True)
        orch._move(reminder, Status.HELD, "test")
        orch.delegate_from_voice("Remind me at 3 o'clock to call the dentist", False, None, run_linked=True)
        self.assertEqual(reminder.status, Status.HELD)
        self.assertEqual(orch.dispatch_log, [])

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
        self.assertEqual(orch.amend_from_voice("task_9", "make it thursday")["ok"], False)

    def test_amend_of_a_superseded_task_amends_its_newest_successor(self):
        orch = Orchestrator()
        old = orch._new_task("set a reminder for tuesday", 0, {"orchestrator"}, side_effect=True)
        orch._move(old, Status.HELD, "test")
        first = orch.amend_from_voice(old.task_id, "make it wednesday")
        second = orch.amend_from_voice(old.task_id, "make it thursday")
        self.assertEqual(second["replaces"], first["task_id"])
        self.assertEqual(orch.tasks[second["task_id"]].revises, first["task_id"])

    def test_amend_revives_a_task_the_decider_withdrew(self):
        orch = Orchestrator()
        old = orch._new_task("set a reminder for tuesday", 0, {"orchestrator"}, side_effect=True)
        orch._move(old, Status.WITHDRAWN, "corrected by segment \"make it wednesday\"")
        result = orch.amend_from_voice(old.task_id, "make it wednesday")
        new = orch.tasks[result["task_id"]]
        self.assertTrue(result["ok"])
        self.assertEqual((old.status, new.revises, new.status), (Status.WITHDRAWN, old.task_id, Status.DISPATCHED))
        self.assertEqual(new.instruction, "set a reminder for tuesday (corrected: make it wednesday)")


def gated(decider, gate=Gate(0.4, 0.9)):
    seg = HybridSegmenter(decider, "test")
    seg.orch.gate, seg.orch.side_effects_wait_for_final = gate, False
    return seg


SOUP = ["find the recipe for lentil soup then make", "find the recipe for lentil soup then make a list"]
EMAIL = ["email dana the notes and then check the room"] * 3


class TaskGate(unittest.TestCase):
    def test_a_confident_task_runs_during_speech(self):
        seg = gated(FakeDecider(starters={"then"}))
        for i, text in enumerate(SOUP, 1):
            seg.update(TranscriptUpdate(i, text, False))
        self.assertEqual([d["instruction"] for d in seg.dispatch_log], ["find the recipe for lentil soup"])

    def test_a_task_below_the_bar_waits_and_the_gate_reads_it_again_at_the_next_point(self):
        decider = FakeDecider(starters={"then"}, ready_margin=0.3)
        seg = gated(decider)
        for i, text in enumerate(SOUP, 1):
            seg.update(TranscriptUpdate(i, text, False))
        soup = task(seg, "soup")
        self.assertEqual((soup["status"], seg.dispatch_log), ("held", []))
        self.assertIn("gate: ready confidence 0.30 fell short of 0.40", soup["history"][-1]["reason"])
        decider.ready_margin = 0.8
        seg.update(TranscriptUpdate(3, SOUP[1] + " and check the oven", False))
        self.assertEqual(task(seg, "soup")["status"], "dispatched")

    def test_a_side_effect_needs_the_stricter_bar(self):
        strict = gated(FakeDecider(), Gate(0.4, 0.9))
        loose = gated(FakeDecider(), Gate(0.4, 0.7))
        for seg in (strict, loose):
            for i, text in enumerate(EMAIL, 1):
                seg.update(TranscriptUpdate(i, text, False))
        self.assertEqual(task(strict, "dana")["status"], "held")
        self.assertEqual(task(loose, "dana")["status"], "dispatched")

    def test_the_close_without_settling_leaves_every_task_as_the_last_point_left_it(self):
        seg = gated(FakeDecider(starters={"then"}, ready_margin=0.3))
        for i, text in enumerate(SOUP, 1):
            seg.update(TranscriptUpdate(i, text, False))
        before = seg.records()
        point = seg.close(TranscriptUpdate(3, "Find the recipe for lentil soup, then make a list.", True), settle=False)
        self.assertEqual([(t["task_id"], t["status"]) for t in seg.records()],
                         [(t["task_id"], t["status"]) for t in before])
        self.assertEqual(point["calls"], [])

    def test_a_voice_dispatch_after_the_gated_close_logs_as_after_the_final_transcript(self):
        seg = gated(FakeDecider(starters={"then"}, ready_margin=0.3))
        for i, text in enumerate(SOUP, 1):
            seg.update(TranscriptUpdate(i, text, False))
        seg.close(TranscriptUpdate(3, "Find the recipe for lentil soup, then make a list.", True), settle=False)
        seg.orch.run_from_voice(task(seg, "soup")["task_id"])
        self.assertEqual([(d["revision"], d["is_final"]) for d in seg.dispatch_log], [(3, True)])

    def test_run_task_starts_a_task_that_waits_and_refuses_one_that_ran(self):
        orch = orchestrator_with(("find the sales report", False))
        held = orch._new_task("email dana", 0, {"orchestrator"}, side_effect=True)
        orch._move(held, Status.HELD, "test")
        self.assertTrue(orch.run_from_voice(held.task_id)["ok"])
        self.assertEqual(held.status, Status.DISPATCHED)
        self.assertFalse(orch.run_from_voice("task_1")["ok"])

    def test_a_delegate_that_links_to_a_waiting_task_runs_it_when_asked(self):
        orch = Orchestrator()
        held = orch._new_task("find the sales report", 0, {"orchestrator"})
        orch._move(held, Status.HELD, "test")
        orch.delegate_from_voice("find the sales report", False, None, run_linked=True)
        self.assertEqual(held.status, Status.DISPATCHED)


if __name__ == "__main__":
    unittest.main()
