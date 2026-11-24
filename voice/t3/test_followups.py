"""Follow-ups about the thread the call last spoke about, on recorded d1.2 answers: with the thread in view, one that
asks about it, sends it a message or stops it reaches that T3 tool, and chat, thanks and lookups stay where they were.
Without it, one reaches T3 only because a parked thread request goes there (`Orchestrator._settle`): as its own act
when the tool answer is firm, which T3 confirms, else as a lookup that acts on nothing."""

from __future__ import annotations

import json
import unittest

from voice.t3.eval import record_day_question_pairs as pairs
from voice.t3.eval.record_followups import CASES, FIXTURE, T3, decide, key, passes

RECORDED = json.loads(FIXTURE.read_text(encoding="utf-8"))
PAIRS = json.loads(pairs.FIXTURE.read_text(encoding="utf-8"))
# Held-out cases d1.2 does not always route as the case wants, so a re-recording may take either. "Cancel that one"
# with no follow-up held is read as withdrawing the earlier request (correction 0.50, request 0.43), though its tool answer is t3_stop at 0.9, so nothing is
# dispatched and nothing acts.
# "Please tell it to stop touching the config." is a coin flip for d1.2 with or without the longer day question: it reads
# t3_tell in 3 of 8 fresh samples on the base question set and 4 of 8 on the shipped one, and three controls of the same
# kind read their tool in 8 of 8 on both (`PAIRS`, `test_the_longer_day_question_does_not_move_a_thread_follow_up`).
MISSES = {("Cancel that one.", False), ("Please tell it to stop touching the config.", True)}


class Replay:
    """The decider, answering each request from the fixture, and only a request it recorded."""

    def call(self, route, body, label=""):
        answer = RECORDED["answers"].get(key(body))
        if answer is None:
            raise KeyError("no recorded answer for this request: re-run voice.t3.eval.record_followups")
        return {"status": 200, "response": answer, "timing": {"ms": 0.0, "started_at": ""}}


class FollowUps(unittest.TestCase):
    def test_the_longer_day_question_does_not_move_a_thread_follow_up(self):
        hits = {}
        for case in pairs.cases():
            utterance, recent, earlier, want, _ = case
            for name in pairs.ARMS:
                with pairs.arm(name):
                    for row in (r for r in PAIRS["rows"] if r["arm"] == name and r["utterance"] == utterance):
                        routed = decide(pairs.Replay(PAIRS["answers"], row["sample"]), utterance, recent, earlier,
                                        "after")
                        self.assertEqual(routed, row["routed"])
                        hits[name, utterance] = hits.get((name, utterance), 0) + (routed == [want])
        exempt = "Please tell it to stop touching the config."
        for utterance in (u for u in pairs.UTTERANCES if u != exempt):
            self.assertEqual((hits["base", utterance], hits["shipped", utterance]), (pairs.SAMPLES, pairs.SAMPLES),
                             utterance)
        self.assertGreaterEqual(hits["shipped", exempt], hits["base", exempt], "the shipped question reads it no worse")
        self.assertTrue(0 < hits["base", exempt] < pairs.SAMPLES, "and the base question misses it too")

    def test_every_case_was_recorded_in_both_modes(self):
        self.assertEqual(len(RECORDED["rows"]), 2 * len(CASES))

    def test_with_the_thread_in_view_each_follow_up_goes_where_its_case_says(self):
        for row in RECORDED["rows"]:
            if row["mode"] != "after":
                continue
            with self.subTest(row["utterance"], held=row["held"]):
                routed = decide(Replay(), row["utterance"], row["recent"], row["earlier"], "after")
                self.assertEqual(routed, row["routed"])
                if (row["utterance"], row["held"]) not in MISSES:
                    self.assertTrue(passes(row["want"], routed), routed)

    def test_before_a_follow_up_reaches_t3_only_as_its_own_act_or_a_lookup(self):
        for row in RECORDED["rows"]:
            if row["mode"] == "before" and row["want"] in T3:
                with self.subTest(row["utterance"]):
                    routed = decide(Replay(), row["utterance"], row["recent"], row["earlier"], "before")
                    self.assertEqual(routed, row["routed"])
                    self.assertIn(routed, ([], [row["want"]], ["t3_ask"]))


if __name__ == "__main__":
    unittest.main()
