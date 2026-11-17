"""The gate eval's own scoring: the tool a dispatch runs for an item, against the label.

A correction turn asks the decider nothing (live mode only asks it when the utterance is a request), so a correction
item's tool comes from the registry alone: the corrected task's own tool, which is what live mode re-runs.
"""

import json
import unittest

from voice.research.segmentation.live_gate_eval import EVAL, rules_tool


def items():
    lines = EVAL.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


class CorrectionToolTest(unittest.TestCase):
    def test_every_correction_item_runs_the_tool_its_label_names(self):
        wrong = [(item["id"], item["tool"], rules_tool(item)) for item in items()
                 if item["category"] == "correction" and rules_tool(item) != item["tool"]]
        self.assertEqual(wrong, [])


if __name__ == "__main__":
    unittest.main()
