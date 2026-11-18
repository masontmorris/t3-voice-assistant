import json
import unittest

from voice.t3.pick.decider import Candidate, choice_request, interpret, shortlist
from voice.t3.pick.harness import confirm_outcome, outcome


class DeciderTest(unittest.TestCase):
	def test_top_eight_rank_by_overlap_and_fill_zero_overlap_in_listing_order(self):
		items = [Candidate(f"local-{i}", f"Garden {i}") for i in range(9)]
		items.append(Candidate("local-target", "Copper Lantern", "Weather Station"))
		result = shortlist("What about the copper weather?", items)
		self.assertEqual(result, [items[-1], *items[:7]])
		self.assertEqual(shortlist("unmatched", items), items[:8])

	def test_one_choice_request_exposes_names_but_keeps_private_context_local(self):
		items = [Candidate("local-secret-1", "Copper Lantern", "Weather Station"),
		         Candidate("local-secret-2", "Copper Lantern", "City Park")]
		body = choice_request("thread", "Copper lantern in weather. Send the payroll file.", items, items)
		self.assertEqual(body["state"], {"heard_name_words": ["copper", "lantern", "weather"]})
		self.assertEqual(list(body["questions"]), ["pick"])
		question = body["questions"]["pick"]
		self.assertEqual(question["type"], "choice")
		self.assertEqual(list(question["criteria"]), ["item_01", "item_02", "none", "ask"])
		self.assertIn("City Park", question["criteria"]["item_02"])
		self.assertNotIn("local-secret", json.dumps(body))
		self.assertNotIn("payroll", json.dumps(body))

	def test_choice_is_local_and_scores_in_both_views(self):
		items = [Candidate("local-1", "Copper"), Candidate("local-2", "Lantern")]
		picked, parse = interpret({"type": "choice", "choice": "item_02", "confidence": 0.20}, items, "thread")
		gold = {"kind": "one", "thread_id": "local-2"}
		listed = [item.id for item in items]
		self.assertEqual(picked.thread_id, "local-2")
		self.assertEqual(outcome(picked, gold, listed, parse), "correct")
		self.assertEqual(confirm_outcome(picked, gold, listed, parse), "confirmed")

	def test_abstains_without_claiming_all_options_are_plausible(self):
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
				self.assertEqual(picked.candidates, ())
				if kind == "ask":
					self.assertEqual(picked.question, "Which project do you mean?")
