"""The day and place a forecast runs for, on recorded d1.2 answers: a request that names a day, counts one from today,
follows an earlier forecast, is a request of its own or names none runs for the day and place its case says, and a day
past the forecast's range picks none. The cases and their recording are `voice/tools/eval/record_days.py`."""

from __future__ import annotations

import json
import unittest

from voice.tools.eval.record_days import CASES, FIXTURE, SAMPLES, Replay, passes, run_case

RECORDED = json.loads(FIXTURE.read_text(encoding="utf-8"))
AFTER = [row for row in RECORDED["rows"] if row["label"] == "after"]


class DayDecisions(unittest.TestCase):
    def test_every_case_was_recorded_for_every_sample(self):
        self.assertEqual(len(AFTER), SAMPLES * len(CASES))

    def test_each_recorded_sample_resolves_to_the_day_it_recorded(self):
        for row in AFTER:
            with self.subTest(row["utterance"], sample=row["sample"]):
                got = run_case(Replay(RECORDED["answers"], row["sample"]), row["category"], row["utterance"], row["before"],
                               row["sample"])
                self.assertEqual(got, row["got"])
                self.assertEqual(passes(row["place"], row["offset"], got), row["passes"])

    def test_a_forecast_the_decider_dispatched_runs_for_the_counted_or_followed_day(self):
        for category in ("in three days", "a week from today", "ten days from now", "follow-up, day only",
                         "follow-up, place only", "fresh request after a lookup"):
            rows = [row for row in AFTER if row["category"] == category and row["got"] is not None
                    and row["got"]["arguments"]["place"] is not None]
            asked = [row for row in AFTER if row["category"] == category]
            self.assertGreaterEqual(len(rows), len(asked) - 3, "all but a tool or place miss or two were read")
            self.assertTrue(all(row["passes"] for row in rows), category)

    def test_a_place_the_decider_read_is_its_whole_span_and_no_word_in_it_is_cut_out(self):
        for category in ("place with a day or month word", "two-word place"):
            rows = [row for row in AFTER if row["category"] == category]
            self.assertTrue(rows and all(row["passes"] for row in rows), category)

    def test_a_request_with_no_place_dispatches_no_lookup_with_one(self):
        rows = [row for row in AFTER if row["category"] == "no place stated"]
        self.assertTrue(rows and all(row["passes"] for row in rows))
        self.assertTrue(all(row["got"] is None or row["got"]["arguments"]["place"] is None for row in rows))

    def test_the_deciders_reading_of_an_answer_to_which_city_needs_no_word_list(self):
        for category in ("clarification, just the place", "clarification, not an answer"):
            rows = [row for row in AFTER if row["category"] == category]
            self.assertTrue(rows and all(row["passes"] for row in rows), category)
        filler = [row for row in AFTER if row["category"] == "clarification, with filler"]
        self.assertGreaterEqual(sum(row["passes"] for row in filler), len(filler) - 2)

    def test_a_day_that_worked_before_still_works(self):
        for category in ("today", "tomorrow", "weekday", "weekend", "no day"):
            for row in (r for r in AFTER if r["category"] == category and r["got"] is not None):
                with self.subTest(row["utterance"], sample=row["sample"]):
                    self.assertTrue(row["passes"])

    def test_the_cases_the_old_rules_got_wrong_are_the_deciders_now(self):
        before = [row for row in RECORDED["rows"] if row["label"] == "before"]
        for category in ("in three days", "a week from today", "ten days from now", "fresh request after a lookup",
                         "clarification, with filler"):
            old = sum(row["passes"] for row in before if row["category"] == category)
            new = sum(row["passes"] for row in AFTER if row["category"] == category)
            self.assertGreater(new, old, category)

if __name__ == "__main__":
    unittest.main()
