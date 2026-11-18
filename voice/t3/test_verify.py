"""The decider's line check, on recorded d1.2 answers: the reviewer's three escapes and every status claim the old
word lists refused now fail, the right lines pass, and a decider that cannot answer never lets a line through."""

from __future__ import annotations

import json
import socket
import unittest
from pathlib import Path

from voice.liquid_decider.client import BillingStop, SpendStop
from voice.t3.acts import Act, Kind, act
from voice.t3.speech import Lane, RenderedLine, Speech
from voice.t3.verify import Decider, Verdict, asks, request

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "verify_lines.json").read_text(encoding="utf-8"))
ESCAPES = {"Alpha is complete?", "Alpha has failed.", "Beta is running. Alpha is unrelated."}
# The one right line d1.2 refused when the fixture was recorded: "running" is not among its act's facts. A refusal
# fails safe, so the act goes to LFM3 and then waits; it is kept here so a change in it shows.
REFUSED_RIGHT_LINES = {"Okay, I'll leave Add a clamp helper running."}


class Recorded:
    """A decider client that answers each request from the fixture, and only one whose questions it recorded."""

    def __init__(self, rows=FIXTURE):
        self.rows = {row["line"]: row for row in rows}
        self.bodies = []

    def call(self, route, body, label=""):
        self.bodies.append(body)
        row = self.rows[body["state"]["line"]]
        assert set(body["questions"]) == set(row["answers"]), "the questions changed: re-run record_verify"
        return {"status": row["status"], "response": {"answers": row["answers"]}}


class Failing:
    def __init__(self, error):
        self.error = error

    def call(self, route, body, label=""):
        raise self.error


def acts_of(row):
    return [Act.from_json(a) for a in row["acts"]]


class LineCheck(unittest.TestCase):
    def test_the_reviewers_escapes_pass_code_and_fail_the_decider(self):
        decider = Decider(Recorded())
        escapes = [row for row in FIXTURE if row["line"] in ESCAPES]
        self.assertEqual({row["line"] for row in escapes}, ESCAPES)
        for row in escapes:
            with self.subTest(row["line"]):
                rendered = Speech(None, lambda thread_id: None, decider).accept(acts_of(row), row["line"], "text_model")
                self.assertIsNone(rendered.line)
                self.assertTrue(rendered.verdict is not None and rendered.verdict.failed, rendered)

    def test_no_wrong_line_passes_and_the_right_lines_do(self):
        decider = Decider(Recorded())
        for row in FIXTURE:
            with self.subTest(row["line"]):
                verdict = decider.verify(acts_of(row), row["line"])
                expected = row["should_pass"] and row["line"] not in REFUSED_RIGHT_LINES
                self.assertEqual(verdict.passed, expected, verdict)
        self.assertEqual(sum(not row["should_pass"] for row in FIXTURE), 20, "3 escapes and 17 old word-list claims")

    def test_the_questions_come_from_the_act(self):
        stop = asks([act(Kind.CONFIRM_STOP, thread="Alpha")])
        self.assertEqual([(a.name, a.want) for a in stop],
                         [("a1_about", True), ("a1_means", True), ("claims_stopped", False), ("invents", False)])
        self.assertIn("whether to stop Alpha", stop[1].question)
        body = request([act(Kind.THREAD_STATUS, thread="Alpha", status="working")], "Alpha is busy.")
        self.assertEqual(body["state"]["facts"], [{"act": "thread_status", "thread": "Alpha", "status": "working"}])
        self.assertTrue(all(q["type"] == "noul" for q in body["questions"].values()))

    def test_a_decider_that_cannot_answer_never_passes_a_line(self):
        stop = [act(Kind.CONFIRM_STOP, thread="Alpha")]
        for error in (socket.timeout("timed out"), TimeoutError(), SpendStop("cap"), BillingStop("402")):
            with self.subTest(type(error).__name__):
                verdict = Decider(Failing(error)).verify(stop, "Want me to stop Alpha?")
                self.assertFalse(verdict.passed)
                self.assertIn(type(error).__name__, verdict.error)
        rate_limited = Decider(type("Limited", (), {"call": lambda self, *a, **k: {"status": 429}})())
        self.assertFalse(rate_limited.verify(stop, "Want me to stop Alpha?").passed)
        unanswered = Decider(type("Empty", (), {"call": lambda self, *a, **k: {"status": 200, "response": {}}})())
        self.assertFalse(unanswered.verify(stop, "Want me to stop Alpha?").passed)

    def test_a_decider_timeout_sends_the_line_to_the_fallback_never_to_speech(self):
        class Text:
            model = "fake/text:free"

            def complete(self, messages):
                return {"text": "Want me to stop Alpha?"}

        speech = Speech(Lane(Text()), lambda thread_id: None, Decider(Failing(socket.timeout("timed out"))))
        rendered = speech.render([act(Kind.CONFIRM_STOP, thread="Alpha")])
        self.assertIsNone(rendered.line, "a line the decider never answered for is not spoken")
        self.assertEqual(rendered.raw, "Want me to stop Alpha?")
        self.assertIn("TimeoutError", rendered.error)
        self.assertIsNone(speech.accept([act(Kind.CONFIRM_STOP, thread="Alpha")], "Should I stop Alpha?", "lfm3").line,
                          "LFM3's wording meets the same timeout and waits")


class OnlyAMintedLineIsSpoken(unittest.TestCase):
    def test_a_rendered_line_cannot_be_made_outside_the_check(self):
        with self.assertRaises(TypeError):
            RenderedLine("It finished.", (), "text_model", Verdict(True))


if __name__ == "__main__":
    unittest.main()
