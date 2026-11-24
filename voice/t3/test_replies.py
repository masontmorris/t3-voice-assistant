"""The decider's reading of a reply to a pending confirmation, on recorded d1.2 answers: the ways people say yes act,
and a no, a question, a different target, a yes with a condition or a change, and words that answer nothing never do."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from voice.t3.acts import Act
from voice.t3.test_verify import Failing
from voice.t3.verify import ELSEWHERE, Decider, judge_reading, reading_request

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "reply_reads.json").read_text(encoding="utf-8"))
ROWS = FIXTURE["rows"]
# Replies d1.2 misreads. The first names no project and the decider says it keeps the proposed one; the second points
# away from it at 0.67, under the 0.7 a change of target needs. Neither is a yes, so nothing starts, and the person has
# to name the project.
TARGET_MISREADS = {"Yes, but in the other project.", "No, the other one."}
DECLINES = {"No.", "No, don't.", "Nope.", "No, cancel that.", "No, leave it running.", "Don't stop it.",
            "Hold on, not yet.", "Not that one."}


class Recorded:
    """A decider client that answers each request from the fixture, and only one whose question it recorded."""

    def __init__(self, rows=ROWS):
        self.rows = {(json.dumps(row["act"]), row["reply"], tuple(row.get("options", ()))): row for row in rows}

    def call(self, route, body, label=""):
        for row in self.rows.values():
            act = Act.from_json(row["act"])
            if reading_request(act, row["reply"], tuple(row.get("options", ()))) == body:
                assert body["questions"]["reading"]["criteria"] == FIXTURE["criteria"], "re-run record_replies"
                answers = {"reading": {"choice": row["choice"], "probabilities": row["p"]},
                           **{name: {"type": "noul", "noul": share} for name, share in row["noul"].items()}}
                if row.get("target_answer") is not None:
                    answers["target"] = row["target_answer"]
                return {"status": 200, "response": {"answers": answers}}
        raise KeyError(body["state"])


def answered(shares, agrees=0.9, asks=0.1, withdraws=None, adds=0.1, closes=0.1):
    """A decider's answers: the choice's shares with the label that has the most, and the yes-or-no questions' shares."""
    whole = {label: 0.0 for label in ("yes", "no", "question", "other", "unrelated")} | shares
    answers = {"reading": {"choice": max(shares, key=shares.get), "probabilities": whole},
               "agrees": {"type": "noul", "noul": agrees}, "asks": {"type": "noul", "noul": asks},
               "adds": {"type": "noul", "noul": adds}, "closes": {"type": "noul", "noul": closes}}
    if withdraws is not None:
        answers["withdraws"] = {"type": "noul", "noul": withdraws}
    return answers


def acts_of(row):
    return Act.from_json(row["act"])


