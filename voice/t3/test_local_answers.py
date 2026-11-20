"""Replay the local decider's recorded answers without a model server."""

import hashlib
import json
import unittest
from pathlib import Path

from voice.local_decider.client import D1Client
from voice.t3.acts import Act
from voice.t3.eval.pick_holdout import CASES as HELD, THREADS
from voice.t3.eval.record_picks import cases, listed
from voice.t3.eval.record_replies import CASES as REPLIES
from voice.t3.verify import Decider, pick_request, reading_request

FIXTURES = Path(__file__).parent / "fixtures"


def request_key(body):
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class Recorded:
    pick_sure = D1Client.pick_sure

    def __init__(self, rows):
        self.rows = rows

    def call(self, route, body, label=""):
        return {"status": 200, "response": {"answers": self.rows[request_key(body)]}}


class LocalAnswers(unittest.TestCase):
    def test_picks_never_act_on_the_wrong_thread(self):
        rows = load("local_thread_picks.json")
        self.assertEqual(len(rows), len(list(cases())))
        answers = {r["request_hash"]:
                   {"pick": r["answer"]} for r in rows}
        decider = Decider(Recorded(answers))
        for row in rows:
            threads = listed(row["listing"])
            picked = decider.picks(row["request"], threads, row["recent"])
            if picked.sure:
                self.assertIn(row["want"], ("sure", "leans"))
                self.assertEqual(next(t.title for t in threads if t.thread_id == picked.thread), row["about"])
        clear = [r for r in rows if r["want"] == "sure"]
        self.assertGreaterEqual(sum(decider.picks(r["request"], listed(r["listing"]), r["recent"]).sure
                                    for r in clear), 30)

    def test_holdout_never_acts_on_the_wrong_thread(self):
        rows = load("local_pick_holdout.json")
        self.assertEqual([(r["request"], r["want"], r["about"]) for r in rows], HELD)
        answers = {r["request_hash"]: {"pick": r["answer"]} for r in rows}
        decider = Decider(Recorded(answers))
        for row in rows:
            picked = decider.picks(row["request"], THREADS)
            if picked.sure:
                self.assertEqual(row["want"], "sure")
                self.assertEqual(next(t.title for t in THREADS if t.thread_id == picked.thread), row["about"])

    def test_hosted_holdout_never_acts_on_the_wrong_thread(self):
        rows = load("hosted_pick_holdout.json")
        self.assertEqual([(r["request"], r["want"], r["about"]) for r in rows], HELD)
        class Hosted:
            def call(self, route, body, label=""):
                row = next(r for r in rows if r["request_hash"] == request_key(body))
                return {"status": 200, "response": {"answers": {"pick": row["answer"]}}}

        decider = Decider(Hosted())
        for row in rows:
            picked = decider.picks(row["request"], THREADS)
            if picked.sure:
                self.assertEqual(row["want"], "sure")
                self.assertEqual(next(t.title for t in THREADS if t.thread_id == picked.thread), row["about"])

    def test_replies_do_not_accept_any_recorded_non_yes(self):
        rows = load("local_reply_reads.json")
        self.assertEqual(len(rows), len(REPLIES))
        answers = {r["request_hash"]: r["answers"] for r in rows}
        decider = Decider(Recorded(answers))
        for row in rows:
            if not row["must_act"]:
                self.assertFalse(decider.reads(Act.from_json(row["act"]), row["reply"], tuple(row["options"])).yes,
                                 row["reply"])


if __name__ == "__main__":
    unittest.main()
