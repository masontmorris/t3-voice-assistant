import unittest

from voice.t3.pick.harness import lenient, outcome, p95, pass_bar, summarise
from voice.t3.pick.pick import Pick

LISTED = ["a", "b", "c"]
GOLD_ONE = {"kind": "one", "thread_id": "a"}
GOLD_ASK = {"kind": "ask", "thread_ids": ["a", "b"]}
GOLD_NONE = {"kind": "none"}


class OutcomeTest(unittest.TestCase):
	def test_a_listed_pick_of_the_gold_thread_is_correct(self):
		self.assertEqual(outcome(Pick.one("a", LISTED), GOLD_ONE, LISTED), "correct")

	def test_another_listed_thread_is_a_wrong_thread(self):
		self.assertEqual(outcome(Pick.one("b", LISTED), GOLD_ONE, LISTED), "wrong_thread")

	def test_an_ask_is_correct_only_when_its_candidates_include_the_gold_threads(self):
		both = Pick.ask(["a", "b"], "Which?")
		self.assertEqual(outcome(both, GOLD_ASK, LISTED), "correct")
		one = Pick.ask(["a", "c"], "Which?")
		self.assertEqual(outcome(one, GOLD_ASK, LISTED), "question_missing_gold")
		empty = Pick.ask([], "Which?")
		self.assertEqual(outcome(empty, GOLD_ASK, LISTED), "question_missing_gold")

	def test_an_ask_with_no_gold_set_only_has_to_be_a_question(self):
		open_ask = {"kind": "ask", "thread_ids": []}
		self.assertEqual(outcome(Pick.ask([], "Which?"), open_ask, LISTED), "correct")
		self.assertEqual(outcome(Pick.one("a", LISTED), open_ask, LISTED), "picked_when_should_ask")

	def test_an_ask_for_a_one_gold_thread_is_its_own_outcome(self):
		self.assertEqual(outcome(Pick.ask(["a"], "Which?"), GOLD_ONE, LISTED), "asked_when_one")

	def test_an_unparseable_reply_is_not_a_question(self):
		verdict = outcome(Pick.ask(["a"], "Which?"), GOLD_ASK, LISTED, parse="unparseable")
		self.assertEqual(verdict, "unparseable")

	def test_a_model_that_could_not_be_reached_is_unavailable(self):
		self.assertEqual(outcome(Pick.ask([], "Which?"), GOLD_ONE, LISTED, parse="failed"), "unavailable")

	def test_lenient_counts_a_question_as_correct_when_the_gold_was_a_question(self):
		self.assertEqual(lenient("question_missing_gold", GOLD_ASK), "correct")
		self.assertEqual(lenient("unparseable", GOLD_ASK), "correct")
		self.assertEqual(lenient("unparseable", GOLD_ONE), "asked_when_one")
		self.assertEqual(lenient("wrong_thread", GOLD_ONE), "wrong_thread")


def run_row(slice_name, verdict, request_ms=0, wall_ms=1, parse="ok", length=1, tokens=None, gold=GOLD_ONE):
	row = {"arm": "model", "case": f"{slice_name}-{id(object())}", "slice": slice_name,
	       "outcome": verdict, "lenient": lenient(verdict, gold), "request_ms": request_ms,
	       "wall_ms": wall_ms, "model_call": bool(request_ms), "parse": parse,
	       "tokens": tokens}
	row["_repeat"] = length
	return row


class SummaryTest(unittest.TestCase):
	def test_a_case_passes_only_when_every_repeat_passes(self):
		rows = []
		for i in range(3):
			row = run_row("exact", "correct")
			row["case"] = "one-case"
			rows.append(row)
		rows[2]["outcome"] = "wrong_thread"
		summary = summarise(rows, ["model"])
		self.assertEqual(summary["model"]["exact"]["cases"], 1)
		self.assertEqual(summary["model"]["exact"]["pass_strict"], 0)

	def test_single_request_latency_is_reported_apart_from_wall_time(self):
		rows = [run_row("exact", "correct", request_ms=900, wall_ms=1400)]
		bucket = summarise(rows, ["model"])["model"]["exact"]
		self.assertEqual(bucket["p95_single_request_ms"], 900)
		self.assertEqual(bucket["p95_wall_ms"], 1400)

	def test_the_pass_bar_names_its_latency_metric(self):
		rows = [run_row("exact", "correct", request_ms=900, wall_ms=1400)]
		summary = summarise(rows, ["model"])
		bar = pass_bar(summary, "model")
		self.assertEqual(bar["latency_metric"], "single_request_p95_ms")
		self.assertEqual(bar["p95_single_request_ms"], 900)
		self.assertFalse(bar["p95_at_most_700ms"])

	def test_unparseable_replies_reach_the_strict_outcome_counts(self):
		rows = [run_row("ambiguous", "unparseable", parse="unparseable", gold=GOLD_ASK)]
		bucket = summarise(rows, ["model"])["model"]["ambiguous"]
		self.assertEqual(bucket["outcomes"], {"unparseable": 1})
		self.assertEqual(bucket["lenient_outcomes"], {"correct": 1})

	def test_tokens_are_summed_per_slice(self):
		rows = [run_row("exact", "correct", request_ms=10, tokens={"reasoning_tokens": 4, "content_tokens": 6})]
		bucket = summarise(rows, ["model"])["model"]["exact"]
		self.assertEqual((bucket["reasoning_tokens"], bucket["content_tokens"]), (4, 6))

	def test_p95_takes_the_nearest_rank(self):
		self.assertEqual(p95([1, 2, 3, 4, 5]), 5)
		self.assertEqual(p95([]), 0)


if __name__ == "__main__":
	unittest.main()
