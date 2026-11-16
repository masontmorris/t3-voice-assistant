"""Replay every committed run from recorded decider responses, with no network, and compare to its summary."""

import json
import unittest

from voice.segmentation.run import RESULTS, SETS, OfflineClient, make_runner

RUNS = {
    "final-a": ("a", "final", False, None, "original"), "final-b": ("b", "final", False, None, "original"),
    "final-c": ("c", "final", False, None, "original"), "stream-a": ("a", "stream", False, None, "original"),
    "final-a-separate": ("a", "final", True, None, "original"),
    "final-a-repeat1": ("a", "final", False, 1, "original"),
    "stream-a-repeat1": ("a", "stream", False, 1, "original"),
    "stream-e": ("e", "stream", False, None, "original"),
    "stream-a-heldout": ("a", "stream", False, None, "heldout"),
    "stream-e-heldout": ("e", "stream", False, None, "heldout"),
}


class Replay(unittest.TestCase):
    def test_recorded_runs_reproduce_offline(self):
        for name, (option, mode, separate, repeat, scenario_set) in RUNS.items():
            scenarios = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(SETS[scenario_set].glob("*.json"))]
            committed = {r["scenario"]: r for r in
                         json.loads((RESULTS / name / "summary.json").read_text(encoding="utf-8"))}
            runner = make_runner(option, OfflineClient(), separate, repeat)
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
