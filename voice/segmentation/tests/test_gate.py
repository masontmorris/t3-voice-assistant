"""The gate's score: which answers it reads, and where its two bars sit.

The measured case behind both rules is the live gate eval (`voice/segmentation/live_gate_eval.py`): a task's `kind`
answer is a five-way choice, so a request/description tie reads 0.375 and was held at the 0.40 bar while the same
task's readiness answer read 0.89.
"""

import unittest

from voice.segmentation.contract import Kind, Readiness, SegmentDecision
from voice.segmentation.gate import PLAN_A, Gate


def decision(**confidence):
    """A ready read-only request whose answers are the confidences given."""
    return SegmentDecision(sid="s1", kind=Kind.REQUEST, readiness=Readiness.DISPATCH_NOW, depends_on=None,
                           corrects=None, side_effect=False, confidence=confidence)


class GateScoreTest(unittest.TestCase):
    def test_a_near_tie_on_kind_does_not_hold_a_task_whose_readiness_is_firm(self):
        verdict = PLAN_A(decision(kind=0.369, ready=0.891, side_effect=0.956))
        self.assertTrue(verdict.passed)
        self.assertEqual(verdict.weakest, "ready")

    def test_a_weak_readiness_answer_holds_the_task(self):
        verdict = PLAN_A(decision(kind=0.9, ready=0.30, side_effect=0.95))
        self.assertFalse(verdict.passed)
        self.assertEqual(verdict.weakest, "ready")

    def test_a_side_effect_needs_the_stricter_bar_and_the_gate_reads_its_own_answer(self):
        held = PLAN_A(SegmentDecision("s1", Kind.REQUEST, Readiness.DISPATCH_NOW, None, None, True,
                                      {"kind": 0.9, "ready": 0.9, "side_effect": 0.2}))
        self.assertFalse(held.passed)
        self.assertEqual(held.weakest, "side_effect")
        self.assertEqual(held.threshold, PLAN_A.side_effect_threshold)
        passed = PLAN_A(SegmentDecision("s1", Kind.REQUEST, Readiness.DISPATCH_NOW, None, None, True,
                                        {"kind": 0.9, "ready": 0.9, "side_effect": 0.98}))
        self.assertTrue(passed.passed)

    def test_a_yes_no_answer_counts_by_its_distance_from_a_coin_flip(self):
        self.assertTrue(Gate(0.40, 0.80)(decision(ready=0.9, side_effect=0.30)).passed)

    def test_no_answers_hold_the_task(self):
        self.assertFalse(PLAN_A(decision()).passed)


if __name__ == "__main__":
    unittest.main()
