"""Consent to a confirmed action, on recorded hosted d1.2 answers (voice/t3/eval/record_consent.py): a thanks, a goodbye
or a note of what was said never authorises starting, sending, stopping, approving or denying, and the ways people say
yes, a bare okay among them, still do."""

from __future__ import annotations

import json
import unittest

from voice.t3.eval.record_consent import ACTS, CASES, FIXTURE, key
from voice.t3.verify import judge_reading, reading_request

RECORDED = json.loads(FIXTURE.read_text(encoding="utf-8"))


class Consent(unittest.TestCase):
    def test_every_case_was_recorded_on_this_reader_and_on_the_one_before_it(self):
        cases = {(kind, reply, held) for kind, reply, _, held in CASES}
        for section in ("rows", "before"):
            self.assertEqual({(r["act"], r["reply"], r["held"]) for r in RECORDED[section]}, cases, section)
        for row in RECORDED["rows"]:
            self.assertEqual(row["request"], key(reading_request(ACTS[row["act"]], row["reply"])),
                             "re-run voice.t3.eval.record_consent")

    def test_no_thanks_goodbye_or_note_of_what_was_said_authorises_anything(self):
        for row in RECORDED["rows"]:
            if not row["must_act"]:
                with self.subTest(row["reply"], act=row["act"], held=row["held"]):
                    self.assertEqual([judge_reading(a).yes for a in row["answers"]], [False] * len(row["answers"]))

    def test_every_way_of_saying_yes_authorises_in_most_samples(self):
        for row in RECORDED["rows"]:
            if row["must_act"]:
                with self.subTest(row["reply"], act=row["act"], held=row["held"]):
                    taken = [judge_reading(a).yes for a in row["answers"]]
                    self.assertEqual(taken, row["yes"])
                    self.assertGreater(sum(taken), len(taken) / 2, taken)


if __name__ == "__main__":
    unittest.main()