class Reads(unittest.TestCase):
    def test_every_way_of_saying_yes_acts_and_nothing_else_does(self):
        decider = Decider(Recorded())
        for row in ROWS:
            with self.subTest(row["reply"], act=row["act"]["kind"]):
                reading = decider.reads(acts_of(row), row["reply"], tuple(row.get("options", ())))
                self.assertIsNone(reading.error)
                if row["act"]["kind"] != "ask_which":  # a which-question has no yes: its reply names one
                    self.assertEqual(reading.yes, row["must_act"], reading)
                if row["reply"] in DECLINES:
                    self.assertEqual(reading.kind, "no", reading)

    def test_a_reply_that_changes_the_target_says_which_or_that_it_changed(self):
        decider = Decider(Recorded())
        rows = [row for row in ROWS if row.get("options") and row["reply"] not in TARGET_MISREADS]
        self.assertGreaterEqual(len(rows), 20)
        for row in rows:
            options, want = tuple(row["options"]), row["expected_target"]
            with self.subTest(row["reply"], options=options):
                reading = decider.reads(acts_of(row), row["reply"], options)
                if want in options and reading.target is None:
                    # Pointed at, not named: with one alternative, changing elsewhere can only mean that one.
                    self.assertTrue(reading.elsewhere and len(options) == 1, reading)
                else:
                    self.assertEqual((reading.target, reading.elsewhere),
                                     (want if want in options else None, want == ELSEWHERE), reading)

    def test_a_yes_that_changes_the_target_or_adds_a_condition_is_not_a_yes(self):
        conditional = [row for row in ROWS if row["reply"].startswith(("Yes, but", "Sure, in", "Yeah, but", "Okay, if",
                                                                         "Yes, as long", "Yes please, and"))]
        self.assertGreaterEqual(len(conditional), 7)
        decider = Decider(Recorded())
        for row in conditional:
            with self.subTest(row["reply"]):
                reading = decider.reads(acts_of(row), row["reply"], tuple(row.get("options", ())))
                self.assertFalse(reading.yes, reading)
                self.assertEqual(reading.label, "other")

    def test_a_yes_with_a_real_share_of_a_no_or_another_target_is_not_a_yes(self):
        for shares in ({"yes": 0.7, "other": 0.3}, {"yes": 0.7, "no": 0.3}, {"yes": 0.59, "other": 0.21, "question": 0.2}):
            reading = judge_reading(answered(shares))
            self.assertFalse(reading.yes, shares)
        self.assertTrue(judge_reading(answered({"yes": 0.9, "other": 0.1})).yes)

    def test_a_yes_the_transcript_marked_as_a_question_is_a_yes_and_a_question_in_words_is_not(self):
        # The numbers are the hosted d1.2's on "Yeah?" and on "Yeah, but which project?".
        split = {"yes": 0.48, "question": 0.33, "unrelated": 0.11, "no": 0.05, "other": 0.03}
        self.assertTrue(judge_reading(answered(split, agrees=0.77, asks=0.38)).yes)
        self.assertFalse(judge_reading(answered({"question": 0.43, "other": 0.48, "yes": 0.08, "no": 0.01},
                                                agrees=0.6, asks=0.97)).yes)

    def test_a_reading_missing_one_of_the_yes_questions_is_not_a_yes(self):
        for left_out in ("agrees", "asks", "adds", "closes"):
            answers = answered({"yes": 0.9, "other": 0.1})
            del answers[left_out]
            self.assertFalse(judge_reading(answers).yes, left_out)

    def test_a_no_withdraws_only_when_the_reply_takes_the_request_back(self):
        no = {"no": 0.8, "other": 0.1, "unrelated": 0.1}
        self.assertTrue(judge_reading(answered(no, agrees=0.02, withdraws=0.93)).withdrawing)
        self.assertFalse(judge_reading(answered(no, agrees=0.02, withdraws=0.1)).withdrawing)
        self.assertFalse(judge_reading(answered(no, agrees=0.02)).withdrawing)
        self.assertTrue(judge_reading(answered({"unrelated": 0.9, "no": 0.1}, agrees=0.02, withdraws=0.93)).withdrawing)
        for label in ("yes", "question", "other"):
            self.assertFalse(judge_reading(answered({label: 0.9, "no": 0.1}, agrees=0.5, withdraws=0.93)).withdrawing)

    def test_a_yes_read_beside_a_change_of_target_is_not_a_yes(self):
        yes = {"yes": 0.95, "other": 0.05}
        for choice in ("option_1", "elsewhere"):
            target = {"choice": choice, "probabilities": {choice: 0.9, "keeps": 0.1}}
            self.assertFalse(judge_reading({**answered(yes), "target": target}, ("Voice server",)).yes, choice)
        keeps = {"choice": "keeps", "probabilities": {"keeps": 0.9, "option_1": 0.1}}
        self.assertTrue(judge_reading({**answered(yes), "target": keeps}, ("Voice server",)).yes)

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
                       {"choice": "yes", "probabilities": {"yes": 0.51, "no": 0.99}},
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
