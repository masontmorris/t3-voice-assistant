"""The day a forecast runs for, on recorded d1.2 answers: a request that names a day, counts one from today, follows
an earlier forecast or names none runs for the day its case says, and a day past the forecast's range picks none.
The cases and their recording are `voice/tools/eval/record_days.py`."""

from __future__ import annotations

import json
import unittest

from voice.tools.eval.record_days import CASES, FIXTURE, SAMPLES, Replay, decide, passes

RECORDED = json.loads(FIXTURE.read_text(encoding="utf-8"))
AFTER = [row for row in RECORDED["rows"] if row["label"] == "after"]


class DayDecisions(unittest.TestCase):
    def test_every_case_was_recorded_for_every_sample(self):
        self.assertEqual(len(AFTER), SAMPLES * len(CASES))

    def test_each_recorded_sample_resolves_to_the_day_it_recorded(self):
        for row in AFTER:
            with self.subTest(row["utterance"], sample=row["sample"]):
                got = decide(Replay(RECORDED["answers"], row["sample"]), row["utterance"], row["before"], row["sample"])
                self.assertEqual(got, row["got"])
                self.assertEqual(passes(row["place"], row["offset"], got), row["passes"])

    def test_a_forecast_the_decider_dispatched_runs_for_the_counted_or_followed_day(self):
        for category in ("in three days", "a week from today", "ten days from now", "follow-up, day only",
                         "follow-up, place only"):
            rows = [row for row in AFTER if row["category"] == category and row["got"] is not None]
            self.assertGreaterEqual(len(rows), 17, "all but a tool miss were dispatched, of 6 cases in 3 samples")
            self.assertTrue(all(row["passes"] for row in rows), category)

    def test_a_day_that_worked_before_still_works(self):
        for category in ("today", "tomorrow", "weekday", "weekend", "no day"):
            for row in (r for r in AFTER if r["category"] == category and r["got"] is not None):
                with self.subTest(row["utterance"], sample=row["sample"]):
                    self.assertTrue(row["passes"])


if __name__ == "__main__":
    unittest.main()
