"""The free-reply check on the local d1-2B's recorded answers (voice/t3/fixtures/claims.json, made-up cases): a reply that
says something T3 does not support is flagged, and a supported reply and a chat reply are not. A decider that cannot
answer never flags one."""

from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

from voice.liquid_decider.client import BillingStop
from voice.t3.eval.claim_cases import HELD_OUT, NO_THREAD, SEEN
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

    def test_with_no_thread_in_view_a_claimed_start_or_send_is_flagged(self):
        """The cases of claim_cases.NO_THREAD, recorded once (the local d1-2B, GPU): 7 of 8 replies that claim an
        action are flagged, the missed one is an approval at 0.48, and one of 8 other replies is, an offer to start a
        thread (0.72)."""
        rows = json.loads((Path(__file__).parent / "fixtures" / "claims_no_thread.json").read_text(encoding="utf-8"))
        self.assertEqual([(r["name"], r["want"]) for r in rows], [(n, w) for n, _, _, w in NO_THREAD])
        recorded = Recorded()
        recorded.answers = {row["request_hash"]: row["answers"] for row in rows}
        decider = Decider(recorded)
        flagged = {name: decider.claims(reply, facts).wrong for name, reply, facts, _ in NO_THREAD}
        wrong = [name for name, _, _, want in NO_THREAD if want == "wrong"]
        self.assertEqual(sorted(n for n in wrong if flagged[n]), sorted(set(wrong) - {"approved"}))
        self.assertEqual([n for n, _, _, want in NO_THREAD if want != "wrong" and flagged[n]], ["offer"])

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
