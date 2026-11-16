import unittest
from types import SimpleNamespace

from voice.prototype.backlog import Backlog


def executions(**reported):
    return {task_id: SimpleNamespace(reported=told) for task_id, told in reported.items()}


class DecideTest(unittest.TestCase):
    def test_results_the_person_has_not_heard_are_told_in_one_merged_update(self):
        backlog = Backlog()
        backlog.record("task_1", "running", "completed", "Forecast: sunny.", 1.0, "first reply")
        backlog.record("task_2", "running", "failed", "No place given.", 1.2, "first reply")
        tell = backlog.decide(backlog.drain(), executions(task_1=False, task_2=False), 2.0)
        self.assertEqual([e.task_id for e in tell], ["task_1", "task_2"])
        self.assertEqual(len(backlog.history), 1)
        self.assertTrue(backlog.history[0]["reprompt"])

    def test_a_result_already_told_and_a_cancellation_do_not_re_prompt(self):
        backlog = Backlog()
        backlog.record("task_1", "running", "completed", "Forecast: sunny.", 1.0, "first reply")
        backlog.record("task_2", "running", "cancelled", None, 1.1, "first reply")
        tell = backlog.decide(backlog.drain(), executions(task_1=True, task_2=False), 2.0)
        self.assertEqual(tell, [])
        self.assertFalse(backlog.history[0]["reprompt"])

    def test_draining_empties_the_backlog_so_the_next_reply_starts_a_new_one(self):
        backlog = Backlog()
        backlog.record("task_1", "running", "completed", "x", 1.0, "first reply")
        self.assertEqual(len(backlog.drain()), 1)
        self.assertEqual(backlog.drain(), [])


if __name__ == "__main__":
    unittest.main()
