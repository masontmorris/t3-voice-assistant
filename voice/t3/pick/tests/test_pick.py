import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError, URLError

from voice.t3.pick import model
from voice.t3.pick.decide import pick, prefilter_pick
from voice.t3.pick.model import LiveClient, ModelClient, ModelFailed, error_is_billing, pick_thread, tokens
from voice.t3.pick.pick import Pick
from voice.t3.pick.prefilter import content_words, prefilter

THREADS = [
	{"threadId": "t-flaky", "projectId": "p1", "title": "Fix the flaky ASR test", "activity": "working", "lastActivityAt": "2026-11-16T10:00:00Z"},
	{"threadId": "t-pr-a", "projectId": "p1", "title": "Verify PR 12 at aaa", "activity": "finished", "lastActivityAt": "2026-11-16T09:00:00Z"},
	{"threadId": "t-pr-b", "projectId": "p1", "title": "Verify PR 12 at bbb", "activity": "finished", "lastActivityAt": "2026-11-16T08:00:00Z"},
	{"threadId": "t-merge", "projectId": "p2", "title": "Merge upstream main", "activity": "finished", "lastActivityAt": "2026-11-16T07:00:00Z"},
	{"threadId": "t-upload", "projectId": "p2", "title": "Failed upload handling", "activity": "finished", "lastActivityAt": "2026-11-16T06:00:00Z"},
	{"threadId": "t-broke", "projectId": "p2", "title": "Caching layer rewrite", "activity": "failed", "lastActivityAt": "2026-11-16T05:00:00Z"},
]
PROJECTS = {"p1": "alpha", "p2": "beta"}
NOW = "2026-11-16T12:00:00.000Z"


class StateWordTest(unittest.TestCase):
	def test_a_state_word_does_not_name_the_title_that_contains_it(self):
		# The old rule matched "failed" inside a title and spoke it for a question about state.
		picked = prefilter_pick("how's the one that failed", THREADS)
		self.assertNotEqual(picked.thread_id, "t-upload")
		self.assertEqual(picked.thread_id, "t-broke")
	def test_a_state_word_matches_activity(self):
		picked = prefilter_pick("how's the one that failed", [t for t in THREADS if t["threadId"] != "t-broke"])
		self.assertNotEqual(picked.kind, "one", "nothing failed, so it asks instead of naming the title")

	def test_a_state_word_with_one_matching_activity_is_the_pick(self):
		picked = prefilter_pick("what failed", THREADS)
		self.assertEqual(picked.kind, "one")
		self.assertEqual(picked.thread_id, "t-broke")

	def test_state_words_alone_ask_when_several_activities_match(self):
		picked = prefilter_pick("what's finished", THREADS)
		self.assertEqual(picked.kind, "ask")
		self.assertEqual(set(picked.candidates), {"t-pr-a", "t-pr-b", "t-merge", "t-upload"})

	def test_a_state_word_narrows_matching_titles(self):
		twin = {"threadId": "t-flaky-old", "projectId": "p1", "title": "Fix the flaky ASR test",
		        "activity": "finished", "lastActivityAt": "2026-11-15T10:00:00Z"}
		picked = prefilter_pick("the flaky asr test that is working", THREADS + [twin])
		self.assertEqual(picked.kind, "one")
		self.assertEqual(picked.thread_id, "t-flaky")

	def test_a_state_word_in_a_title_keeps_that_title_among_the_candidates(self):
		threads = [
			{"threadId": "t-fix", "projectId": "p1", "title": "Fix broken login", "activity": "idle", "lastActivityAt": "2026-11-16T10:00:00Z"},
			{"threadId": "t-page", "projectId": "p1", "title": "Login page redesign", "activity": "failed", "lastActivityAt": "2026-11-16T09:00:00Z"},
		]
		picked = prefilter_pick("how's the broken login thread", threads)
		self.assertEqual(picked.kind, "ask")
		self.assertEqual(set(picked.candidates), {"t-fix", "t-page"})


