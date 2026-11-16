import unittest

from voice.prototype.report import short_failure, wer


class WerTest(unittest.TestCase):
    def test_case_punctuation_and_digits_do_not_count_as_errors(self):
        self.assertEqual(wer("Set a timer for ten minutes, no wait, make that fifteen minutes.",
                             "Set a timer for 10 minutes. No wait. Make that 15 minutes."), 0.0)
        self.assertEqual(wer("the fare is two hundred forty", "the fare is 240"), 0.0)

    def test_a_split_word_is_a_substitution_and_an_insertion(self):
        self.assertAlmostEqual(wer("run the dishwasher", "run the dish washer"), 2 / 3)

    def test_a_missing_word_is_a_deletion(self):
        self.assertAlmostEqual(wer("what time is it", "what time it"), 1 / 4)


class ShortFailureTest(unittest.TestCase):
    def test_failures_lose_the_transcript_text(self):
        self.assertEqual(short_failure("task_3: unexpected dispatched task \"turn the lights on\""),
                         "extra dispatched task")
        self.assertEqual(short_failure("T2: no task matches ['lights']"), "T2 missing")
        self.assertEqual(short_failure("T1: status proposed, expected one of ['dispatched']"), "T1 status proposed")


if __name__ == "__main__":
    unittest.main()
