import tempfile
import unittest
from pathlib import Path
from unittest import mock

from voice.segmentation import client as api


class ScriptedClient(api.Client):
    """The real call loop, with HTTP replaced by a script of statuses."""

    def __init__(self, statuses):
        super().__init__()
        self.statuses = list(statuses)

    def _send(self, method, path, payload):
        status = self.statuses.pop(0)
        return {"status": status, "body": {"answers": {}, "usage": {"cost": 0}}, "started_at": "t", "ms": 1.0,
                "connect_ms": None, "retry_after": None}


class Pacing(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        patches = [mock.patch.object(api, "CACHE", tmp / "cache"), mock.patch.object(api, "LEDGER", tmp / "calls.jsonl"),
                   mock.patch.object(api, "STOP", tmp / "STOP"), mock.patch.object(api, "BUDGET", 100)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.sleeps = []
        sleep = mock.patch.object(api.time, "sleep", side_effect=self.sleeps.append)
        sleep.start()
        self.addCleanup(sleep.stop)

    def body(self, n):
        return {"model": api.MODEL, "state": {"n": n}, "questions": {}}

    def test_calls_go_out_back_to_back_until_a_429(self):
        client = ScriptedClient([200, 200, 200])
        for n in range(3):
            client.call("decisions", self.body(n))
        self.assertEqual(self.sleeps, [])

    def test_a_429_backs_off_then_the_gap_decays_on_success(self):
        client = ScriptedClient([429, 429, 200, 200, 200, 200])
        client.call("decisions", self.body(0))
        self.assertEqual([round(s) for s in self.sleeps], [2, 4])
        for n in range(1, 4):
            client.call("decisions", self.body(n))
        self.assertEqual(client.pacer.gap_s, 0.0)
        self.assertEqual(len(api.LEDGER.read_text().splitlines()), 6)

    def test_a_cached_request_makes_no_attempt(self):
        client = ScriptedClient([200])
        client.call("decisions", self.body(0))
        client.call("decisions", self.body(0))
        self.assertEqual(len(api.LEDGER.read_text().splitlines()), 1)

    def test_a_lane_makes_one_attempt_and_sleeps_for_no_retry(self):
        lane = ScriptedClient([]).lane(2.0)
        with mock.patch.object(api.Client, "_send", return_value={
                "status": 503, "body": {}, "started_at": "t", "ms": 1.0, "connect_ms": None, "retry_after": None}):
            record = lane.call("decisions", self.body(0))
        self.assertEqual([entry["status"] for entry in record["attempts"]], [503])
        self.assertEqual(self.sleeps, [])

    def test_a_non_free_model_is_refused_before_any_attempt(self):
        with self.assertRaises(AssertionError):
            ScriptedClient([200]).call("decisions", {"model": "paid/model", "state": {}, "questions": {}})


if __name__ == "__main__":
    unittest.main()
