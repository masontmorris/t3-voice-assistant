"""The decider's line check, on recorded d1.2 answers: the reviewer's three escapes and every status claim the old
word lists refused now fail, the right lines pass, and a decider that cannot answer never lets a line through."""

from __future__ import annotations

import json
import socket
import unittest
from pathlib import Path

from voice.liquid_decider.client import BillingStop, SpendStop
from voice.t3.acts import NOTHING_NEW, Act, Kind, act
from voice.t3.speech import Lane, RenderedLine, Speech
from voice.t3.verify import Decider, Verdict, asks, request

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "verify_lines.json").read_text(encoding="utf-8"))
ESCAPES = {"Alpha is complete?", "Alpha has failed.", "Beta is running. Alpha is unrelated."}
# The one right line d1.2 refused when the fixture was recorded: "running" is not among its act's facts. A refusal
# fails safe, so the act goes to LFM3 and then waits; it is kept here so a change in it shows.
REFUSED_RIGHT_LINES = {"Okay, I'll leave Add a clamp helper running."}
# The one wrong line of the summary paraphrase cases that d1.2 passed: a vague status that carries none of the report
# ("a1_summary" 0.60). Faithful paraphrases of a report all passed (0.91 or more) and the other six wrong lines were
# refused, so the summary question is not too strict, but it lets a vague line through.
PASSED_WRONG_LINES = {"Speed up the loom importer is still working and has made good progress."}


class Recorded:
    """A decider client that answers each request from the fixture, and only one whose questions it recorded."""

    def __init__(self, rows=FIXTURE):
        self.rows = {row["line"]: row for row in rows}

    def call(self, route, body, label=""):
        row = self.rows[body["state"]["line"]]
        asked = {name: question["instructions"] for name, question in body["questions"].items()}
        assert asked == row["asked"], "the questions changed: re-run record_verify"
        return {"status": row["status"], "response": {"answers": row["answers"]}}


class Failing:
    def __init__(self, error):
        self.error = error

    def call(self, route, body, label=""):
        raise self.error


def acts_of(row):
    return [Act.from_json(a) for a in row["acts"]]


class AnsweringAsks:
    """A decider client that answers each question of a line's check as the test says, and no to the rest."""

    def __init__(self, answers):
        self.answers = answers

    def call(self, route, body, label=""):
        return {"status": 200, "response": {"answers": {
            name: {"type": "noul", "noul": self.answers.get(name, 0.0)} for name in body["questions"]}}}


class ConfirmingAnApproval(unittest.TestCase):
    """An approval is confirmed with what this request would do, the way a status line carries its summary."""

    ACT = act(Kind.CONFIRM_RESPOND, thread="Add clamp helper", decision="approve", need="command_approval",
              summary="It wants to run a script that deletes the old build folder.")

    def test_the_line_must_pass_on_the_description_and_a_line_that_omits_it_is_refused(self):
        self.assertIn("a1_summary", [ask.name for ask in asks([self.ACT])])
        line = "Should I approve Add clamp helper?"
        omitted = Decider(AnsweringAsks({"a1_about": 0.9, "a1_means": 0.9, "a1_summary": 0.1})).verify([self.ACT], line)
        self.assertFalse(omitted.passed)
        self.assertEqual(omitted.failed, ("a1_summary",))
        self.assertIn("left out what the request would do", omitted.problems[0])
        said = Decider(AnsweringAsks({"a1_about": 0.9, "a1_means": 0.9, "a1_summary": 0.9})).verify([self.ACT], line)
        self.assertTrue(said.passed)

    def test_a_denial_without_a_description_is_checked_without_one(self):
        deny = act(Kind.CONFIRM_RESPOND, thread="Add clamp helper", decision="deny", need="command_approval")
        self.assertNotIn("a1_summary", [ask.name for ask in asks([deny])])


