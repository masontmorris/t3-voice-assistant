"""The reply reader on made-up replies the transcript marks as questions or that withdraw a request, on recorded hosted
d1.2 answers (voice/t3/eval/record_reply_marks.py). A yes that is not one starts or approves something, so no reply that
is not an unconditional yes may read as one; a yes the transcript marked as a question must."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from voice.t3.acts import Act
from voice.t3.eval.reply_marks import HELD_OUT, SEEN
from voice.t3.verify import Decider, reading_request

ROWS = json.loads((Path(__file__).parent / "fixtures" / "reply_marks.json").read_text(encoding="utf-8"))


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
        for split, cases in (("seen", SEEN), ("held_out", HELD_OUT)):
            rows = [(r["act"], r["reply"], r["group"], r["must_act"], r["takes_back"]) for r in ROWS if r["split"] == split]
            self.assertEqual(rows, [(a.to_json(), *rest) for a, *rest in cases])

    def test_no_reply_that_is_not_a_yes_reads_as_one(self):
        for row in ROWS:
            if not row["must_act"]:
                with self.subTest(row["reply"], split=row["split"], group=row["group"]):
                    self.assertFalse(read(row).yes)

    def test_a_yes_the_transcript_marked_as_a_question_is_taken(self):
        for split in ("seen", "held_out"):
            marked = [r for r in ROWS if r["group"] == "yes_marked" and r["split"] == split]
            taken = sum(read(r).yes for r in marked)
            self.assertGreaterEqual(taken, len(marked) - 2, (split, [r["reply"] for r in marked if not read(r).yes]))

    def test_a_hesitant_yes_is_taken(self):
        for row in (r for r in ROWS if r["group"] == "hesitant_yes"):
            with self.subTest(row["reply"]):
                self.assertTrue(read(row).yes)

    def test_a_no_to_a_thread_question_withdraws_only_when_it_takes_the_request_back(self):
        for row in (r for r in ROWS if r["takes_back"] is not None):
            with self.subTest(row["reply"], split=row["split"]):
                self.assertEqual(read(row).withdrawing, row["takes_back"])


if __name__ == "__main__":
    unittest.main()
