"""The decider's choice among the options of a question a thread asked, on recorded d1.2 answers: an option named by
name, by place or in other words is chosen and sure, the caller's own words are an answer only where the question takes
them, and a reply that fits no option, or answers nothing, is never turned into one."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from voice.t3.eval.record_answers import HELD, LATE, QUESTIONS, SEEN, request_for
from voice.t3.test_verify import Failing
from voice.t3.verify import (ANSWER_SURE, NO_ANSWER, OWN_WORDS, UNCLEAR, WORDS_SURE, Chosen, Decider, choice_request,
                             judge_choice)

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "answer_choices.json").read_text(encoding="utf-8"))
ROWS = FIXTURE["rows"]


class Recorded:
    """A decider client that answers each request from the fixture, and only one whose question it recorded."""

    def call(self, route, body, label=""):
        for row in ROWS:
            if request_for(row["question"], row["reply"]) == body:
                return {"status": 200, "response": {"answers": {"choice": {"choice": row["choice"],
                                                                           "probabilities": row["p"]}}}}
        raise KeyError(body["state"])


def chosen(row) -> Chosen:
    question, options, own_words = QUESTIONS[row["question"]]
    return Decider(Recorded()).chooses(question, options, row["reply"], own_words)


class Choices(unittest.TestCase):
    def test_every_recorded_case_is_one_the_eval_makes(self):
        self.assertEqual([(r["question"], r["reply"], r["want"]) for r in ROWS], [*SEEN, *HELD, *LATE])

    def test_each_reply_is_chosen_as_its_case_says(self):
        for row in ROWS:
            with self.subTest(row["question"], reply=row["reply"], set=row["set"]):
                picked = chosen(row)
                self.assertIsNone(picked.error)
                want = row["want"]
                if want.startswith("option:"):
                    self.assertEqual(picked.option, int(want[len("option:"):]) - 1, picked)
                    self.assertFalse(picked.own_words)
                if want == "own_words":
                    self.assertTrue(picked.own_words, picked)
                    self.assertIsNone(picked.option)
                if want == "either":
                    self.assertTrue(picked.option is not None or picked.own_words, picked)
                if want == "none":
                    self.assertTrue(picked.declined, picked)
                if want == "not option":
                    self.assertEqual((picked.option, picked.own_words), (None, False), picked)

    def test_no_reply_is_chosen_as_an_option_or_as_words_it_is_not(self):
        """The one way a choice acts unasked: a sure option, or sure words, for a reply that is neither."""
        for row in ROWS:
            with self.subTest(row["question"], reply=row["reply"]):
                picked = chosen(row)
                if picked.option is not None and row["want"] != "either":
                    self.assertEqual(f"option:{picked.option + 1}", row["want"], picked)
                if picked.own_words and row["want"] != "either":
                    self.assertEqual(row["want"], "own_words", picked)

    def test_the_request_reads_the_options_out_in_order_with_their_descriptions(self):
        question, options, own_words = QUESTIONS["naming"]
        body = choice_request(question, options, "By date.", own_words)
        criteria = body["questions"]["choice"]["criteria"]
        self.assertEqual(list(criteria), ["option_01", "option_02", OWN_WORDS, NO_ANSWER, UNCLEAR])
        self.assertEqual(criteria["option_01"], "It picks 'By date' (The file is named for the day it was made.), "
                                                "number 1 of the 2 read out.")
        self.assertEqual(body["state"]["options"], ["By date", "By project name"])
        self.assertEqual(body["state"]["reply"], "By date.")

    def test_a_question_that_takes_no_words_offers_none(self):
        question, options, _ = QUESTIONS["runner"]
        criteria = choice_request(question, options, "Jest.", False)["questions"]["choice"]["criteria"]
        self.assertNotIn(OWN_WORDS, criteria)

    def test_a_label_that_was_not_offered_is_no_choice_at_all(self):
        for answers in (None, {}, {"choice": {"choice": "option_09", "probabilities": {"option_09": 0.9}}},
                        {"choice": {"choice": OWN_WORDS, "probabilities": {OWN_WORDS: 0.9}}},
                        {"choice": {"choice": "option_01", "probabilities": {"option_01": "0.9"}}},
                        {"choice": {"choice": "option_01", "probabilities": {"option_01": 0.9, "option_02": 0.9}}}):
            with self.subTest(answers=answers):
                picked = judge_choice(answers, 2, own_words=False)
                self.assertEqual((picked.choice, picked.option, picked.own_words, picked.declined), (None, None, False, False))

    def test_a_lean_short_of_the_bar_is_never_sure(self):
        lean = Chosen("option_01", {"option_01": ANSWER_SURE - 0.01})
        self.assertEqual((lean.option, lean.own_words), (None, False))
        self.assertEqual(Chosen(OWN_WORDS, {OWN_WORDS: WORDS_SURE - 0.01}).own_words, False)
        self.assertEqual(Chosen(OWN_WORDS, {OWN_WORDS: WORDS_SURE}).own_words, True)
        self.assertEqual(Chosen("option_02", {"option_02": ANSWER_SURE}).option, 1)

    def test_an_error_chooses_nothing(self):
        picked = Decider(Failing(TimeoutError("timed out"))).chooses("Which?", (("A", ""), ("B", "")), "A.")
        self.assertEqual((picked.choice, picked.option, picked.declined), (None, None, False))
        self.assertIn("TimeoutError", picked.error)


if __name__ == "__main__":
    unittest.main()
