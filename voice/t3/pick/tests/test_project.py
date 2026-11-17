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


COMMON_PROJECTS = PROJECTS + [
	{"projectId": "p-no-project", "name": "No project", "spokenNames": ["No project", "scratch"]},
]

LAB_PROJECTS = [
	{"projectId": "p-decision-lab", "name": "Decision Lab", "spokenNames": ["Decision Lab", "decision-lab"]},
	{"projectId": "p-s1-loop", "name": "S1 Loop", "spokenNames": ["S1 Loop", "s1-loop"]},
	{"projectId": "p-web-audio", "name": "Web Audio API", "spokenNames": ["Web Audio API", "web-audio-api"]},
]

PREFIX_PROJECTS = [
	{"projectId": "p-harbor-voice", "name": "Harbor Voice", "spokenNames": ["Harbor Voice", "harbor-voice"]},
	{"projectId": "p-harbor-voice-assistant", "name": "Harbor Voice Assistant",
	 "spokenNames": ["Harbor Voice Assistant"]},
]

AGENT_PROJECTS = [
	{"projectId": "p-agent", "name": "Agent Runner", "spokenNames": ["Agent Runner"]},
]


class ExplicitUnlistedTest(unittest.TestCase):
	def test_an_unlisted_project_with_a_listed_name_in_the_task_is_none(self):
		picked = pick_project("start a thread in project zephyria that ports the harbor light importer",
		                      PROJECTS)
		self.assertEqual(picked.kind, "none")

	def test_a_listed_target_still_picks_despite_an_unlisted_project_word(self):
		picked = pick_project("start a thread in the scratch project that cleans up exports",
		                      COMMON_PROJECTS)
		self.assertEqual(picked.kind, "one")
		self.assertEqual(picked.project_id, "p-no-project")


class CommonWordTest(unittest.TestCase):
	def test_a_common_word_inside_the_task_does_not_count(self):
		picked = pick_project("rebuilds the parser from scratch", COMMON_PROJECTS)
		self.assertEqual(picked.kind, "ask")
		self.assertEqual(list(picked.candidates), [])

	def test_a_common_phrase_inside_the_task_does_not_count(self):
		picked = pick_project("there is no project for it yet", COMMON_PROJECTS)
		self.assertEqual(picked.kind, "ask")

	def test_a_common_word_as_the_target_picks(self):
		picked = pick_project("open a thread in scratch to investigate the log", COMMON_PROJECTS)
		self.assertEqual(picked.kind, "one")
		self.assertEqual(picked.project_id, "p-no-project")

	def test_a_common_word_before_project_picks(self):
		picked = pick_project("start a thread in the scratch project that cleans up exports",
		                      COMMON_PROJECTS)
		self.assertEqual(picked.kind, "one")
		self.assertEqual(picked.project_id, "p-no-project")


class StrictPrefixTest(unittest.TestCase):
	def test_a_strict_prefix_of_a_longer_listed_name_asks(self):
		picked = pick_project("start a thread in harbor voice that fixes the crash", PREFIX_PROJECTS)
		self.assertEqual(picked.kind, "ask")
		self.assertEqual(set(picked.candidates), {"p-harbor-voice", "p-harbor-voice-assistant"})

	def test_the_short_form_alone_still_picks(self):
		picked = pick_project("start a thread in harbor voice that fixes the crash",
		                      PREFIX_PROJECTS[:1])
		self.assertEqual(picked.kind, "one")
		self.assertEqual(picked.project_id, "p-harbor-voice")


class TaskSideExactTest(unittest.TestCase):
	def test_a_task_side_exact_match_does_not_pick(self):
		picked = pick_project("start a thread in the lab that ports the s1 loop importer", LAB_PROJECTS)
		self.assertEqual(picked.kind, "ask")
		self.assertIn("p-decision-lab", picked.candidates)

	def test_a_stated_target_beside_a_task_side_exact_still_asks(self):
		picked = pick_project("start a thread in decision lab that ports the s1 loop importer",
		                      LAB_PROJECTS)
		self.assertEqual(picked.kind, "ask")

	def test_an_exact_target_with_only_a_task_fragment_picks_the_target(self):
		picked = pick_project("start a thread in decision lab that cleans up the web audio exports",
		                      LAB_PROJECTS)
		self.assertEqual(picked.kind, "one")
		self.assertEqual(picked.project_id, "p-decision-lab")


class SingularPartialTest(unittest.TestCase):
	def test_a_plural_task_word_matches_the_singular_project(self):
		picked = pick_project("start a thread in the agents project to check the queue", AGENT_PROJECTS)
		self.assertEqual(picked.kind, "ask")
		self.assertEqual(list(picked.candidates), ["p-agent"])


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

	def test_a_choice_with_two_candidates_becomes_a_question(self):
		client = FakeProjectClient('{"choice": "p-quartz", "candidates": ["p-harbor-light", "p-harbor-engine"]}')
		picked, _ = pick_project_via_model("start a thread in harbor", PROJECTS, client)
		self.assertEqual(picked.kind, "ask")
		self.assertEqual(set(picked.candidates), {"p-harbor-light", "p-harbor-engine"})

	def test_an_unparseable_reply_falls_back_to_the_deterministic_candidates(self):
		client = FakeProjectClient("I think it is quartz")
		picked, meta = pick_project_via_model("start a thread in harbor", PROJECTS, client,
		                                      ["p-harbor-light"])
		self.assertEqual(picked.kind, "ask")
		self.assertEqual(meta["parse"], "unparseable")
		self.assertEqual(list(picked.candidates), ["p-harbor-light"])

	def test_an_unlisted_choice_falls_back_to_the_deterministic_candidates(self):
		client = FakeProjectClient('{"choice": "p-imaginary", "candidates": []}')
		picked, _ = pick_project_via_model("start a thread", PROJECTS, client, ["p-quartz"])
		self.assertEqual(picked.kind, "ask")
		self.assertEqual(list(picked.candidates), ["p-quartz"])
		self.assertIn("Quartz", picked.question)

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

	def test_a_model_failure_uses_the_deterministic_candidates(self):
		client = FakeProjectClient(error=ModelFailed("gave up"))
		picked, _ = decide_project("start a thread in harbor to fix x", PROJECTS, client)
		self.assertEqual(picked.kind, "ask")
		self.assertEqual(set(picked.candidates), {"p-harbor-light", "p-harbor-engine"})


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
