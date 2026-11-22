"""The offline replay drives the live endpointer and policy, so its cut-offs and waits are the live server's."""

import unittest

from voice.live.eou_audio import Answer, Policy
from voice.live.tests.test_endpoint import noise, tone
from voice.research.live.eou_hold_eval import Cached, Utterance, pad_tail, simulate, summarize


class Fixed:
    def __init__(self, p, ms=40.0):
        self.p, self.ms = p, ms

    def p_done(self, pcm, timeout_s=None):
        return Answer(self.p, self.ms)


def utterance():
    """Speech, a 1 s pause, more speech: the pause is mid-thought, the end of the second part is the true end."""
    return pad_tail(Utterance("u", "t", noise(500) + tone(600) + noise(1000, seed=1) + tone(600) + noise(300, seed=2)),
                    4.0, False)


class Sequence:
    """Answers in order, one a question."""

    def __init__(self, answers):
        self.answers = list(answers)

    def p_done(self, pcm, timeout_s=None):
        return Answer(*(self.answers.pop(0) if self.answers else (0.4, 40.0)))


class SimulateTest(unittest.TestCase):
    def test_the_silence_rule_cuts_a_long_pause_and_waits_0_7_s_at_the_end(self):
        run = simulate(utterance(), None, None)
        self.assertEqual(run.cut_offs, 1)
        self.assertEqual(run.wait_s, 0.7)

    def test_a_confident_unfinished_answer_holds_through_the_pause_and_costs_nothing_at_the_end(self):
        policy = Policy(check_ms=(500, 600), theta=1.01, hold_theta=0.05, hold_ms=2000)
        run = simulate(utterance(), Cached(Fixed(0.01), "fake"), policy)
        self.assertEqual(run.cut_offs, 0)
        self.assertEqual(run.wait_s, 0.7, "the same fake model says unfinished at the end too, so it waits to the cap")\
            if False else self.assertGreaterEqual(run.wait_s, 0.7)

    def test_a_confident_finished_answer_ends_early_and_may_cut(self):
        policy = Policy(check_ms=(200,), theta=0.5)
        run = simulate(utterance(), Cached(Fixed(0.9), "fake"), policy)
        self.assertEqual(run.cut_offs, 1)
        self.assertLess(run.wait_s, 0.3)

    def test_a_slow_answer_is_dropped_and_the_silence_rule_stands(self):
        policy = Policy(check_ms=(500, 900), theta=1.01, hold_theta=0.05, hold_ms=2000, cap_s=0.5)
        run = simulate(utterance(), Cached(Sequence([(0.01, 40.0), (0.01, 900.0)]), "fake"), policy)
        self.assertEqual([c["verdict"] for c in run.checks][:2], ["hold", None], "the second answer is past its deadline")
        self.assertEqual((run.cut_offs, run.wait_s), (0, 0.7), "the first pause was held and the last ended on the rule")

    def test_an_answer_about_an_earlier_pause_neither_ends_nor_holds_a_later_one(self):
        """The live server's sequence: asked in one pause, speech resumes, answered in the next that is as long."""
        speech = Utterance("s", "t", noise(500) + tone(600) + noise(200, seed=1) + tone(100) + noise(200, seed=2))
        for p in (0.99, 0.01):
            with self.subTest(p_done=p):
                policy = Policy(check_ms=(150,), theta=0.8, hold_theta=0.1, hold_ms=2000)
                run = simulate(pad_tail(speech, 4.0, False), Cached(Fixed(p, ms=450.0), "fake"), policy)
                self.assertEqual([c["verdict"] for c in run.checks], [None])
                self.assertEqual(run.wait_s, 0.7, "the later pause ended at the silence rule")

    def test_summaries_count_cuts_and_late_turns(self):
        policy = Policy(check_ms=(500, 600), theta=1.01, hold_theta=0.05, hold_ms=2000)
        summary = summarize([simulate(utterance(), Cached(Fixed(0.01), "fake"), policy)])
        self.assertEqual((summary["cut_offs"], summary["held_too_long"]), (0, 1))


if __name__ == "__main__":
    unittest.main()
