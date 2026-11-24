"""Whole thread requests, on recorded d1.2 answers (voice/t3/eval/record_whole_requests.py): one request cut at "and"
or a sentence end is one task, two requests in one breath stay two, and an instruction to a thread is a message to it,
not a question about it."""

from __future__ import annotations

import json
import unittest

from voice.t3.eval.record_whole_requests import CASES, FIXTURE, passes, replayed

RECORDED = json.loads(FIXTURE.read_text(encoding="utf-8"))
# Cases d1.2 does not route as the case wants in most samples. A stray "Yes, that one." with nothing asked is picked as a
# message at 0.29 to 0.51 but not read as acting (`acts_on`), so it goes as a lookup, which acts on nothing: as the code
# before this did, 5 of 5.
MISSES = {("Yes, that one.", "controls")}


class WholeRequests(unittest.TestCase):
    def test_every_case_was_recorded_on_this_code_and_on_the_code_before_it(self):
        cases = {(utterance, name) for _, utterance, _, _, _, name in CASES}
        self.assertEqual({(r["utterance"], r["set"]) for r in RECORDED["rows"]}, cases)
        self.assertEqual({(r["utterance"], r["set"]) for r in RECORDED["before"]}, cases)

    def test_each_case_routes_as_recorded_and_as_it_wants_in_most_samples(self):
        for row in RECORDED["rows"]:
            with self.subTest(row["utterance"], set=row["set"]):
                routed = replayed(RECORDED["answers"], row)
                self.assertEqual(routed, row["routed"])
                most = sum(passes(row["want"], r) for r in routed) > len(routed) / 2
                self.assertEqual(most, (row["utterance"], row["set"]) not in MISSES, routed)


if __name__ == "__main__":
    unittest.main()
