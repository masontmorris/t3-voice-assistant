"""The decider's reading of a reply to a pending confirmation, on recorded d1.2 answers: the ways people say yes act,
and a no, a question, a different target, a yes with a condition or a change, and words that answer nothing never do."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from voice.t3.acts import Act
from voice.t3.test_verify import Failing
from voice.t3.verify import Decider, judge_reading, reading_request

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "reply_reads.json").read_text(encoding="utf-8"))
ROWS = FIXTURE["rows"]
# Yes replies the decider read below its confidence line. Code's own table still reads "okay" and "go ahead" as yes;
# the others are asked again, which is the safe side.
BELOW_THE_LINE = {"Okay.", "Uh-huh.", "Go ahead."}


class Recorded:
    """A decider client that answers each request from the fixture, and only one whose question it recorded."""

    def __init__(self, rows=ROWS):
        self.rows = {(json.dumps(row["act"]), row["reply"]): row for row in rows}

    def call(self, route, body, label=""):
        for row in self.rows.values():
            act = Act.from_json(row["act"])
            if reading_request(act, row["reply"]) == body:
                assert body["questions"]["reading"]["criteria"] == FIXTURE["criteria"], "re-run record_replies"
                return {"status": 200, "response": {"answers": {"reading": {
                    "choice": row["choice"], "probabilities": row["p"]}}}}
        raise KeyError(body["state"])


def acts_of(row):
    return Act.from_json(row["act"])


class Reads(unittest.TestCase):
    def test_every_way_of_saying_yes_acts_and_nothing_else_does(self):
        decider = Decider(Recorded())
        for row in ROWS:
            with self.subTest(row["reply"], act=row["act"]["kind"]):
                reading = decider.reads(acts_of(row), row["reply"])
                self.assertIsNone(reading.error)
                self.assertEqual(reading.yes, row["must_act"] and row["reply"] not in BELOW_THE_LINE, reading)

    def test_a_yes_that_changes_the_target_or_adds_a_condition_is_not_a_yes(self):
        conditional = [row for row in ROWS if row["reply"].startswith(("Yes, but", "Sure, in", "Yeah, but", "Okay, if",
                                                                         "Yes, as long", "Yes please, and"))]
        self.assertGreaterEqual(len(conditional), 7)
        decider = Decider(Recorded())
        for row in conditional:
            with self.subTest(row["reply"]):
                reading = decider.reads(acts_of(row), row["reply"])
                self.assertFalse(reading.yes, reading)
                self.assertEqual(reading.label, "other")

    def test_a_label_that_says_yes_with_a_real_share_of_other_or_question_is_not_a_yes(self):
        for shares in ({"yes": 0.8, "other": 0.3}, {"yes": 0.75, "question": 0.25}):
            reading = judge_reading({"reading": {"choice": "yes", "probabilities": shares}})
            self.assertEqual(reading.label, "yes")
            self.assertFalse(reading.yes, shares)
        self.assertTrue(judge_reading({"reading": {"choice": "yes", "probabilities": {"yes": 0.9, "other": 0.1}}}).yes)

    def test_a_rising_please_do_with_its_question_mark_is_a_yes(self):
        row = next(r for r in ROWS if r["reply"] == "Please do?")
        self.assertTrue(Decider(Recorded()).reads(acts_of(row), "Please do?").yes)

    def test_the_question_says_what_was_asked_and_that_the_yes_must_be_unconditional(self):
        body = reading_request(acts_of(ROWS[0]), "Please do?")
        self.assertEqual(body["state"]["assistant_asked"], "whether to start a new coding thread in Voice sandbox")
        self.assertIn("nothing added, changed or conditional", FIXTURE["criteria"]["yes"])

    def test_a_decider_that_cannot_answer_never_reads_a_yes(self):
        for error in (TimeoutError("timed out"), RuntimeError("billing")):
            reading = Decider(Failing(error)).reads(acts_of(ROWS[0]), "Please do?")
            self.assertFalse(reading.yes)
            self.assertIn(type(error).__name__, reading.error)

    def test_malformed_answers_are_no_reading_and_never_an_exception(self):
        for answer in ({"choice": ["yes"], "probabilities": {"yes": 0.9}},
                       {"choice": "maybe", "probabilities": {"yes": 0.9}},
                       {"choice": "yes", "probabilities": {"yes": "high"}},
                       {"choice": "yes", "probabilities": {"yes": float("nan")}},
                       {"choice": "yes", "probabilities": {"yes": 1.7}},
                       {"choice": "yes", "probabilities": {"yes": True}},
                       {"choice": "yes", "probabilities": {"sure": 0.9}},
                       {"choice": "yes", "probabilities": None}, {"choice": "yes"}, "yes", None):
            class Bad:
                def call(self, route, body, label="", answer=answer):
                    return {"status": 200, "response": {"answers": {"reading": answer}}}

            with self.subTest(answer=answer):
                reading = Decider(Bad()).reads(acts_of(ROWS[0]), "Please do?")
                self.assertFalse(reading.yes)
                self.assertIsNone(reading.label)
                self.assertIsNotNone(reading.error)


if __name__ == "__main__":
    unittest.main()
