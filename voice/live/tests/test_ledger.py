import unittest

from voice.live.ledger import ConversationLedger


def record(turn, transcript, reply, task=None, execution=None):
    return {"clip": turn, "final_transcript": transcript,
            "timeline": [{"kind": "transcript_update", "revision": 1, "text": transcript, "is_final": True}],
            "replies": [{"text": reply}], "tasks": [task] if task else [],
            "executions": {task["task_id"]: execution} if task and execution else {},
            "snapshot_after_tools": {"tasks": [task] if task else []}}


class LedgerTest(unittest.TestCase):
    def test_turn_scoped_tasks_keep_proposals_and_outcomes_distinct(self):
        ledger = ConversationLedger()
        task = {"task_id": "task_1", "instruction": "weather", "status": "dispatched", "side_effect": False}
        result = {"state": "completed", "tool": {"tool": "weather", "arguments": {"place": "Denver"},
                                                   "outcome": "ok"}}
        ledger.record(record("turn_001", "Weather?", "Checking.", task, result), muted=False)
        ledger.record(record("turn_002", "What about Saturday?", "Checking.", task, result), muted=False)
        self.assertEqual([turn.proposals[0]["ref"] for turn in ledger.turns],
                         ["turn_001:task_1", "turn_002:task_1"])
        self.assertEqual([task["ref"] for task in ledger.tasks], ["turn_001:task_1", "turn_002:task_1"])
        self.assertEqual(ledger.turns[0].executions["turn_001:task_1"]["state"], "completed")
        self.assertEqual(ledger.turns[0].revisions[0]["text"], "Weather?")

    def test_interrupted_generated_reply_is_not_reseeded_as_a_complete_reply(self):
        ledger = ConversationLedger()
        ledger.record(record("turn_001", "Hello", "A long reply"), muted=True)
        self.assertEqual(ledger.turns[0].generated, ["A long reply"])
        self.assertEqual(ledger.turns[0].delivery, "interrupted")
        self.assertEqual(ledger.conversation(), [{"user": "Hello"}])

    def test_the_workers_context_has_both_sides_and_the_results(self):
        ledger = ConversationLedger()
        task = {"task_id": "task_1", "instruction": "weather", "status": "dispatched", "side_effect": False}
        done = {"state": "completed", "result_summary": "Forecast for San Diego: a high of 70 degrees.",
                "tool": {"tool": "weather", "arguments": {"place": "San Diego"}, "outcome": "ok"}}
        ledger.record(record("turn_001", "What about San Diego?", "Checking that.", task, done), muted=False)
        ledger.record({"clip": "turn_002", "cancelled": "continued"}, muted=True)
        ledger.record(record("turn_003", "Do I need a jacket?", "Probably not."), muted=False)
        self.assertEqual(ledger.context(), [
            {"user": "What about San Diego?", "assistant": ["Checking that."],
             "results": ["Forecast for San Diego: a high of 70 degrees."]},
            {"user": "Do I need a jacket?", "assistant": ["Probably not."]}])
        self.assertEqual(ledger.results(), ["Forecast for San Diego: a high of 70 degrees."])

    def test_queued_reply_is_not_assumed_played(self):
        ledger = ConversationLedger()
        ledger.record(record("turn_001", "Hello", "Hi"), muted=False)
        self.assertEqual(ledger.turns[0].delivery, "queued")
        self.assertEqual(ledger.conversation(), [{"user": "Hello"}])

    def test_failed_turn_does_not_erase_previous_snapshot(self):
        ledger = ConversationLedger()
        ledger.record(record("turn_001", "Hello", "Hi"), muted=False)
        ledger.record({"clip": "turn_002", "error": "network failed"}, muted=False)
        self.assertEqual(ledger.snapshot, {"tasks": []})
        self.assertEqual(ledger.turns[1].error, "network failed")

    def test_the_chosen_place_and_every_spoken_reply_reach_the_next_turn(self):
        ledger = ConversationLedger()
        picked = record("turn_001", "Pick a city on the east coast.", "Let's check the weather in Boston.")
        picked["established"] = ["Boston"]
        ledger.record(picked, muted=False)
        ledger.record(record("turn_002", "Do I need a jacket?", ""), muted=False)
        self.assertEqual(ledger.established(), ["Boston"])
        self.assertEqual(ledger.replies(), ["Let's check the weather in Boston."],
                         "a turn that said nothing adds no reply to screen against")


if __name__ == "__main__":
    unittest.main()
