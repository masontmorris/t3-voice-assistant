"""The free-reply check on the local d1-2B's recorded answers (voice/t3/fixtures/claims.json, made-up cases): a reply that
says something T3 does not support is flagged, and a supported reply and a chat reply are not. A decider that cannot
answer never flags one."""

from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

from voice.liquid_decider.client import BillingStop
from voice.t3.eval.claim_cases import HELD_OUT, NO_THREAD_HELD_OUT, NO_THREAD_PLAIN_SEEN, NO_THREAD_SEEN, SEEN
from voice.t3.test_verify import Failing
from voice.t3.verify import CLAIM_UNSUPPORTED_YES, Claim, Decider, claim_request

ROWS = json.loads((Path(__file__).parent / "fixtures" / "claims.json").read_text(encoding="utf-8"))
CASES = {(split, name): (reply, facts, want) for split, cases in (("seen", SEEN), ("held_out", HELD_OUT))
         for name, reply, facts, want in cases}


class Recorded:
    def __init__(self):
        self.answers = {row["request_hash"]: row["answers"] for row in ROWS}

    def call(self, route, body, label=""):
        key = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
        return {"status": 200, "response": {"answers": self.answers[key]}}


class Check(unittest.TestCase):
    def flags(self, split):
        decider = Decider(Recorded())
        found = {}
        for (kind, name), (reply, facts, want) in CASES.items():
            if kind == split:
                found[name] = (want, decider.claims(reply, facts))
        return found

    def test_the_recording_is_of_these_cases(self):
        self.assertEqual({(row["split"], row["name"]) for row in ROWS}, set(CASES), "the cases changed: re-record")
        for row in ROWS:
            reply, facts, _ = CASES[(row["split"], row["name"])]
            body = claim_request(reply, facts)
            self.assertEqual(hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest(),
                             row["request_hash"], "the question changed: re-run record_claims")

    def test_no_seen_reply_that_is_not_wrong_is_flagged_and_the_held_out_false_flags_are_the_known_two(self):
        flagged = {split: sorted(name for name, (want, claim) in self.flags(split).items()
                                 if want != "wrong" and claim.wrong) for split in ("seen", "held_out")}
        self.assertEqual(flagged["seen"], [])
        # The held-out set was written before its first run. The joke is a chat line the decider reads as saying
        # something about a thread (0.58 against the 0.5 bar). On the GPU recording, made before the cases were re-worded,
        # a time of day was flagged too (0.51); on this CPU recording it is 0.49.
        self.assertEqual(flagged["held_out"], ["approval_joke"])

    def test_most_wrong_replies_are_flagged(self):
        caught = {split: sorted(name for name, (want, claim) in self.flags(split).items()
                                if want == "wrong" and claim.wrong) for split in ("seen", "held_out")}
        self.assertEqual((len(caught["seen"]), len(caught["held_out"])), (7, 8))

    def no_thread(self):
        rows = json.loads((Path(__file__).parent / "fixtures" / "claims_no_thread.json").read_text(encoding="utf-8"))
        recorded = Recorded()
        recorded.answers = {row["request_hash"]: row["answers"] for row in rows}
        cases = {"plain_seen": NO_THREAD_PLAIN_SEEN, "noted_seen": NO_THREAD_SEEN, "noted_held_out": NO_THREAD_HELD_OUT}
        self.assertEqual([(r["split"], r["name"], r["want"]) for r in rows],
                         [(split, n, w) for split, found in cases.items() for n, _, _, w in found])
        decider = Decider(recorded)
        return {split: {name: (want, decider.claims(reply, facts)) for name, reply, facts, want in found}
                for split, found in cases.items()}

    def test_with_no_thread_in_view_an_offer_or_a_question_about_doing_something_is_not_flagged(self):
        """Recorded once on the local d1-2B (GPU). The shipped bar for a claim is the 0.5 of the sets above; with no
        thread in view it was 7 of the 15 replies that are no claim (offers, questions, a promise after a yes) on the
        seen set. With the note in the facts and the bar at 0.75, set on the seen set, none of the 15 and none of the 11
        of the held-out set, which was scored once."""
        found = self.no_thread()
        at_old_bar = [n for n, (want, claim) in found["plain_seen"].items()
                      if want != "wrong" and claim.unsupported >= CLAIM_UNSUPPORTED_YES]
        self.assertEqual(len(at_old_bar), 7, at_old_bar)
        for split in ("noted_seen", "noted_held_out"):
            self.assertEqual([n for n, (want, claim) in found[split].items() if want != "wrong" and claim.wrong], [],
                             split)

    def test_with_no_thread_in_view_the_clearest_claims_are_flagged_and_most_are_not(self):
        found = self.no_thread()
        caught = {split: sorted(n for n, (want, claim) in found[split].items() if want == "wrong" and claim.wrong)
                  for split in ("noted_seen", "noted_held_out")}
        self.assertEqual(caught["noted_seen"], ["new_thread", "running", "sent", "started"])
        self.assertEqual(caught["noted_held_out"], ["h_opened"])

    def test_a_reply_is_judged_at_the_no_thread_bar_only_when_no_thread_is_in_view(self):
        recorded = Recorded()
        recorded.answers = {}
        reply = {"choice": "unsupported", "probabilities": {"unsupported": 0.6, "supported": 0.1, "nothing": 0.3}}

        class Fixed:
            def call(self, route, body, label=""):
                return {"status": 200, "response": {"answers": {"reading": reply}}}

        self.assertTrue(Decider(Fixed()).claims("It is done.", {"thread": "x"}).wrong)
        self.assertFalse(Decider(Fixed()).claims("It is done.", {"thread": None}).wrong)

    def test_a_check_that_could_not_answer_flags_nothing(self):
        for client in (Failing(TimeoutError("slow")), Failing(BillingStop("out"))):
            claim = Decider(client).claims("It is done.", {"thread": "x"})
            self.assertFalse(claim.wrong)
            self.assertTrue(claim.error)

    def test_an_answer_that_is_not_a_reading_flags_nothing(self):
        class Bad:
            def __init__(self, record):
                self.record = record

            def call(self, route, body, label=""):
                return self.record

        for record in ({"status": 500, "response": None},
                       {"status": 200, "response": {"answers": {}}},
                       {"status": 200, "response": {"answers": {"reading": {"choice": "unsupported",
                                                                             "probabilities": {"unsupported": 3}}}}},
                       {"status": 200, "response": {"answers": {"reading": {"choice": "bogus",
                                                                             "probabilities": {"bogus": 1.0}}}}}):
            with self.subTest(record=record):
                self.assertEqual(Decider(Bad(record)).claims("It is done.", {}).wrong, False)

    def test_the_bar_is_the_one_the_fixture_was_chosen_at(self):
        self.assertEqual(CLAIM_UNSUPPORTED_YES, 0.5)
        self.assertTrue(Claim(0.5, 0.1).wrong)
        self.assertFalse(Claim(0.49, 0.1).wrong)


if __name__ == "__main__":
    unittest.main()
