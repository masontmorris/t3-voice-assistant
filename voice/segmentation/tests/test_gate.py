"""The gate's score: which answers it reads, and where its two bars sit.

The measured case behind both rules is the live gate eval (`voice/research/segmentation/live_gate_eval.py`): a task's `kind`
answer is a five-way choice, so a request/description tie reads 0.375 and was held at the 0.40 bar while the same
task's readiness answer read 0.89.
"""

import unittest

from voice.segmentation.contract import Kind, Readiness, SegmentDecision
from voice.segmentation.gate import PLAN_A, Gate
from voice.tools.registry import ToolChoice


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


class NamedToolTest(unittest.TestCase):
    """Demo call 20261117-013438, turn 011: "check the weather in Charlotte, Nuxunda?" read `ready` at 0.33."""

    def charlotte(self, tool="weather", tool_confidence=0.90, side_effect=False):
        return SegmentDecision("s1", Kind.REQUEST, Readiness.DISPATCH_NOW, None, None, side_effect,
                               {"ready": 0.33, "side_effect": 0.97},
                               ToolChoice(tool, tool_confidence, {"place": "Charlotte", "when": None}))

    def test_a_firm_named_tool_runs_a_read_only_task_on_the_final_transcript(self):
        verdict = PLAN_A(self.charlotte(), is_final=True)
        self.assertTrue(verdict.passed)
        self.assertEqual(verdict.reason(), "gate: tool confidence 0.90 reached 0.70")

    def test_during_speech_the_weak_readiness_still_holds_it(self):
        self.assertFalse(PLAN_A(self.charlotte()).passed)

    def test_the_general_worker_a_weak_tool_answer_or_a_side_effect_is_still_held(self):
        self.assertFalse(PLAN_A(self.charlotte(tool="agent"), is_final=True).passed)
        self.assertFalse(PLAN_A(self.charlotte(tool_confidence=0.60), is_final=True).passed)
        self.assertFalse(PLAN_A(self.charlotte(side_effect=True), is_final=True).passed)

    def test_a_firm_t3_pick_passes_as_a_side_effect_because_it_asks_first(self):
        """#57's replay: "Tell it to also add a test." read `ready` at 0.49 while its tool answer was t3_tell at 0.98."""
        for tool in ("t3_start", "t3_ask", "t3_tell", "t3_stop"):
            verdict = PLAN_A(self.charlotte(tool=tool, tool_confidence=0.98, side_effect=True), is_final=True)
            self.assertEqual((verdict.passed, verdict.weakest), (True, "tool"), tool)
        self.assertFalse(PLAN_A(self.charlotte(tool="t3_stop", tool_confidence=0.43, side_effect=True),
                                is_final=True).passed, "a weak T3 pick is held, as on 'Stop, I was talking to someone'")
        self.assertFalse(PLAN_A(self.charlotte(tool="t3_tell", tool_confidence=0.98, side_effect=True)).passed,
                         "during speech a T3 pick waits like any side effect")