class RecallTest(unittest.TestCase):
	def test_contractions_do_not_add_content_words(self):
		self.assertEqual(content_words("let's check the flaky asr test"), ["flaky", "asr", "test"])
		self.assertEqual(content_words("i'm wondering about the flaky asr test"), ["flaky", "asr", "test"])

	def test_filler_does_not_break_a_match(self):
		picked = prefilter_pick("hey, i'm just wondering how the flaky asr test is going", THREADS)
		self.assertEqual(picked.thread_id, "t-flaky")

	def test_a_possessive_matches_its_title(self):
		threads = [{"threadId": "t-mason", "projectId": "p1", "title": "Mason audio bridge",
		            "activity": "working", "lastActivityAt": "2026-11-16T10:00:00Z"}]
		picked = prefilter_pick("how's mason's audio bridge going", threads)
		self.assertEqual(picked.kind, "one")
		self.assertEqual(picked.thread_id, "t-mason")


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

	def test_a_single_content_word_that_one_title_carries_is_the_pick(self):
		match = prefilter("the upstream one", THREADS)
		self.assertEqual(match.candidates, ["t-merge"])
		self.assertEqual(match.words, ["upstream"])

	def test_a_single_content_word_shared_by_two_titles_ties(self):
		threads = [
			{"threadId": "a", "projectId": "p1", "title": "Upstream sync", "activity": "finished", "lastActivityAt": "2026-11-16T10:00:00Z"},
			{"threadId": "b", "projectId": "p1", "title": "Upstream notes", "activity": "finished", "lastActivityAt": "2026-11-16T09:00:00Z"},
		]
		picked = prefilter_pick("the upstream one", threads)
		self.assertEqual(picked.kind, "ask")
		self.assertEqual(set(picked.candidates), {"a", "b"})


PROJECT_THREADS = [
	{"threadId": "g-loader", "projectId": "p3", "title": "Gamma loader cleanup", "activity": "finished", "lastActivityAt": "2026-11-16T10:00:00Z"},
	{"threadId": "g-save", "projectId": "p3", "title": "Fix the save bug", "activity": "finished", "lastActivityAt": "2026-11-16T09:00:00Z"},
	{"threadId": "s-solo", "projectId": "p4", "title": "Delta notes", "activity": "finished", "lastActivityAt": "2026-11-16T08:00:00Z"},
	{"threadId": "a-other", "projectId": "p1", "title": "Merge upstream main", "activity": "failed", "lastActivityAt": "2026-11-16T07:00:00Z"},
]
PROJECT_NAMES = {"p1": "alpha", "p3": "gamma-engine", "p4": "delta"}


class RoundThreeRuleTest(unittest.TestCase):
	def test_asking_what_is_happening_adds_no_content_word(self):
		self.assertEqual(content_words("what's happening with the flaky asr test"), ["flaky", "asr", "test"])
		self.assertEqual(content_words("any news on what happened to the merge"), ["merge"])
		picked = prefilter_pick("what's happening with the verify pr 12 thread", THREADS)
		self.assertEqual(picked.kind, "ask")
		self.assertEqual(set(picked.candidates), {"t-pr-a", "t-pr-b"})

	def test_a_project_name_alone_asks_even_when_one_title_carries_it(self):
		for utterance in ("how's the gamma thread going", "what about gamma engine", "how's the gamma engine project"):
			picked = prefilter_pick(utterance, PROJECT_THREADS, PROJECT_NAMES)
			self.assertEqual(picked.kind, "ask", utterance)
			self.assertEqual(set(picked.candidates), {"g-loader", "g-save"}, utterance)

	def test_a_project_name_with_one_listed_thread_still_matches_titles(self):
		picked = prefilter_pick("the delta one", PROJECT_THREADS, PROJECT_NAMES)
		self.assertEqual(picked.kind, "one")
		self.assertEqual(picked.thread_id, "s-solo")

	def test_a_project_name_with_a_state_word_keeps_the_title_match(self):
		threads = PROJECT_THREADS + [
			{"threadId": "g-run", "projectId": "p3", "title": "Gamma perf pass", "activity": "failed", "lastActivityAt": "2026-11-16T06:00:00Z"},
		]
		picked = prefilter_pick("the gamma one that failed", threads, PROJECT_NAMES)
		self.assertEqual(picked.kind, "one")
		self.assertEqual(picked.thread_id, "g-run")

	def test_a_one_word_spelling_matches_a_title_that_splits_it(self):
		threads = [
			{"threadId": "p-run", "projectId": "p1", "title": "Play test runner review", "activity": "finished", "lastActivityAt": "2026-11-16T10:00:00Z"},
			{"threadId": "p-cache", "projectId": "p1", "title": "Runner cache", "activity": "finished", "lastActivityAt": "2026-11-16T09:00:00Z"},
		]
		picked = prefilter_pick("how's the playtest runner one", threads)
		self.assertEqual(picked.kind, "one")
		self.assertEqual(picked.thread_id, "p-run")


