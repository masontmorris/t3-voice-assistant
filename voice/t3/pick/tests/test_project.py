"""Tests for the Twin A deterministic project pick, on synthetic fixtures only."""

import unittest
from urllib.error import URLError

from voice.t3.pick.decide import decide_project
from voice.t3.pick.model import ModelFailed, pick_project_via_model
from voice.t3.pick.project import ProjectPick, pick_project, question_for_projects


PROJECTS = [
	{"projectId": "p-harbor-light", "name": "Harbor Light", "spokenNames": ["Harbor Light", "harbor-light"]},
	{"projectId": "p-harbor-engine", "name": "Harbor Engine", "spokenNames": ["Harbor Engine", "harbor-engine"]},
	{"projectId": "p-delta", "name": "Delta (fork)", "spokenNames": ["Delta (fork)", "delta-fork", "Delta"]},
	{"projectId": "p-quartz", "name": "Quartz", "spokenNames": ["Quartz", "quartz"]},
]


def ids():
	return [p["projectId"] for p in PROJECTS]


class ExactTest(unittest.TestCase):
	def test_a_full_project_name_is_the_pick(self):
		picked = pick_project("start a thread in harbor light that fixes the crash", PROJECTS)
		self.assertEqual(picked.kind, "one")
		self.assertEqual(picked.project_id, "p-harbor-light")

	def test_a_folder_style_spoken_name_is_the_pick(self):
		picked = pick_project("start a thread in harbor engine for the retry bug", PROJECTS)
		self.assertEqual(picked.kind, "one")
		self.assertEqual(picked.project_id, "p-harbor-engine")

	def test_a_short_spoken_name_is_the_pick(self):
		picked = pick_project("start a thread in delta to fix the sync", PROJECTS)
		self.assertEqual(picked.kind, "one")
		self.assertEqual(picked.project_id, "p-delta")

	def test_case_and_plural_do_not_break_an_exact_match(self):
		picked = pick_project("start a thread in the QUARTZ project", PROJECTS)
		self.assertEqual(picked.kind, "one")
		self.assertEqual(picked.project_id, "p-quartz")


class AskTest(unittest.TestCase):
	def test_a_word_two_projects_share_asks_with_both(self):
		picked = pick_project("start a thread in harbor to fix the crash", PROJECTS)
		self.assertEqual(picked.kind, "ask")
		self.assertEqual(set(picked.candidates), {"p-harbor-light", "p-harbor-engine"})

	def test_two_full_names_ask_with_both(self):
		picked = pick_project("start a thread in harbor light or harbor engine", PROJECTS)
		self.assertEqual(picked.kind, "ask")
		self.assertEqual(set(picked.candidates), {"p-harbor-light", "p-harbor-engine"})

	def test_a_unique_partial_match_still_asks(self):
		picked = pick_project("start a thread in the engine project", PROJECTS)
		self.assertEqual(picked.kind, "ask")
		self.assertEqual(list(picked.candidates), ["p-harbor-engine"])

	def test_no_project_named_asks_with_no_candidates(self):
		picked = pick_project("start a thread that fixes the login crash", PROJECTS)
		self.assertEqual(picked.kind, "ask")
		self.assertEqual(list(picked.candidates), [])

	def test_a_project_mentioned_only_inside_the_task_asks(self):
		picked = pick_project("start a thread in quartz that ports the harbor light importer", PROJECTS)
		self.assertEqual(picked.kind, "ask")
		self.assertEqual(set(picked.candidates), {"p-quartz", "p-harbor-light"})

	def test_the_question_names_the_candidates(self):
		picked = pick_project("start a thread in harbor to fix the crash", PROJECTS)
		self.assertIn("Harbor Light", picked.question)
		self.assertIn("Harbor Engine", picked.question)


class NoneTest(unittest.TestCase):
	def test_an_explicit_project_with_no_match_is_none(self):
		picked = pick_project("start a thread in project zephyria that fixes login", PROJECTS)
		self.assertEqual(picked.kind, "none")

	def test_container_words_alone_are_not_a_project(self):
		picked = pick_project("start a thread", PROJECTS)
		self.assertEqual(picked.kind, "ask")


class MembershipTest(unittest.TestCase):
	def test_an_unlisted_id_is_rejected(self):
		with self.assertRaises(ValueError):
			ProjectPick.one("p-made-up", ids())

	def test_a_listed_one_pick_stays_a_pick(self):
		self.assertEqual(ProjectPick.one("p-quartz", ids()).project_id, "p-quartz")

	def test_an_unlisted_coerced_id_becomes_a_question(self):
		picked = ProjectPick.coerce("p-made-up", ids(), "Which project?")
		self.assertEqual(picked.kind, "ask")

	def test_a_listed_coerced_id_stays_a_pick(self):
		picked = ProjectPick.coerce("p-quartz", ids(), "Which project?")
		self.assertEqual(picked.kind, "one")
		self.assertEqual(picked.project_id, "p-quartz")

	def test_illegal_states_are_rejected(self):
		with self.assertRaises(ValueError):
			ProjectPick(kind="one")
		with self.assertRaises(ValueError):
			ProjectPick(kind="ask", candidates=("p-quartz",))
		with self.assertRaises(ValueError):
			ProjectPick(kind="none", project_id="p-quartz")

	def test_no_real_project_names_leak_into_fixtures(self):
		blob = " ".join(p["name"] for p in PROJECTS)
		self.assertNotIn("t3", blob.lower())

	def test_question_helper_with_no_candidates(self):
		self.assertEqual(question_for_projects([]), "Which project do you mean?")


