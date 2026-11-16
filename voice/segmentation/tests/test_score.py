import unittest

from voice.segmentation.score import score

EXPECTED = {"tasks": [
    {"key": "T1", "match": ["height", "elbert"], "status": ["superseded"], "never_dispatched": True},
    {"key": "T2", "match": ["height", "massive"], "status": ["dispatched"], "revises": "T1"},
], "clarification_needed": False}


def record(task_id, status, segment_text, instruction, revises=None):
    return {"task_id": task_id, "status": status, "segment_text": segment_text, "instruction": instruction,
            "sources": ["orchestrator"], "depends_on": [], "revises": revises, "side_effect": False, "history": []}


class Matching(unittest.TestCase):
    def test_a_correction_matches_on_its_corrected_instruction_not_its_segment_words(self):
        records = [record("task_1", "superseded", "Look up the height of Mount Elbert,",
                          "Look up the height of Mount Elbert,"),
                   record("task_2", "dispatched", "actually Mount Massive.",
                          "Look up the height of Mount Elbert, (corrected: actually Mount Massive.)", revises="task_1")]
        log = [{"task_id": "task_2", "revision": 3, "is_final": True, "instruction": records[1]["instruction"],
                "sources": ["orchestrator"]}]
        result = score(EXPECTED, records, log, {"clarification_needed": None})
        self.assertTrue(result["pass"], result["failures"])

    def test_a_task_whose_instruction_lacks_the_words_still_fails(self):
        records = [record("task_1", "superseded", "x", "Look up the height of Mount Elbert,"),
                   record("task_2", "dispatched", "height massive", "actually Mount Massive.", revises="task_1")]
        result = score(EXPECTED, records, [], {"clarification_needed": None})
        self.assertIn("T2: no task matches ['height', 'massive']", result["failures"])


if __name__ == "__main__":
    unittest.main()