class OptionsAndAnswers(unittest.TestCase):
    """Each option of a question and each answer is asked about on its own: the slot check finds "Strong" in "how strong
    should it be", so a line that left the option out passed it."""

    ASK = act(Kind.ASK_QUESTION, thread="Add Controller Rumble", question="How strong should the default be?",
              options=["Light", "Medium", "Strong"])
    OK = {"a1_about": 0.9, "a1_means": 0.9}

    def test_a_line_that_leaves_one_option_out_is_refused_and_told_which(self):
        self.assertEqual([a.name for a in asks([self.ASK]) if "_options_" in a.name],
                         ["a1_options_1", "a1_options_2", "a1_options_3"])
        said = {**self.OK, "a1_options_1": 0.9, "a1_options_2": 0.9, "a1_options_3": 0.1}
        verdict = Decider(AnsweringAsks(said)).verify([self.ASK], "How strong: Light or Medium?")
        self.assertEqual((verdict.passed, verdict.failed), (False, ("a1_options_3",)))
        self.assertIn("'Strong'", verdict.problems[0])
        said["a1_options_3"] = 0.9
        self.assertTrue(Decider(AnsweringAsks(said)).verify([self.ASK], "Light, Medium or Strong?").passed)

    def test_every_answer_in_a_confirmation_and_a_report_is_asked_about(self):
        for kind in (Kind.CONFIRM_ANSWER, Kind.ANSWERED):
            with self.subTest(kind=kind.value):
                names = [a.name for a in asks([act(kind, thread="T", answers=["On", "Medium"])])]
                self.assertEqual([n for n in names if "_answers_" in n], ["a1_answers_1", "a1_answers_2"])

    def test_a_question_with_no_options_asks_about_none(self):
        free = act(Kind.ASK_QUESTION, thread="T", question="Name?", options=[], own_words=True)
        self.assertEqual([a.name for a in asks([free]) if "_options_" in a.name], [])
        means = next(a.question for a in asks([free]) if a.name == "a1_means")
        self.assertNotIn("options being", means, "it is not asked which option they choose")


