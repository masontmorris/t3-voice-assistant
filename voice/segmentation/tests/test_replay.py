"""Replay every committed run from recorded decider responses, with no network, and compare to its summary."""

import json
import unittest

from voice.segmentation.run import RESULTS, SCENARIOS, OfflineClient, Runner

RUNS = {
    "final-a": ("a", "final", False, None), "final-b": ("b", "final", False, None),
    "final-c": ("c", "final", False, None), "stream-a": ("a", "stream", False, None),
    "final-a-separate": ("a", "final", True, None), "final-a-repeat1": ("a", "final", False, 1),
    "stream-a-repeat1": ("a", "stream", False, 1),
}


class Replay(unittest.TestCase):
    def test_recorded_runs_reproduce_offline(self):
        scenarios = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(SCENARIOS.glob("*.json"))]
        for name, (option, mode, separate, repeat) in RUNS.items():
            committed = {r["scenario"]: r for r in
                         json.loads((RESULTS / name / "summary.json").read_text(encoding="utf-8"))}
            runner = Runner(option, OfflineClient(), separate, repeat)
            for scenario in scenarios:
                with self.subTest(run=name, scenario=scenario["id"]):
                    result = runner.run(scenario, mode)
                    expected = committed[scenario["id"]]
                    self.assertEqual(result["score"]["pass"], expected["pass"])
                    self.assertEqual(result["score"]["failures"], expected["failures"])
                    recorded = json.loads((RESULTS / name / f"{scenario['id']}.json").read_text(encoding="utf-8"))
                    self.assertEqual(result["tasks"], recorded["tasks"])
                    self.assertEqual(result["snapshot"], recorded["snapshot"])


if __name__ == "__main__":
    unittest.main()
