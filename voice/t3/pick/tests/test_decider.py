import unittest

from voice.t3.pick.decider import Candidate, choice_request, interpret, shortlist
from voice.t3.pick.decider_eval import thread_evidence, project_evidence
from voice.t3.pick.harness import confirm_outcome, outcome
from voice.t3.pick.model import render, render_projects


class DeciderTest(unittest.TestCase):
	def test_top_eight_rank_by_overlap_and_fill_zero_overlap_in_listing_order(self):
		items = [Candidate(f"local-{i}", f"Garden {i}") for i in range(9)]
		items.append(Candidate("local-target", "Copper Lantern", "Weather Station"))
		result = shortlist("What about the copper weather?", items)
		self.assertEqual(result, [items[-1], *items[:7]])
		self.assertEqual(shortlist("unmatched", items), items[:8])

	def test_thread_choice_reuses_the_particle_request_and_keeps_task_record_context(self):
		threads = [
			{"threadId": "th-new", "title": "Copper Lantern", "projectId": "p",
			 "activity": "working", "lastActivityAt": "2026-10-01T12:01:00Z"},
			{"threadId": "th-old", "title": "Copper Lantern", "projectId": "p",
			 "activity": "failed", "lastActivityAt": "2026-10-01T11:58:00Z"},
		]
		projects = {"p": "Weather Station"}
		utterance = "Go back to the one I just started."
		now = "2026-10-01T12:02:00Z"
		items = [Candidate(t["threadId"], t["title"], projects[t["projectId"]]) for t in reversed(threads)]
		evidence = thread_evidence(utterance, threads, projects, now, {item.id for item in items})
		body = choice_request("thread", evidence, items, "th-new")
		self.assertEqual(body["state"], {
			"request": render(utterance, threads, projects, now)[-1]["content"],
			"task_record_thread_id": "th-new",
		})
		self.assertEqual(list(body["questions"]), ["pick"])
		question = body["questions"]["pick"]
		self.assertEqual(question["type"], "choice")
		self.assertEqual(list(question["criteria"]), ["item_01", "item_02", "none", "ask"])
		self.assertIn("th-new", question["criteria"]["item_02"])
		narrow = thread_evidence(utterance, threads, projects, now, {"th-old"})
		self.assertNotIn("th-new", narrow)
		self.assertIn("failed", narrow)
		self.assertIn(utterance, narrow)

	def test_project_choice_reuses_the_particle_request_including_spoken_names(self):
		projects = [{"projectId": "p", "name": "Weather Station", "spokenNames": ["Weather Station", "Climate Lab"]}]
		utterance = "Open the Climate Lab."
		evidence = project_evidence(utterance, projects, {"p"})
		body = choice_request("project", evidence, [Candidate("p", "Weather Station")])
		self.assertEqual(body["state"], {"request": render_projects(utterance, projects)[-1]["content"]})

	def test_choice_is_local_and_scores_in_both_views(self):
		items = [Candidate("local-1", "Copper"), Candidate("local-2", "Lantern")]
		picked, parse = interpret({"type": "choice", "choice": "item_02", "confidence": 0.20}, items, "thread")
		gold = {"kind": "one", "thread_id": "local-2"}
		listed = [item.id for item in items]
		self.assertEqual(picked.thread_id, "local-2")
		self.assertEqual(outcome(picked, gold, listed, parse), "correct")
		self.assertEqual(confirm_outcome(picked, gold, listed, parse), "confirmed")

	def test_clarification_names_and_scores_the_offered_options(self):
		items = [Candidate("local-1", "Copper")]
		for choice, confidence, kind, parse in (
			("item_01", 0.19, "ask", "ok"),
			("ask", 0.90, "ask", "ok"),
			("none", 0.90, "none", "ok"),
			("item_02", 0.90, "ask", "unparseable"),
			("item_01", float("nan"), "ask", "unparseable"),
		):
			with self.subTest(choice=choice, confidence=confidence):
				picked, actual = interpret({"type": "choice", "choice": choice, "confidence": confidence},
				                           items, "project")
				self.assertEqual((picked.kind, actual), (kind, parse))
				if kind == "ask":
					self.assertEqual(picked.candidates, ("local-1",))
					self.assertEqual(picked.question, "Do you mean Copper?")
					gold = {"kind": "ask", "thread_ids": ["local-1"]}
					if parse == "ok":
						self.assertEqual(outcome(picked, gold, ["local-1"], parse), "correct")
