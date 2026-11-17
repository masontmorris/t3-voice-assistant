import unittest

from voice.t3.pick.decide import pick, prefilter_pick
from voice.t3.pick.model import pick_thread
from voice.t3.pick.pick import Pick
from voice.t3.pick.prefilter import content_words, prefilter

THREADS = [
	{"threadId": "t-flaky", "projectId": "p1", "title": "Fix the flaky ASR test", "activity": "working", "lastActivityAt": "2026-11-16T10:00:00Z"},
	{"threadId": "t-pr-a", "projectId": "p1", "title": "Verify PR 12 at aaa", "activity": "finished", "lastActivityAt": "2026-11-16T09:00:00Z"},
	{"threadId": "t-pr-b", "projectId": "p1", "title": "Verify PR 12 at bbb", "activity": "finished", "lastActivityAt": "2026-11-16T08:00:00Z"},
	{"threadId": "t-merge", "projectId": "p2", "title": "Merge upstream main", "activity": "finished", "lastActivityAt": "2026-11-16T07:00:00Z"},
]
PROJECTS = {"p1": "alpha", "p2": "beta"}
NOW = "2026-11-16T12:00:00.000Z"


class PrefilterTest(unittest.TestCase):
	def test_one_title_containing_every_content_word_is_the_pick(self):
		picked = prefilter_pick("how's the flaky asr test going?", THREADS)
		self.assertEqual(picked.kind, "one")
		self.assertEqual(picked.thread_id, "t-flaky")

	def test_a_shared_prefix_ties_instead_of_picking_the_newest(self):
		picked = prefilter_pick("how's the verify pr 12 thread?", THREADS)
		self.assertEqual(picked.kind, "ask")
		self.assertEqual(set(picked.candidates), {"t-pr-a", "t-pr-b"})

	def test_a_word_no_title_carries_finds_nothing(self):
		picked = prefilter_pick("how's the deploy thread going?", THREADS)
		self.assertEqual(picked.kind, "none")

	def test_container_words_alone_name_no_subject(self):
		self.assertEqual(content_words("how's the thread going"), [])
		self.assertEqual(prefilter_pick("how's it going", THREADS).kind, "none")

	def test_plurals_and_case_still_match(self):
		picked = prefilter_pick("THE FLAKY ASR TESTS", THREADS)
		self.assertEqual(picked.kind, "one")
		self.assertEqual(picked.thread_id, "t-flaky")

	def test_a_single_content_word_shared_by_two_titles_ties(self):
		hits, words = prefilter("the upstream one", THREADS)
		self.assertEqual(hits, ["t-merge"])
		self.assertEqual(words, ["upstream"])


class PickCoercionTest(unittest.TestCase):
	def test_an_unlisted_id_becomes_a_question_with_the_listing(self):
		picked = Pick.coerce("t-made-up", [t["threadId"] for t in THREADS], "Which thread?")
		self.assertEqual(picked.kind, "ask")
		self.assertEqual(set(picked.candidates), {"t-flaky", "t-pr-a", "t-pr-b", "t-merge"})
		self.assertIn("unlisted id", picked.reason)

	def test_a_listed_id_stays_a_pick(self):
		picked = Pick.coerce("t-merge", ["t-merge"], "Which thread?")
		self.assertEqual(picked.kind, "one")

	def test_illegal_states_are_rejected(self):
		with self.assertRaises(ValueError):
			Pick(kind="one")
		with self.assertRaises(ValueError):
			Pick(kind="ask", candidates=("t-merge",))
		with self.assertRaises(ValueError):
			Pick(kind="none", thread_id="t-merge")


class FakeClient:
	def __init__(self, reply):
		self.reply = reply
		self.calls = 0
		self.paused = None
		self.model = "fake:free"

	def complete(self, messages):
		self.calls += 1
		return {"text": self.reply, "ms": 12, "model": "fake:free", "usage": {}}


class ModelPickTest(unittest.TestCase):
	def run_model(self, reply, threads=THREADS):
		client = FakeClient(reply)
		picked, meta = pick_thread("how's the flaky one", threads, PROJECTS, NOW, client)
		return picked, meta, client

	def test_a_listed_choice_is_a_pick(self):
		picked, meta, _ = self.run_model('{"choice": "t-flaky", "candidates": ["t-flaky"]}')
		self.assertEqual(picked.kind, "one")
		self.assertEqual(picked.thread_id, "t-flaky")
		self.assertEqual(meta["ms"], 12)

	def test_an_unlisted_choice_becomes_a_question(self):
		picked, _, _ = self.run_model('```json\n{"choice": "t-imaginary", "candidates": []}\n```')
		self.assertEqual(picked.kind, "ask")
		self.assertIn("t-flaky", picked.candidates)

	def test_null_choice_with_candidates_is_a_question(self):
		picked, _, _ = self.run_model('{"choice": null, "candidates": ["t-pr-a", "t-pr-b"]}')
		self.assertEqual(picked.kind, "ask")
		self.assertEqual(set(picked.candidates), {"t-pr-a", "t-pr-b"})

	def test_nothing_matching_is_none(self):
		picked, _, _ = self.run_model('{"choice": null, "candidates": []}')
		self.assertEqual(picked.kind, "none")

	def test_an_unparseable_reply_asks_instead_of_guessing(self):
		picked, _, _ = self.run_model("I think it is the flaky one")
		self.assertEqual(picked.kind, "ask")


class ChainTest(unittest.TestCase):
	def test_a_prefilter_hit_never_calls_the_model(self):
		client = FakeClient('{"choice": "t-merge"}')
		picked, meta = pick("how's the flaky asr test going?", THREADS, PROJECTS, NOW, client)
		self.assertEqual(picked.kind, "one")
		self.assertEqual(client.calls, 0)
		self.assertFalse(meta["model"])

	def test_a_tie_hands_the_model_only_the_tied_threads(self):
		seen = {}

		class Recorder(FakeClient):
			def complete(self, messages):
				seen["prompt"] = messages[-1]["content"]
				return super().complete(messages)

		client = Recorder('{"choice": "t-pr-b", "candidates": ["t-pr-a", "t-pr-b"]}')
		picked, meta = pick("how's the verify pr 12 thread?", THREADS, PROJECTS, NOW, client)
		self.assertEqual(picked.thread_id, "t-pr-b")
		self.assertTrue(meta["model"])
		self.assertIn("t-pr-a", seen["prompt"])
		self.assertIn("t-pr-b", seen["prompt"])
		self.assertNotIn("t-merge", seen["prompt"])

	def test_no_prefilter_hit_lets_the_model_see_the_whole_listing(self):
		client = FakeClient('{"choice": "t-merge", "candidates": []}')
		picked, meta = pick("how's the deploy thread going", THREADS, PROJECTS, NOW, client)
		self.assertEqual(picked.kind, "one")
		self.assertEqual(meta["shortlist"], len(THREADS))


if __name__ == "__main__":
	unittest.main()
