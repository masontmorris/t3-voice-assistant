"""The gate on an approval's description, on the local d1-2B's recorded answers (voice/t3/fixtures/approval_gate.json,
made-up requests): which descriptions pass at the shipped bar, by kind, and which unfaithful one does. The summary gate
the approvals used before is replayed beside it. The numbers are what the docs report; a re-recording that moves them
fails here until the docs are updated."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from voice.t3 import local_summary as LS
from voice.t3 import types as T
from voice.t3.eval import approval_cases as AC
from voice.t3.eval.record_approval_gate import bodies, key

ROWS = json.loads((Path(__file__).parent / "fixtures" / "approval_gate.json").read_text(encoding="utf-8"))


def passed(row, gate):
    record = {"status": 200, "response": {"answers": row["gates"][gate]["answers"]}}
    if gate == "request":
        return LS.doubt(record) < LS.REQUEST_THRESHOLD
    return LS.unsupported(record) < LS.GATE_THRESHOLD


def counts(split, gate):
    rows = [r for r in ROWS if r["split"] == split]
    by_kind = {k: (sum(passed(r, gate) for r in rows if r["kind"] == k), sum(r["kind"] == k for r in rows))
               for k in ("faithful", *AC.KINDS)}
    return by_kind


class RecordingTest(unittest.TestCase):
    def test_the_recording_is_of_these_cases_and_these_questions(self):
        expected = {(split, row["case"], row["text"], row["kind"]) for split, cases in AC.SPLITS
                    for row in AC.descriptions(cases)}
        self.assertEqual({(r["split"], r["case"], r["text"], r["kind"]) for r in ROWS}, expected,
                         "the cases changed: re-run record_approval_gate")
        for split, cases in AC.SPLITS:
            by_name = {c["name"]: c for c in cases}
            for row in (r for r in ROWS if r["split"] == split):
                for gate, body in bodies(by_name[row["case"]], row["text"]).items():
                    self.assertEqual(row["gates"][gate]["hash"], key(body),
                                     f"the {gate} gate's question changed: re-run record_approval_gate")

    def test_the_sources_are_only_the_describable_ones_and_no_hard_request_is_a_worked_example(self):
        for _, cases in AC.SPLITS:
            self.assertTrue({c["source"] for c in cases} <= {"command", "file-change"})
        examples = " ".join(r["text"] for r, _ in LS.REQUEST_EXAMPLES)
        for case in AC.HARD_HELD_OUT:
            self.assertNotIn(case["prompt"], examples)


class BarTest(unittest.TestCase):
    def test_the_approval_gate_passes_no_unfaithful_description_but_one_softened_one_on_the_hard_held_out_set(self):
        for split, faithful, unfaithful in (("seen", 8, 0), ("held_out", 11, 0), ("hard_held_out", 13, 1)):
            by_kind = counts(split, "request")
            self.assertEqual(by_kind["faithful"][0], faithful, split)
            self.assertEqual(sum(v[0] for k, v in by_kind.items() if k != "faithful"), unfaithful, split)
        passing = [(r["case"], r["kind"]) for r in ROWS if not r["faithful"] and passed(r, "request")]
        self.assertEqual(passing, [("dump_then_truncate", "harmless")], "the one that is reported, not tuned away")

    def test_the_hard_negatives_by_kind(self):
        seen, held = counts("seen", "request"), counts("hard_held_out", "request")
        self.assertEqual({k: seen[k] for k in AC.HARD}, {"harmless": (0, 6), "omits": (0, 6), "target": (0, 11),
                                                         "tail": (0, 3)})
        self.assertEqual({k: held[k] for k in AC.HARD}, {"harmless": (1, 11), "omits": (0, 11), "target": (0, 15),
                                                         "tail": (0, 5)})

    def test_the_summary_gate_the_approvals_used_before_let_through_many(self):
        leaked = {split: sum(v[0] for k, v in counts(split, "summary").items() if k != "faithful")
                  for split, _ in AC.SPLITS}
        self.assertEqual(leaked, {"seen": 11, "held_out": 5, "hard_held_out": 19})

    def test_a_missing_or_malformed_answer_is_no_score(self):
        for record in ({"status": 500, "response": {}}, {"status": 200, "response": {"answers": {}}},
                       {"status": 200, "response": {"answers": {"accurate": {"type": "noul", "noul": True}}}}):
            self.assertIsNone(LS.doubt(record))
        self.assertAlmostEqual(LS.doubt({"status": 200, "response": {"answers": {"accurate": {"noul": 0.96}}}}), 0.04)


class DescribesTest(unittest.TestCase):
    def need(self, source=T.NeedSource.COMMAND, kind=T.PendingKind.APPROVAL, prompt="pnpm test"):
        return T.PendingNeed("q1", kind, prompt, source)

    def test_a_yes_may_answer_an_approval_by_its_source_alone_and_only_some_are_described(self):
        described = {"a command": self.need(), "a file change": self.need(T.NeedSource.FILE_CHANGE),
                     "a tool permission": self.need(T.NeedSource.PERMISSION),
                     "a tool permission with a command for its text": self.need(T.NeedSource.PERMISSION, prompt="rm -rf dist")}
        asked_by_kind = {"a file read": self.need(T.NeedSource.FILE_READ)}
        app_only = {"a tool's input request": self.need(T.NeedSource.MCP_ELICITATION),
                    "a question": self.need(T.NeedSource.USER_INPUT, T.PendingKind.QUESTION),
                    "an in-app card": self.need(T.NeedSource.CARD, T.PendingKind.IN_APP)}
        for name, need in {**described, **asked_by_kind}.items():
            self.assertTrue(LS.approvable(need), name)
        for name, need in {**asked_by_kind, **app_only}.items():
            self.assertFalse(LS.describable(need), name)
            self.assertFalse(name in app_only and LS.approvable(need), name)
        for name, need in described.items():
            self.assertTrue(LS.describable(need), name)
        self.assertEqual(LS.request_view(self.need(T.NeedSource.PERMISSION))["kind"], "a tool it wants to use")

    def test_a_tool_is_named_only_by_a_single_stored_token_of_a_permission(self):
        tool = T.NeedSource.PERMISSION
        self.assertEqual(LS.tool_name(tool, "mcp__notes__list_pages"), "mcp__notes__list_pages")
        for source, prompt in ((tool, "rm -rf dist"), (tool, None), (tool, ""), (tool, "x" * 81),
                               (T.NeedSource.COMMAND, "pnpm")):
            self.assertIsNone(LS.tool_name(source, prompt), (source, prompt))

    def test_the_model_is_shown_the_request_by_its_kind_and_never_a_thread_title_or_reply(self):
        shown = LS.request_messages(self.need())[-1]["content"]
        self.assertIn('"kind": "a shell command"', shown)
        self.assertIn("pnpm test", shown)
        self.assertEqual(LS.request_view(self.need(T.NeedSource.FILE_CHANGE))["kind"], "a change to a file")

    def test_a_description_is_read_from_json_or_prose_and_nothing_here_judges_its_words(self):
        self.assertEqual(LS.read_description('{"gist": "It wants to run the tests,  then stop."}'),
                         "It wants to run the tests, then stop.")
        self.assertEqual(LS.read_description("It wants to run the tests."), "It wants to run the tests.")
        for text in ("", "   ", '{"gist": 3}', '{"other": "x"}', '["x"]', '{"gist": "%s"}' % ("x" * 300)):
            self.assertIsNone(LS.read_description(text), text)
        self.assertEqual(LS.read_description('{"gist": "It edits src/app.py."}'), "It edits src/app.py.")


if __name__ == "__main__":
    unittest.main()
