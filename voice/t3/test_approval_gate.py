"""The gate on an approval's description, on the local d1-2B's recorded answers (voice/t3/fixtures/approval_gate.json,
made-up requests): a faithful description passes, and a description that is wrong, generic, of another command, or that
leaves out or softens a deletion does not. The summary gate the approvals used before is replayed beside it."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from voice.t3 import local_summary as LS
from voice.t3 import types as T
from voice.t3.eval import approval_cases as AC
from voice.t3.eval.record_approval_gate import SPLITS, key
from voice.t3.verify import noul

ROWS = json.loads((Path(__file__).parent / "fixtures" / "approval_gate.json").read_text(encoding="utf-8"))
BAR = LS.REQUEST_THRESHOLD


def scores(split):
    out = []
    for row in ROWS:
        if row["split"] == split:
            request = LS.doubt({"status": 200, "response": {"answers": row["gates"]["request"]["answers"]}})
            summary = LS.unsupported({"status": 200, "response": {"answers": row["gates"]["summary"]["answers"]}})
            out.append((row, request, summary))
    return out


class RecordingTest(unittest.TestCase):
    def test_the_recording_is_of_these_cases_and_these_questions(self):
        expected = {(split, row["case"], row["text"], row["kind"]) for split, cases in SPLITS
                    for row in AC.descriptions(cases)}
        self.assertEqual({(r["split"], r["case"], r["text"], r["kind"]) for r in ROWS}, expected,
                         "the cases changed: re-run record_approval_gate")
        for split, cases in SPLITS:
            by_name = {c["name"]: c for c in cases}
            for row in (r for r in ROWS if r["split"] == split):
                state = AC.state(by_name[row["case"]])
                self.assertEqual(row["gates"]["request"]["hash"], key(LS.request_gate(state, row["text"])),
                                 "the approval gate's question changed: re-run record_approval_gate")
                self.assertEqual(row["gates"]["summary"]["hash"], key(LS.gate_request(state, row["text"])))


class BarTest(unittest.TestCase):
    def count(self, split, gate):
        index = 1 if gate == "request" else 2
        bar = BAR if gate == "request" else LS.GATE_THRESHOLD
        rows = [(r[0], r[index]) for r in scores(split)]
        kept = sum(p < bar for r, p in rows if r["faithful"])
        leaked = sorted((r["kind"], r["case"]) for r, p in rows if not r["faithful"] and p < bar)
        return kept, sum(r["faithful"] for r, _ in rows), leaked

    def test_no_misleading_description_passes_the_approval_gate_and_about_half_of_the_faithful_ones_do(self):
        # SEEN chose the question and the bar; HELD_OUT was written before its first run.
        self.assertEqual(self.count("seen", "request"), (10, 20, []))
        self.assertEqual(self.count("held_out", "request"), (13, 28, []))

    def test_the_summary_gate_let_softened_and_incomplete_descriptions_through(self):
        kept, total, leaked = self.count("seen", "summary")
        self.assertEqual((kept, total), (8, 20))
        self.assertEqual([k for k, _ in leaked].count("harmless") + [k for k, _ in leaked].count("omits"), 5)
        kept, total, leaked = self.count("held_out", "summary")
        self.assertEqual((kept, total, len(leaked)), (11, 28, 5))

    def test_a_missing_or_malformed_answer_is_no_score(self):
        for record in ({"status": 500, "response": {}}, {"status": 200, "response": {"answers": {}}},
                       {"status": 200, "response": {"answers": {"accurate": {"type": "noul", "noul": True}}}}):
            self.assertIsNone(LS.doubt(record))
        self.assertAlmostEqual(LS.doubt({"status": 200, "response": {"answers": {"accurate": {"noul": 0.96}}}}), 0.04)


class DescribesTest(unittest.TestCase):
    def state(self, kind=T.PendingKind.APPROVAL, reply=None, pending=True):
        need = T.PendingNeed("q1", kind, "pnpm test", T.NeedSource.COMMAND) if pending else None
        return T.ThreadState("t1", "p1", "A title", T.ThreadActivity.WAITING_ON_YOU, None, need, reply, 1)

    def test_only_an_approval_with_no_reply_to_read_is_described_from_the_request(self):
        reply = T.LastReply("Done.", "m1", None, "r1", "2026-11-18T10:00:00Z")
        self.assertTrue(LS.describes_request(self.state()))
        for name, state in {"a reply is there": self.state(reply=reply),
                            "a question waits": self.state(kind=T.PendingKind.QUESTION),
                            "nothing waits": self.state(pending=False)}.items():
            self.assertFalse(LS.describes_request(state), name)

    def test_the_model_is_shown_the_request_by_its_source_and_never_the_title(self):
        shown = LS.request_messages(self.state())[-1]["content"]
        self.assertIn('"kind": "a shell command"', shown)
        self.assertIn("pnpm test", shown)
        self.assertNotIn("A title", shown)
        for kind, source in ((T.PendingKind.APPROVAL, T.NeedSource.PERMISSION),):
            need = T.PendingNeed("q1", kind, "mcp__notes__append_note", source)
            state = T.ThreadState("t1", "p1", "A title", T.ThreadActivity.WAITING_ON_YOU, None, need, None, 1)
            self.assertEqual(LS.request_view(state), {"kind": "permission to use a tool",
                                                      "text": "mcp__notes__append_note"})

    def test_a_description_is_only_what_the_narrower_schema_allows(self):
        self.assertEqual(LS.read_description('{"gist": "It wants to run the tests, then stop."}'),
                         "It wants to run the tests, then stop.")
        for text in ('{"gist": "It wants to edit src/app.py."}', '{"gist": "It wants to run npm test --watch=false."}',
                     '{"gist": "It wants to read https://example.com."}', '{"gist": "It wants to run pnpm_build."}'):
            self.assertIsNone(LS.read_description(text), text)


if __name__ == "__main__":
    unittest.main()