class FakeProjectClient:
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


class ModelProjectTest(unittest.TestCase):
	def test_a_listed_choice_is_a_pick(self):
		client = FakeProjectClient('{"choice": "p-quartz", "candidates": []}')
		picked, meta = pick_project_via_model("start a thread in quartz", PROJECTS, client)
		self.assertEqual(picked.kind, "one")
		self.assertEqual(picked.project_id, "p-quartz")
		self.assertEqual(meta["ms"], 12)

	def test_null_choice_with_candidates_is_a_question(self):
		client = FakeProjectClient('{"choice": null, "candidates": ["p-harbor-light"]}')
		picked, _ = pick_project_via_model("start a thread in harbor", PROJECTS, client)
		self.assertEqual(picked.kind, "ask")
		self.assertEqual(list(picked.candidates), ["p-harbor-light"])

	def test_nothing_matching_is_none(self):
		client = FakeProjectClient('{"choice": null, "candidates": []}')
		picked, _ = pick_project_via_model("start a thread in project zephyria", PROJECTS, client)
		self.assertEqual(picked.kind, "none")

	def test_an_unlisted_choice_becomes_a_question(self):
		client = FakeProjectClient('{"choice": "p-imaginary", "candidates": []}')
		picked, _ = pick_project_via_model("start a thread", PROJECTS, client)
		self.assertEqual(picked.kind, "ask")

	def test_an_unparseable_reply_is_reported_as_unparseable(self):
		client = FakeProjectClient("I think it is quartz")
		picked, meta = pick_project_via_model("start a thread", PROJECTS, client)
		self.assertEqual(picked.kind, "ask")
		self.assertEqual(meta["parse"], "unparseable")

	def test_the_prompt_lists_every_project_once(self):
		seen = {}

		class Recorder(FakeProjectClient):
			def complete(self, messages):
				seen["prompt"] = messages[-1]["content"]
				return super().complete(messages)

		client = Recorder('{"choice": null, "candidates": []}')
		pick_project_via_model("start a thread", PROJECTS, client)
		for p in PROJECTS:
			self.assertIn(p["projectId"], seen["prompt"])
			self.assertIn(p["name"], seen["prompt"])


class DecideProjectTest(unittest.TestCase):
	def test_a_deterministic_hit_never_calls_the_model(self):
		client = FakeProjectClient('{"choice": "p-quartz"}')
		picked, meta = decide_project("start a thread in harbor light that fixes x", PROJECTS, client)
		self.assertEqual(picked.kind, "one")
		self.assertEqual(picked.project_id, "p-harbor-light")
		self.assertEqual(client.calls, 0)
		self.assertFalse(meta["model"])

	def test_an_ask_reaches_the_model(self):
		client = FakeProjectClient('{"choice": "p-harbor-engine", "candidates": []}')
		picked, meta = decide_project("start a thread in harbor to fix x", PROJECTS, client)
		self.assertEqual(picked.kind, "one")
		self.assertEqual(picked.project_id, "p-harbor-engine")
		self.assertTrue(meta["model"])
		self.assertEqual(meta["shortlist"], len(PROJECTS))

	def test_a_model_failure_becomes_a_question(self):
		for error in (ModelFailed("gave up"), URLError("offline")):
			client = FakeProjectClient(error=error)
			picked, meta = decide_project("start a thread in harbor to fix x", PROJECTS, client)
			self.assertEqual(picked.kind, "ask")
			self.assertEqual(meta["parse"], "failed")


class ProjectHarnessTest(unittest.TestCase):
	def test_a_wrong_project_is_a_wrong_thread(self):
		from voice.t3.pick.harness import outcome
		from voice.t3.pick.project_harness import gold_for_thread
		case = {"gold": {"kind": "one", "project_id": "p-quartz"}}
		picked = ProjectPick.one("p-delta", ids(), "test").as_thread_pick(ids())
		self.assertEqual(outcome(picked, gold_for_thread(case), ids()), "wrong_thread")

	def test_a_direct_pick_scored_as_a_confirming_question(self):
		from voice.t3.pick.harness import confirm_outcome
		from voice.t3.pick.project_harness import gold_for_thread
		case = {"gold": {"kind": "one", "project_id": "p-quartz"}}
		right = ProjectPick.one("p-quartz", ids(), "model").as_thread_pick(ids())
		wrong = ProjectPick.one("p-delta", ids(), "model").as_thread_pick(ids())
		self.assertEqual(confirm_outcome(right, gold_for_thread(case), ids()), "confirmed")
		self.assertEqual(confirm_outcome(wrong, gold_for_thread(case), ids()), "confirm_wrong")

	def test_a_correct_pick_survives_the_round_trip(self):
		from voice.t3.pick.harness import outcome
		from voice.t3.pick.project_harness import gold_for_thread
		case = {"gold": {"kind": "ask", "project_ids": ["p-quartz"]}}
		picked = ProjectPick.ask(["p-quartz"], "Which project?", "test").as_thread_pick(ids())
		self.assertEqual(outcome(picked, gold_for_thread(case), ids()), "correct")


if __name__ == "__main__":
	unittest.main()
