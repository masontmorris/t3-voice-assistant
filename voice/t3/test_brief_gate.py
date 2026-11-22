"""The gate on a started thread's first message, on the local d1-2B's recorded answers (voice/t3/fixtures/brief_gate.json,
made-up requests, voice/t3/eval/record_brief_gate.py): which hand-written briefs pass at the shipped bars, by kind, the
same under the gate as it was before, and how often LFM3-1.7B's own briefs reach the thread. The numbers are what the
docs report; a re-recording that moves them fails here until the docs are updated."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from voice.t3 import local_summary as LS
from voice.t3.eval import brief_cases as BC
from voice.t3.eval.record_brief_gate import BEFORE_BARS, bodies, key

ROWS = json.loads((Path(__file__).parent / "fixtures" / "brief_gate.json").read_text(encoding="utf-8"))


def passed(gates, gate="shipped"):
    record = {"status": 200, "response": {"answers": gates[gate]["answers"]}}
    if gate == "shipped":
        return LS.brief_passes(record)
    from voice.t3.verify import noul
    return all((score := noul(record, name)) is not None and score < bar for name, bar in BEFORE_BARS.items())


def counts(split, gate):
    rows = [r for r in ROWS if r["split"] == split and r["kind"] != "written"]
    return {k: (sum(passed(r["gates"], gate) for r in rows if r["kind"] == k), sum(r["kind"] == k for r in rows))
            for k in ("faithful", *BC.KINDS)}


def handed(split, gate):
    """Of the writer's briefs, how many reach the thread: a first draft that passed, or a retry that did."""
    rows = [r for r in ROWS if r["split"] == split and r["kind"] == "written"]
    first = sum(bool(r["drafts"][0]["gates"]) and passed(r["drafts"][0]["gates"], gate) for r in rows)
    either = sum(any(d["gates"] and passed(d["gates"], gate) for d in r["drafts"]) for r in rows)
    return first, either, len(rows)


class RecordingTest(unittest.TestCase):
    def test_the_recording_is_of_these_cases_and_these_questions(self):
        expected = {(split, row["case"], row["kind"], row["text"]) for split, cases in BC.SPLITS
                    for row in BC.briefs(cases)}
        self.assertEqual({(r["split"], r["case"], r["kind"], r["text"]) for r in ROWS if r["kind"] != "written"},
                         expected, "the cases changed: re-run record_brief_gate")
        requests = {c["name"]: c["request"] for _, cases in BC.SPLITS for c in cases}
        for row in ROWS:
            for gates, text in ([(row["gates"], row["text"])] if row["kind"] != "written" else
                                [(d["gates"], d["text"]) for d in row["drafts"] if d["gates"]]):
                for gate, body in bodies(requests[row["case"]], text).items():
                    self.assertEqual(gates[gate]["hash"], key(body), f"the {gate} gate changed: re-run record_brief_gate")

    def test_no_case_is_a_worked_example(self):
        examples = " ".join(request for request, _ in LS.BRIEF_EXAMPLES)
        for _, cases in BC.SPLITS:
            for c in cases:
                self.assertNotIn(c["request"], examples)


class BarTest(unittest.TestCase):
    def test_the_shipped_gate_passes_faithful_briefs_and_refuses_added_and_started_ones(self):
        self.assertEqual(counts("seen", "shipped"), SHIPPED_SEEN)
        self.assertEqual(counts("held_out", "shipped"), SHIPPED_HELD)

    def test_the_gate_before_refused_most_faithful_briefs(self):
        self.assertEqual(counts("seen", "before"), BEFORE_SEEN)
        self.assertEqual(counts("held_out", "before"), BEFORE_HELD)

    def test_how_often_the_writer_s_own_brief_reaches_the_thread(self):
        self.assertEqual({split: handed(split, "shipped") for split, _ in BC.SPLITS}, WRITTEN_SHIPPED)
        self.assertEqual({split: handed(split, "before") for split, _ in BC.SPLITS}, WRITTEN_BEFORE)


# Passed of scored, by kind of brief. The two seen `drops` that pass leave out one word of detail ("short", "settings").
SHIPPED_SEEN = {"faithful": (12, 12), "adds": (0, 6), "drops": (2, 6), "starts": (0, 6)}
SHIPPED_HELD = {"faithful": (8, 9), "adds": (0, 5), "drops": (0, 5), "starts": (0, 5)}
BEFORE_SEEN = {"faithful": (2, 12), "adds": (0, 6), "drops": (0, 6), "starts": (0, 6)}
BEFORE_HELD = {"faithful": (0, 9), "adds": (0, 5), "drops": (0, 5), "starts": (0, 5)}
# For the writer: (first drafts passed, briefs handed after one retry, briefs written). Every one it did not hand kept
# "start a thread" in a hesitant request, and the thread got the framed request instead.
WRITTEN_SHIPPED = {"seen": (15, 17, 18), "held_out": (12, 12, 15)}
WRITTEN_BEFORE = {"seen": (3, 3, 18), "held_out": (0, 0, 15)}


if __name__ == "__main__":
    unittest.main()
