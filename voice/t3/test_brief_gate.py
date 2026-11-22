"""The gate on a started thread's first message, on the local d1-2B's recorded answers (voice/t3/fixtures/brief_gate.json,
made-up requests, voice/t3/eval/record_brief_gate.py): which hand-written briefs pass, by kind, under the shipped gate
and the gates before it, and how often LFM3-1.7B's own first draft passes. The numbers are what the docs report; a
re-recording that moves them fails here until the docs are updated."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from voice.t3 import local_summary as LS
from voice.t3.eval import brief_cases as BC
from voice.t3.eval.record_brief_gate import GATES, bodies, key, shipped_gate
from voice.t3.verify import noul

ROWS = json.loads((Path(__file__).parent / "fixtures" / "brief_gate.json").read_text(encoding="utf-8"))
REQUESTS = {c["name"]: c["request"] for _, cases in BC.SPLITS for c in cases}
SHIPPED = shipped_gate("a request", "a task")


def passed(gates, gate):
    record = {"status": 200, "response": {"answers": gates[gate]["answers"]}}
    if gate == SHIPPED:
        return LS.brief_passes(record)
    bars = GATES[gate][2]
    return all((score := noul(record, name)) is not None and score < bar for name, bar in bars.items())


def counts(split, gate):
    rows = [r for r in ROWS if r["split"] == split and r["kind"] != "written"]
    out = {k: (sum(passed(r["gates"], gate) for r in rows if r["kind"] == k), sum(r["kind"] == k for r in rows))
           for k in ("faithful", *BC.KINDS)}
    written = [r for r in ROWS if r["split"] == split and r["kind"] == "written"]
    out["written"] = (sum(bool(r["gates"]) and passed(r["gates"], gate) for r in written), len(written))
    return out


class RecordingTest(unittest.TestCase):
    def test_the_recording_is_of_these_cases_and_these_questions(self):
        expected = {(split, row["case"], row["kind"], row["text"]) for split, cases in BC.SPLITS
                    for row in BC.briefs(cases)}
        self.assertEqual({(r["split"], r["case"], r["kind"], r["text"]) for r in ROWS if r["kind"] != "written"},
                         expected, "the cases changed: re-run record_brief_gate")
        for row in ROWS:
            if row["gates"]:
                for gate, body in bodies(REQUESTS[row["case"]], row["text"]).items():
                    self.assertEqual(row["gates"][gate]["hash"], key(body),
                                     f"the {gate} gate changed: re-run record_brief_gate")

    def test_the_shipped_gate_is_one_of_those_recorded(self):
        self.assertEqual(SHIPPED, "keep_drops")

    def test_no_case_is_a_worked_example(self):
        examples = " ".join(request for request, _ in LS.BRIEF_EXAMPLES)
        for request in REQUESTS.values():
            self.assertNotIn(request, examples)


class BarTest(unittest.TestCase):
    def test_passed_of_scored_by_kind_for_each_gate(self):
        for gate, split, expected in EXPECTED:
            with self.subTest(gate=gate, split=split):
                self.assertEqual(counts(split, gate), expected)


# Passed of scored, by kind of brief, and LFM3-1.7B's first drafts. `context` shipped first on 2026-11-22; `keep_drops`
# (what must be kept, said in the context) replaced it after the review's `target`, `negation` and `limit` cases.
EXPECTED = [
    ("before", "seen", {"faithful": (3, 15), "adds": (0, 6), "drops": (0, 3), "starts": (0, 6), "target": (0, 7),
                        "negation": (0, 3), "limit": (1, 3), "written": (6, 27)}),
    ("before", "held_out", {"faithful": (0, 12), "adds": (0, 5), "drops": (0, 3), "starts": (0, 5), "target": (0, 6),
                            "negation": (0, 3), "limit": (0, 2), "written": (0, 24)}),
    ("context", "seen", {"faithful": (15, 15), "adds": (0, 6), "drops": (0, 3), "starts": (0, 6), "target": (4, 7),
                         "negation": (0, 3), "limit": (3, 3), "written": (24, 27)}),
    ("context", "held_out", {"faithful": (11, 12), "adds": (0, 5), "drops": (0, 3), "starts": (0, 5), "target": (0, 6),
                             "negation": (0, 3), "limit": (0, 2), "written": (21, 24)}),
    # The two seen limits that pass drop "short" and "only ... nothing else".
    ("keep_drops", "seen", {"faithful": (15, 15), "adds": (0, 6), "drops": (0, 3), "starts": (0, 6), "target": (0, 7),
                            "negation": (0, 3), "limit": (2, 3), "written": (24, 27)}),
    ("keep_drops", "held_out", {"faithful": (8, 12), "adds": (0, 5), "drops": (0, 3), "starts": (0, 5),
                                "target": (0, 6), "negation": (0, 3), "limit": (0, 2), "written": (15, 24)}),
]


if __name__ == "__main__":
    unittest.main()
