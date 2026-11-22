"""The decider's pick of a thread, on recorded d1.2 answers: a thread named plainly is picked and is sure, one named
loosely is picked, words that fit several are asked about by the threads they fit, and a thread that is not listed is
never turned into one that is."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from voice.t3.eval.record_picks import LISTINGS, listed
from voice.t3.test_verify import Failing
from voice.t3.verify import NONE, UNCLEAR, Decider, Listed, Picked, judge_pick, pick_request

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "thread_picks.json").read_text(encoding="utf-8"))
ROWS = FIXTURE["rows"]
# Requests d1.2 does not place as the case wants, each with the threads it offers instead. "That one" gives the thread
# last spoken about 0.3 to 0.4 (0.68 in an earlier recording), under what picks it. It is still the only thread
# offered, so the voice confirms it, as it would have.
MISSES = {("And that one?", "long"): ["Upgrade the websocket library"],
          ("And that one?", "short"): ["Upgrade the websocket library"]}


class Recorded:
    """A decider client that answers each request from the fixture, and only one whose question it recorded."""

    def call(self, route, body, label=""):
        for row in ROWS:
            if pick_request(row["request"], listed(row["listing"]), row["recent"]) == body:
                assert body["questions"]["pick"] == FIXTURE["criteria"][row["listing"]], "re-run record_picks"
                return {"status": 200, "response": {"answers": {"pick": {"choice": row["choice"],
                                                                         "probabilities": row["p"]}}}}
        raise KeyError(body["state"])


def placed(row) -> tuple[Picked, dict[str, str]]:
    threads = listed(row["listing"])
    return Decider(Recorded()).picks(row["request"], threads, row["recent"]), {t.thread_id: t.title for t in threads}


class Picks(unittest.TestCase):
    def test_each_request_is_placed_as_its_case_says(self):
        self.assertGreaterEqual(len(ROWS), 80)
        for row in ROWS:
            with self.subTest(row["request"], listing=row["listing"], held=row["held"]):
                picked, titles = placed(row)
                self.assertIsNone(picked.error)
                if (row["request"], row["listing"]) in MISSES:
                    self.assertEqual(([titles[t] for t in picked.candidates], picked.thread),
                                     (MISSES[row["request"], row["listing"]], None), picked)
                    continue
                thread = titles.get(picked.thread)
                if row["want"] in ("sure", "leans"):
                    self.assertEqual(thread, row["about"], picked)
                if row["want"] == "sure":
                    self.assertTrue(picked.sure, picked)
                if row["want"] == "none":
                    self.assertEqual(picked.choice, NONE, picked)
                if row["want"] == "asks":
                    self.assertIsNone(thread, picked)
                    self.assertNotEqual(picked.choice, NONE, picked)
                    offered = [titles[t] for t in picked.candidates]
                    if row["about"] and row["listing"] == "short":
                        self.assertGreaterEqual(len(offered), 2, picked)
                        self.assertLessEqual(set(offered), set(row["about"]), picked)

    def test_no_request_is_sure_of_a_thread_it_does_not_mean(self):
        """The one way a pick acts unasked. This holds for every row, the known misses included."""
        for row in ROWS:
            with self.subTest(row["request"], listing=row["listing"]):
                picked, titles = placed(row)
                if picked.sure:
                    self.assertIn(row["want"], ("sure", "leans"), picked)
                    self.assertEqual(titles[picked.thread], row["about"], picked)

    def test_the_request_lists_each_thread_with_its_project_and_state(self):
        body = pick_request("How is it going?", (Listed("a", "Add a clamp helper", "Voice sandbox", "waiting_on_you"),),
                            "Add a clamp helper")
        question = body["questions"]["pick"]
        self.assertEqual(question["criteria"]["thread_01"], "'Add a clamp helper' in the project 'Voice sandbox', "
                                                            "waiting on you.")
        self.assertEqual(set(question["criteria"]), {"thread_01", NONE, UNCLEAR})
        self.assertEqual(body["state"]["last_discussed"], "Add a clamp helper")
        threads = (Listed("a", "Add a clamp helper", "Voice sandbox", "working"),
                   Listed("b", "Add a clamp helper", "Docs site", "finished"))
        on_call = pick_request("How is it going?", threads, None, call_thread="b")["questions"]["pick"]["criteria"]
        self.assertEqual((on_call["thread_01"], on_call["thread_02"]),
                         ("'Add a clamp helper' in the project 'Voice sandbox', working.",
                          "'Add a clamp helper' in the project 'Docs site', finished. The person opened this call "
                          "from this thread."), "the call's own option is marked, by id, not by its shared title")
        answering = pick_request("Postgres.", threads, None, answering=True, call_thread="b")["questions"]["pick"]
        self.assertNotIn("opened this call", json.dumps(answering), "an answer names no thread, so none is marked")

    def test_an_answer_that_is_not_a_pick_picks_nothing(self):
        threads = listed("short")[:2]
        for answers in (None, {}, {"pick": {"choice": "thread_09", "probabilities": {"thread_09": 0.9}}},
                        {"pick": {"choice": "thread_01", "probabilities": {"thread_01": "0.9"}}},
                        {"pick": {"choice": "thread_01", "probabilities": {"thread_01": 0.9, "thread_02": 0.9}}}):
            with self.subTest(answers=answers):
                picked = judge_pick(answers, threads)
                self.assertEqual((picked.choice, picked.thread, picked.candidates, picked.sure), (None, None, (), False))

    def test_an_error_or_no_threads_never_picks_one(self):
        self.assertIsNone(Decider(Failing(TimeoutError("timed out"))).picks("the clamp one", listed("short")).thread)
        self.assertEqual(Decider(Recorded()).picks("the clamp one", ()).choice, NONE)
        self.assertEqual(len(LISTINGS["long"]), 40)


if __name__ == "__main__":
    unittest.main()


class CallPicks(unittest.TestCase):
    """On recorded d1.2 answers for a call opened on a thread (voice/t3/eval/record_call_picks.py): with the call's
    thread marked, words that name no other thread choose it, in most samples of each case, beside a near twin too;
    words naming another thread, the twin among them, choose that one either way."""

    FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "call_picks.json").read_text(encoding="utf-8"))

    def choices(self, row):
        from voice.t3.eval.record_call_picks import body, call_id, listed
        threads, answers = listed(row["listing"]), iter(row["samples"])

        class Replay:
            def call(self, route, sent, label=""):
                assert sent == body(row), "re-run record_call_picks"
                sample = next(answers)
                return {"status": 200, "response": {"answers": {"pick": {"choice": sample["choice"],
                                                                         "probabilities": sample["p"]}}}}

        titles = {t.thread_id: t.title for t in threads}
        call = call_id(row["listing"]) if row["shown"] else None
        return [titles.get(p.choice, p.choice) for p in
                (Decider(Replay()).picks(row["request"], threads, row["recent"], call_thread=call)
                 for _ in row["samples"])]

    def test_each_case_chooses_its_thread_in_most_samples_with_the_calls_thread_shown(self):
        for row in self.FIXTURE["rows"]:
            if not row["shown"]:
                continue
            with self.subTest(row["request"], listing=row["listing"], held=row["held"]):
                choices = self.choices(row)
                self.assertGreater(choices.count(row["want"]), len(choices) / 2, choices)

    def test_the_dry_runs_two_questions_did_not_choose_the_calls_thread_unmarked(self):
        for row in self.FIXTURE["rows"]:
            if row["shown"] or row["request"] not in ("What is this thread working on right now?",
                                                      "Is it waiting on anything from me?"):
                continue
            with self.subTest(row["request"], listing=row["listing"]):
                choices = self.choices(row)
                self.assertLessEqual(choices.count(row["want"]), len(choices) / 2, choices)
