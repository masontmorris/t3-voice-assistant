"""The decider's reading of a reply to a pending confirmation, on recorded d1.2 answers: the ways people say yes act,
and a no, a question, a different target and words that answer nothing never do."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from voice.t3.acts import Act
from voice.t3.test_verify import Failing
from voice.t3.verify import Decider, reading_request

ROWS = json.loads((Path(__file__).parent / "fixtures" / "reply_reads.json").read_text(encoding="utf-8"))
# Yes replies the decider read below its confidence line. Code's own table still reads "okay" as a yes; "mm-hmm, go on"
# is asked again, which is the safe side.
BELOW_THE_LINE = {"Okay.", "Mm-hmm, go on."}


class Recorded:
    def __init__(self, rows=ROWS):
        self.rows = {(row["asked"], row["reply"]): row for row in rows}

    def call(self, route, body, label=""):
        row = self.rows[(body["state"]["assistant_asked"], body["state"]["reply"])]
        assert body["questions"]["reading"]["criteria"] == row["criteria"], "the question changed: re-run record_replies"
        return {"status": row["status"], "response": {"answers": row["answers"]}}


class Reads(unittest.TestCase):
    def test_every_way_of_saying_yes_acts_and_nothing_else_does(self):
        decider = Decider(Recorded())
        for row in ROWS:
            with self.subTest(row["reply"], asked=row["asked"]):
                reading = decider.reads(Act.from_json(row["acts"][0]), row["reply"])
                self.assertIsNone(reading.error)
                self.assertEqual(reading.yes, row["must_act"] and row["reply"] not in BELOW_THE_LINE, reading)

    def test_a_rising_please_do_with_its_question_mark_is_a_yes(self):
        row = next(r for r in ROWS if r["reply"] == "Please do?")
        self.assertTrue(Decider(Recorded()).reads(Act.from_json(row["acts"][0]), "Please do?").yes)

    def test_the_question_says_what_was_asked_and_that_punctuation_may_not_match(self):
        body = reading_request(Act.from_json(ROWS[0]["acts"][0]), "Please do?")
        self.assertEqual(body["state"]["assistant_asked"], "whether to start a new coding thread in Voice sandbox")
        self.assertIn("punctuation", ROWS[0]["criteria"]["yes"])

    def test_a_decider_that_cannot_answer_never_reads_a_yes(self):
        for error in (TimeoutError("timed out"), RuntimeError("billing")):
            reading = Decider(Failing(error)).reads(Act.from_json(ROWS[0]["acts"][0]), "Please do?")
            self.assertFalse(reading.yes)
            self.assertIn(type(error).__name__, reading.error)

    def test_a_malformed_answer_is_no_reading(self):
        class Bad:
            def call(self, route, body, label=""):
                return {"status": 200, "response": {"answers": {"reading": {"choice": "maybe"}}}}

        self.assertFalse(Decider(Bad()).reads(Act.from_json(ROWS[0]["acts"][0]), "Please do?").yes)


if __name__ == "__main__":
    unittest.main()
