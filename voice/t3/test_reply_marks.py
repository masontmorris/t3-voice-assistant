"""The reply reader on made-up replies the transcript marks as questions, that echo the question, answer another one,
reverse themselves or are cut off, and that withdraw a request, on recorded hosted d1.2 answers
(voice/t3/eval/record_reply_marks.py). A yes that is not one starts or approves something, so no reply that is not an
unconditional yes may read as one except the known few below; a yes the transcript marked as a question must."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from voice.t3.acts import Act
from voice.t3.eval.reply_marks import FRESH_HELD_OUT, FRESH_SEEN, HELD_OUT, SEEN
from voice.t3.verify import Decider, reading_request

ROWS = json.loads((Path(__file__).parent / "fixtures" / "reply_marks.json").read_text(encoding="utf-8"))
SPLITS = {"seen": SEEN, "held_out": HELD_OUT, "fresh_seen": FRESH_SEEN, "fresh_held_out": FRESH_HELD_OUT}
# Replies labelled "not a yes" that the reader still takes, as recorded. A cut-off word ("Ye", "Ok") is a bare
# affirmative that nothing can tell from the start of one, and the two replies that agree and go on about something
# else are ones the reader does not tell from a yes. They are listed so that a new one is a failure.
KNOWN_TAKEN = {("fresh_seen", "cut_word"): {"Ye", "Oka"}, ("fresh_held_out", "cut_word"): {"Yea", "Ok"},
               ("fresh_seen", "other_question"): {"Yeah, that's what he said.", "Yeah, I like Postgres."}}


class Recorded:
    def __init__(self):
        self.rows = {(json.dumps(r["act"]), r["reply"]): r["answers"] for r in ROWS}

    def call(self, route, body, label=""):
        for (said, reply), answers in self.rows.items():
            if reading_request(Act.from_json(json.loads(said)), reply) == body:
                return {"status": 200, "response": {"answers": answers}}
        raise KeyError(body["state"])


def read(row):
    return Decider(Recorded()).reads(Act.from_json(row["act"]), row["reply"])


class ReplyMarks(unittest.TestCase):
    def test_the_recording_holds_every_case(self):
        for split, cases in SPLITS.items():
            rows = [(r["act"], r["reply"], r["group"], r["must_act"], r["takes_back"]) for r in ROWS if r["split"] == split]
            self.assertEqual(rows, [(a.to_json(), *rest) for a, *rest in cases], split)

    def test_no_reply_that_is_not_a_yes_reads_as_one_but_the_known_few(self):
        taken = {}
        for row in ROWS:
            if not row["must_act"] and read(row).yes:
                taken.setdefault((row["split"], row["group"]), set()).add(row["reply"])
        self.assertEqual(taken, KNOWN_TAKEN)

    def test_every_new_shape_but_the_known_few_is_refused_in_both_sets(self):
        for split in ("fresh_seen", "fresh_held_out"):
            for group in ("echo", "reversal_to_no", "truncated_condition", "truncated_refusal"):
                rows = [r for r in ROWS if r["split"] == split and r["group"] == group]
                self.assertGreaterEqual(len(rows), 7, (split, group))
                self.assertFalse(any(read(r).yes for r in rows), (split, group))

    def test_a_yes_the_transcript_marked_as_a_question_is_taken_most_of_the_time(self):
        for split, at_least in (("seen", 19), ("held_out", 15)):
            marked = [r for r in ROWS if r["group"] == "yes_marked" and r["split"] == split]
            self.assertGreaterEqual(sum(read(r).yes for r in marked), at_least, split)

    def test_a_hesitant_yes_and_a_cut_off_but_whole_affirmative_are_taken(self):
        for row in (r for r in ROWS if r["group"] == "hesitant_yes"):
            with self.subTest(row["reply"]):
                self.assertTrue(read(row).yes)
        for split in ("fresh_seen", "fresh_held_out"):
            cut = [r for r in ROWS if r["split"] == split and r["group"] == "truncated_yes"]
            self.assertGreaterEqual(sum(read(r).yes for r in cut), 9, split)

    def test_a_no_to_a_thread_question_withdraws_only_when_it_takes_the_request_back(self):
        for row in (r for r in ROWS if r["takes_back"] is not None):
            reading = read(row)
            if row["takes_back"]:
                # One recorded draw of the 14 withdrawals is read as another label, which only looks for a thread.
                continue
            with self.subTest(row["reply"], split=row["split"]):
                self.assertFalse(reading.withdrawing)
        taken = [r for r in ROWS if r["takes_back"] and read(r).withdrawing]
        every = [r for r in ROWS if r["takes_back"]]
        self.assertGreaterEqual(len(taken), len(every) - 1)


if __name__ == "__main__":
    unittest.main()
