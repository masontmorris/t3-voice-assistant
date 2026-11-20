"""Replay the local decider's recorded answers without a model server."""

import hashlib
import json
import unittest
from pathlib import Path

from voice.local_decider.client import D1Client
from voice.t3.acts import Act
from voice.t3.eval import fresh_holdout as fresh
from voice.t3.eval.pick_holdout import CASES as HELD, THREADS
from voice.t3.eval.record_picks import cases, listed
from voice.t3.eval.record_replies import CASES as REPLIES
from voice.t3.verify import NONE, Decider

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
        self.assertGreaterEqual(sum(decider.picks(r["request"], THREADS).sure for r in rows if r["want"] == "sure"), 19)

    def test_a_vague_reference_with_nothing_discussed_is_asked_about_not_called_unlisted(self):
        vague = [(r, listed(r["listing"])) for r in load("local_thread_picks.json")
                 if r["want"] == "asks" and r["recent"] is None and r["about"] is None]
        vague += [(r, THREADS) for r in load("local_pick_holdout.json") if r["want"] == "asks"]
        vague += [(r, fresh.THREADS) for r in load("local_fresh_picks.json") if r["want"] == "asks" and r["recent"] is None]
        self.assertEqual(len(vague), 24)
        decider = Decider(Recorded({r["request_hash"]: {"pick": r["answer"]} for r, _ in vague}))
        for row, threads in vague:
            picked = decider.picks(row["request"], threads)
            self.assertNotEqual(picked.choice, NONE, row["request"])
            self.assertIsNone(picked.thread, row["request"])

    def test_fresh_picks_never_act_on_the_wrong_thread(self):
        for name, sure in (("local_fresh_picks.json", D1Client.pick_sure), ("hosted_fresh_picks.json", None)):
            rows = load(name)
            self.assertEqual([(r["request"], r["recent"], r["want"], r["about"]) for r in rows], fresh.PICKS)
            client = Recorded({r["request_hash"]: {"pick": r["answer"]} for r in rows})
            if sure is None:
                client.pick_sure = 0.9
            decider = Decider(client)
            for row in rows:
                picked = decider.picks(row["request"], fresh.THREADS, row["recent"])
                if picked.sure:
                    self.assertIn(row["want"], ("sure", "leans"), row["request"])
                    self.assertEqual(next(t.title for t in fresh.THREADS if t.thread_id == picked.thread),
                                     row["about"])

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

    def test_replies_act_on_every_recorded_yes_and_nothing_else(self):
        rows = load("local_reply_reads.json")
        self.assertEqual(len(rows), len(REPLIES))
        answers = {r["request_hash"]: r["answers"] for r in rows}
        decider = Decider(Recorded(answers))
        for row in rows:
            reading = decider.reads(Act.from_json(row["act"]), row["reply"], tuple(row["options"]))
            self.assertEqual(reading.yes, row["must_act"], row["reply"])

    def test_fresh_replies_never_accept_a_non_yes(self):
        for name in ("local_fresh_replies.json", "hosted_fresh_replies.json"):
            rows = load(name)
            self.assertEqual([r["reply"] for r in rows], [reply for _, reply, *_ in fresh.REPLIES])
            decider = Decider(Recorded({r["request_hash"]: r["answers"] for r in rows}))
            accepted = [decider.reads(Act.from_json(r["act"]), r["reply"], tuple(r["options"])).yes for r in rows]
            self.assertFalse(any(yes for yes, r in zip(accepted, rows) if not r["must_act"]), name)
            self.assertGreaterEqual(sum(yes for yes, r in zip(accepted, rows) if r["must_act"]), 15, name)


if __name__ == "__main__":
    unittest.main()