class LineCheck(unittest.TestCase):
    def test_an_unanswered_line_still_checks_each_unconfirmed_action(self):
        line = "I couldn't give an answer, but I sent the follow-up."
        decider = Decider(AnsweringAsks({"a1_means": 0.9, "claims_sent": 0.9}))
        self.assertEqual(decider.verify([act(Kind.UNANSWERED)], line).failed, ("claims_sent",))
        self.assertTrue(decider.verify([act(Kind.UNANSWERED, outcomes=["sent"])], "I sent the follow-up.").passed)

    def test_an_unanswered_line_never_says_a_permission_request_was_approved_or_denied(self):
        decider = Decider(AnsweringAsks({"a1_means": 0.9, "claims_answered": 0.9}))
        for line in ("I approved the request.", "I said no to the request."):
            self.assertEqual(decider.verify([act(Kind.UNANSWERED)], line).failed, ("claims_answered",), line)
        for outcome in ("approved", "denied"):
            self.assertTrue(decider.verify([act(Kind.UNANSWERED, outcomes=[outcome])], "Done.").passed, outcome)

    def test_the_decider_refuses_the_reviewers_escapes(self):
        decider = Decider(Recorded())
        escapes = [row for row in FIXTURE if row["line"] in ESCAPES]
        self.assertEqual({row["line"] for row in escapes}, ESCAPES)
        for row in escapes:
            with self.subTest(row["line"]):
                verdict = decider.verify(acts_of(row), row["line"])
                self.assertFalse(verdict.passed, verdict)
                self.assertTrue(verdict.failed, verdict)

    def test_the_decider_refuses_status_escapes_and_accepts_correct_news(self):
        rows = [row for row in FIXTURE if not row["case"].startswith("unsolicited question ")]
        for row in rows:
            with self.subTest(row["line"], take=row.get("take")):
                verdict = Decider(Recorded([row])).verify(acts_of(row), row["line"])
                expected = ((row["should_pass"] and row["line"] not in REFUSED_RIGHT_LINES)
                            or row["line"] in PASSED_WRONG_LINES)
                self.assertIsNone(verdict.error, verdict)
                self.assertEqual(verdict.passed, expected, verdict)
        self.assertEqual(sum(not row["should_pass"] for row in rows), 34,
                         "3 escapes, 17 old word-list claims, 3 beside smoke-run lines, 3 takes of a false done, "
                         "a status said again without its summary and 7 wrong lines beside summary paraphrases")

    def test_a_reply_is_never_asked_whether_its_run_ended(self):
        reply = act(Kind.REPLIED, thread="Alpha", status="running", reply="I added clamp. The tests pass.")
        self.assertEqual([a.name for a in asks([reply])],
                         ["a1_about", "a1_means", "a1_asks_unrequested", "claims_stopped", "invents"])
        for status_act in (act(Kind.THREAD_STATUS, thread="Alpha", status="working"),
                           act(Kind.RUN_UPDATE, thread="Alpha", status="running")):
            with self.subTest(status_act.kind.value):
                self.assertIn("claims_finished", [a.name for a in asks([status_act])])
        held = act(Kind.HELD_FOLLOWUP, thread="Alpha")
        self.assertIn("claims_finished", [a.name for a in asks([reply, held])], "another act's own claim stays")

    def test_finished_means_the_run_ended_while_it_runs_and_success_once_it_ended(self):
        def finished(said):
            return next(a.question for a in asks([said]) if a.name == "claims_finished")

        self.assertIn("the run of Alpha has already ended", finished(act(Kind.THREAD_STATUS, thread="Alpha",
                                                                         status="working")))
        self.assertIn("Alpha completed its work successfully", finished(act(Kind.DONE, thread="Alpha",
                                                                            status="interrupted")))

    def test_unsolicited_questions_are_not_spoken(self):
        rows = [row for row in FIXTURE if row["case"].startswith("unsolicited question ")]
        self.assertEqual(sum(row["should_pass"] for row in rows), 5)
        self.assertEqual(sum(not row["should_pass"] for row in rows), 5)
        for row in rows:
            with self.subTest(row["line"]):
                acts, line = acts_of(row), row["line"]
                decider = Decider(Recorded([row]))
                verdict = decider.verify(acts, line)
                self.assertIsNone(verdict.error, verdict)
                # The recorded "What's next?" answer fell just below 0.5. The form check still refuses it.
                decider_passes = row["should_pass"] or line.endswith(" What's next?")
                self.assertEqual(verdict.failed, () if decider_passes else ("a1_asks_unrequested",))
                rendered = Speech(None, lambda thread_id: None, decider).accept(acts, line, "text_model")
                self.assertEqual(rendered.line is not None, row["should_pass"], rendered)

    def test_the_questions_come_from_the_act(self):
        stop = asks([act(Kind.CONFIRM_STOP, thread="Alpha")])
        self.assertEqual([(a.name, a.want) for a in stop],
                         [("a1_about", True), ("a1_means", True), ("claims_stopped", False), ("invents", False)])
        self.assertIn("whether to stop Alpha", stop[1].question)
        body = request([act(Kind.THREAD_STATUS, thread="Alpha", status="working")], "Alpha is busy.")
        self.assertEqual(body["state"]["facts"], [{"act": "thread_status", "thread": "Alpha", "status": "working"}])
        self.assertTrue(all(q["type"] == "noul" for q in body["questions"].values()))
        again = asks([act(Kind.THREAD_STATUS, thread="Alpha", status="working", summary="It says the tests pass.",
                          since_last_asked=NOTHING_NEW)])
        self.assertIn(("a1_summary", True), [(a.name, a.want) for a in again],
                      "a status said again still passes on what the thread reported")

    def test_an_unnamed_cancellation_cannot_name_a_thread(self):
        acts = (act(Kind.CANCELLED, op="start"),)

        class NameCheck:
            def call(self, route, body, label=""):
                answers = {"a1_means": 1, "a1_asks_unrequested": 0, "claims_started": 0, "invents": 0, "a1_names_target": 1}
                return {"status": 200, "response": {"answers": {
                    name: {"type": "noul", "noul": value} for name, value in answers.items()
                }}}

        speech = Speech(None, lambda _: None, Decider(NameCheck()))
        line = "I won't start the Plan Icon Audit."
        self.assertIsNone(speech.accept(acts, line, "call").line)

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