class PickMembershipTest(unittest.TestCase):
	def test_an_unlisted_id_becomes_a_question_with_the_listing(self):
		picked = Pick.coerce("t-made-up", [t["threadId"] for t in THREADS], "Which thread?")
		self.assertEqual(picked.kind, "ask")
		self.assertEqual(set(picked.candidates), {t["threadId"] for t in THREADS})
		self.assertIn("unlisted id", picked.reason)

	def test_an_unlisted_one_pick_is_rejected(self):
		with self.assertRaises(ValueError):
			Pick.one("t-made-up", ["t-merge"])

	def test_a_listed_one_pick_stays_a_pick(self):
		self.assertEqual(Pick.one("t-merge", ["t-merge"]).kind, "one")

	def test_illegal_states_are_rejected(self):
		with self.assertRaises(ValueError):
			Pick(kind="one")
		with self.assertRaises(ValueError):
			Pick(kind="ask", candidates=("t-merge",))
		with self.assertRaises(ValueError):
			Pick(kind="none", thread_id="t-merge")


class FakeClient:
	def __init__(self, reply=None, error=None):
		self.reply = reply
		self.error = error
		self.calls = 0
		self.paused = None
		self.model = "fake:free"

	def complete(self, messages):
		self.calls += 1
		if self.error:
			raise self.error
		return {"text": self.reply, "ms": 12, "model": "fake:free", "usage": {},
		        "tokens": {"reasoning_tokens": 0, "content_tokens": 7}}


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
		self.assertEqual(meta["parse"], "ok")

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

	def test_a_quoted_null_choice_is_read_as_no_choice(self):
		picked, _, _ = self.run_model('{"choice": "null", "candidates": ["t-flaky"]}')
		self.assertEqual(picked.kind, "ask")
		self.assertEqual(set(picked.candidates), {"t-flaky"})

	def test_an_unparseable_reply_is_reported_as_unparseable(self):
		picked, meta, _ = self.run_model("I think it is the flaky one")
		self.assertEqual(picked.kind, "ask")
		self.assertEqual(meta["parse"], "unparseable")


class ChainTest(unittest.TestCase):
	def test_a_prefilter_hit_never_calls_the_model(self):
		client = FakeClient('{"choice": "t-merge"}')
		picked, meta = pick("how's the flaky asr test going?", THREADS, PROJECTS, NOW, client)
		self.assertEqual(picked.kind, "one")
		self.assertEqual(client.calls, 0)
		self.assertFalse(meta["model"])

	def test_a_tie_asks_without_the_model_by_default(self):
		client = FakeClient('{"choice": "t-pr-b"}')
		picked, meta = pick("how's the verify pr 12 thread?", THREADS, PROJECTS, NOW, client)
		self.assertEqual(picked.kind, "ask")
		self.assertEqual(client.calls, 0)
		self.assertFalse(meta["model"])

	def test_the_tie_arm_hands_the_model_only_the_tied_threads(self):
		seen = {}

		class Recorder(FakeClient):
			def complete(self, messages):
				seen["prompt"] = messages[-1]["content"]
				return super().complete(messages)

		client = Recorder('{"choice": "t-pr-b", "candidates": ["t-pr-a", "t-pr-b"]}')
		picked, meta = pick("how's the verify pr 12 thread?", THREADS, PROJECTS, NOW, client, break_tie=True)
		self.assertEqual(picked.thread_id, "t-pr-b")
		self.assertTrue(meta["model"])
		self.assertIn("t-pr-a", seen["prompt"])
		self.assertIn("t-pr-b", seen["prompt"])
		self.assertNotIn("t-merge", seen["prompt"])

	def test_a_miss_lets_the_model_see_the_whole_listing(self):
		client = FakeClient('{"choice": "t-merge", "candidates": []}')
		picked, meta = pick("how's the deploy thread going", THREADS, PROJECTS, NOW, client)
		self.assertEqual(picked.kind, "one")
		self.assertEqual(meta["shortlist"], len(THREADS))

	def test_a_model_failure_becomes_a_question(self):
		for error in (ModelFailed("gave up"), URLError("offline")):
			client = FakeClient(error=error)
			picked, meta = pick("how's the deploy thread going", THREADS, PROJECTS, NOW, client)
			self.assertEqual(picked.kind, "ask")
			self.assertEqual(meta["parse"], "failed")


