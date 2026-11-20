"""Follow-ups about the thread the call last spoke about, on recorded d1.2 answers: with the thread in view, one that
asks about it, sends it a message or stops it reaches that T3 tool, and chat, thanks and lookups stay where they were.
Without it, one reaches T3 only because a parked thread request goes there (`Orchestrator._settle`): as its own act
when the tool answer is firm, which T3 confirms, else as a lookup that acts on nothing."""

from __future__ import annotations

import json
import unittest

from voice.t3.eval.record_followups import CASES, FIXTURE, T3, decide, key, passes

RECORDED = json.loads(FIXTURE.read_text(encoding="utf-8"))
# Held-out cases d1.2 does not route as the case wants. "Cancel that one" with no follow-up held is read as withdrawing
# the earlier request (correction 0.50, request 0.43), though its tool answer is t3_stop at 0.9, so nothing is
# dispatched and nothing acts.
MISSES = {("Cancel that one.", False)}


class Replay:
    """The decider, answering each request from the fixture, and only a request it recorded."""

    def call(self, route, body, label=""):
        answer = RECORDED["answers"].get(key(body))
        if answer is None:
            raise KeyError("no recorded answer for this request: re-run voice.t3.eval.record_followups")
        return {"status": 200, "response": answer, "timing": {"ms": 0.0, "started_at": ""}}


class FollowUps(unittest.TestCase):
    def test_every_case_was_recorded_in_both_modes(self):
        self.assertEqual(len(RECORDED["rows"]), 2 * len(CASES))

    def test_with_the_thread_in_view_each_follow_up_goes_where_its_case_says(self):
        for row in RECORDED["rows"]:
            if row["mode"] != "after":
                continue
            with self.subTest(row["utterance"], held=row["held"]):
                routed = decide(Replay(), row["utterance"], row["recent"], row["earlier"], "after")
                self.assertEqual(routed, row["routed"])
                self.assertEqual(passes(row["want"], routed), (row["utterance"], row["held"]) not in MISSES, routed)

    def test_before_a_follow_up_reaches_t3_only_as_its_own_act_or_a_lookup(self):
        for row in RECORDED["rows"]:
            if row["mode"] == "before" and row["want"] in T3:
                with self.subTest(row["utterance"]):
                    routed = decide(Replay(), row["utterance"], row["recent"], row["earlier"], "before")
                    self.assertEqual(routed, row["routed"])
                    self.assertIn(routed, ([], [row["want"]], ["t3_ask"]))


if __name__ == "__main__":
    unittest.main()
