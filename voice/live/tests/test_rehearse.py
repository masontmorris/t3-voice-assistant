"""The rehearsal's scoring: a thread is started only by a launch that carries this call's request, and a line the voice
said is judged against what the scripted seam recorded."""

import tempfile
import unittest
from pathlib import Path

from voice.live import rehearse as R


def line(kinds, reply, delivery="sent"):
    return {"kinds": kinds, "draft": reply, "draft_problems": [], "reply": reply, "status": "completed",
            "delivery": delivery}


def launch(call, payload=None, overlap=None, project="p1"):
    payload = payload if payload is not None else R.REQUESTS[call]
    return {"at": 0, "project": project, "instruction": payload, "call": call,
            "overlap": R.overlap(R.REQUESTS[call], payload) if overlap is None else overlap, "thread": f"t{call + 1}"}


class Scoring(unittest.TestCase):
    def turns(self, *entries):
        return [{"t3": list(entries)}]

    def test_a_launch_that_carries_the_request_starts_the_thread(self):
        graded = R.grade(self.turns(line(["confirm_launch"], "Should I start a new coding thread in Voice sandbox?"),
                                    line(["launched"], "A new coding thread has started in Voice sandbox.")),
                         0, [launch(0)])
        self.assertTrue(graded["started"])
        self.assertEqual((graded["false_claims"], graded["first_break"]), ([], None))

    def test_a_launch_with_another_calls_request_or_a_fragment_does_not(self):
        for payload in (R.REQUESTS[1], "Uhm, I was wondering if you could start a thread to get work going on."):
            with self.subTest(payload=payload[:30]):
                graded = R.grade(self.turns(line(["launched"], "A new coding thread has started in Voice sandbox.")),
                                 0, [launch(1 if payload == R.REQUESTS[1] else 0, payload)])
                self.assertFalse(graded["started"])
                self.assertEqual(graded["false_claims"], ["A new coding thread has started in Voice sandbox."],
                                 "and the line that says it started is false")

    def test_a_launch_in_another_project_does_not(self):
        graded = R.grade(self.turns(), 0, [launch(0, project="p2")])
        self.assertFalse(graded["started"])

    def test_a_status_line_is_true_only_for_the_thread_this_call_launched(self):
        started = launch(0)
        good = R.grade(self.turns(line(["thread_status"], f"{R.THREADS[0]} is still working.")), 0, [started])
        self.assertTrue(good["status_answered"])
        wrong = R.grade(self.turns(line(["thread_status"], f"{R.THREADS[1]} is still working.")), 0, [started])
        self.assertFalse(wrong["status_answered"])
        self.assertEqual(len(wrong["false_claims"]), 1)
        none = R.grade(self.turns(line(["thread_status"], f"{R.THREADS[0]} is still working.")), 0, [])
        self.assertEqual((none["status_answered"], len(none["false_claims"])), (False, 1))

    def test_a_mixed_turn_counts_the_lookup_heard_only_when_a_reply_says_its_forecast_high(self):
        record = {"executions": {"task_1": {"result_summary": "Forecast for Denver. Friday: a high of 47 degrees.",
                                            "tool": {"tool": "weather", "outcome": "ok"}}}}
        asked = dict(line(["confirm_launch"], "Should I start a new coding thread in Voice sandbox?"), relays=1)
        row = {"path": "turn_003.json", "highs": R.forecast_highs(record), "t3": [asked],
               "replies": [("snapshot", None, "Tomorrow in Denver it will reach 47 degrees."), ("t3", "sent", "...")]}
        self.assertEqual(R.mixed_turn(row), {"path": "turn_003.json", "first_relay_recited": True, "t3_said": True,
                                             "lookup_heard": True, "both_heard": True,
                                             "free_lines_naming_a_thread": []})
        unheard = {**row, "replies": [("snapshot", None, "I can't start a thread."), ("t3", "sent", "...")],
                   "t3": [dict(asked, relays=2)]}
        self.assertEqual({k: v for k, v in R.mixed_turn(unheard).items() if k != "path"},
                         {"first_relay_recited": False, "t3_said": True, "lookup_heard": False, "both_heard": False,
                          "free_lines_naming_a_thread": ["I can't start a thread."]})
        self.assertIsNone(R.mixed_turn({**row, "t3": []}), "a turn with no T3 line is not a mixed turn")

    def test_the_seam_knows_no_thread_until_a_launch_makes_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            seam = R.ScriptedSeam(Path(tmp) / "launches.json")
            self.assertEqual(seam.list_threads().threads, ())
            seam.launch("r1", "p1", R.REQUESTS[7])
            listed = seam.list_threads().threads
            self.assertEqual([t.title for t in listed], [R.THREADS[7]])
            self.assertEqual(seam.launches[0]["call"], 7)


if __name__ == "__main__":
    unittest.main()