class RefusalTest(unittest.TestCase):
	def test_a_non_free_model_id_is_refused_before_any_call(self):
		with self.assertRaises(ValueError):
			ModelClient(model="some-vendor/some-model")

	def test_billing_words_in_a_model_reply_are_not_a_billing_stop(self):
		self.assertFalse(error_is_billing(200, '{"choice": "t-flaky", "candidates": []}'))
		self.assertFalse(error_is_billing(200, "your credit balance and billing page"))

	def test_a_402_or_a_billing_error_payload_is_a_stop(self):
		self.assertTrue(error_is_billing(402, "{}"))
		self.assertTrue(error_is_billing(429, '{"error": {"message": "Insufficient credits", "code": 402}}'))

	def test_an_unrelated_error_payload_is_not_a_stop(self):
		self.assertFalse(error_is_billing(500, '{"error": {"message": "upstream timeout"}}'))
		self.assertFalse(error_is_billing(500, "not json"))

	def test_reasoning_and_content_tokens_are_split(self):
		got = tokens({"prompt_tokens": 90, "completion_tokens": 30,
		              "completion_tokens_details": {"reasoning_tokens": 22}})
		self.assertEqual(got, {"reasoning_tokens": 22, "content_tokens": 8,
		                       "prompt_tokens": 90, "completion_tokens": 30})



class LiveClientTest(unittest.TestCase):
	"""The arm inside a voice turn makes one short attempt and never waits out a rate limit."""

	def client(self, error):
		tmp = tempfile.TemporaryDirectory()
		self.addCleanup(tmp.cleanup)
		patches = (mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "test"}),
		           mock.patch.object(model, "_post", side_effect=error), mock.patch.object(model, "MIN_GAP_S", 0.0))
		post = [p.start() for p in patches][1]
		for p in patches:
			self.addCleanup(p.stop)
		return LiveClient(cache_dir=Path(tmp.name)), post

	def test_a_failure_or_a_429_fails_the_pick_once_and_cools_down(self):
		for error in (TimeoutError("slow"), HTTPError(model.ENDPOINT, 429, "rate", {}, io.BytesIO(b"{}"))):
			with self.subTest(error=type(error).__name__):
				client, post = self.client(error)
				with self.assertRaises(ModelFailed):
					client.complete([{"role": "user", "content": "a"}])
				with self.assertRaises(ModelFailed):
					client.complete([{"role": "user", "content": "b"}])
				self.assertEqual(post.call_count, 1)
				self.assertEqual(post.call_args.args[2], 5.0)
				self.assertIsNone(client.paused)
				client.quiet_until = 0.0
				with self.assertRaises(ModelFailed):
					client.complete([{"role": "user", "content": "c"}])
				self.assertEqual((post.call_count, client.failures), (2, 2))

	def test_a_billing_stop_fails_every_later_pick_without_a_call(self):
		client, post = self.client(HTTPError(model.ENDPOINT, 402, "pay", {}, io.BytesIO(b"{}")))
		with self.assertRaises(ModelFailed):
			client.complete([{"role": "user", "content": "a"}])
		client.quiet_until = 0.0
		with self.assertRaises(ModelFailed):
			client.complete([{"role": "user", "content": "b"}])
		self.assertEqual(post.call_count, 1)
		self.assertTrue(client.billing)

	def test_the_live_arm_is_a_free_model(self):
		self.assertTrue(LiveClient.model.endswith(":free"))
		with self.assertRaises(ValueError):
			LiveClient(model="some-vendor/some-model")

	def test_a_paid_model_runs_only_when_named_in_the_opt_in(self):
		from voice.t3.pick import model as m
		with mock.patch.object(m, "PAID_MODEL", "some-vendor/some-model"):
			self.assertTrue(m.allowed_model("some-vendor/some-model"))
			self.assertFalse(m.allowed_model("some-vendor/other-model"))
			with self.assertRaises(ValueError):
				LiveClient(model="some-vendor/other-model")
		self.assertFalse(m.allowed_model("some-vendor/some-model"))


if __name__ == "__main__":
	unittest.main()
