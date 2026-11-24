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
from voice.t3.verify import Decider, Verdict, act_meaning, asks, request

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


class DescribingARequest(unittest.TestCase):
    """What a line says a pending request is about is the gated description or nothing."""

    DESCRIBED = act(Kind.NEEDS_YOU, thread="Add clamp helper", need="command_approval",
                    request="It wants to write a short note into a file.")
    KIND_ONLY = act(Kind.NEEDS_YOU, thread="Add clamp helper", need="command_approval")
    PASSING = {"a1_about": 0.9, "a1_means": 0.9}

    def verdict(self, said, **answers):
        return Decider(AnsweringAsks({**self.PASSING, **answers})).verify([said], "Add clamp helper needs approval.")

    def test_a_described_line_must_carry_the_description_and_add_nothing(self):
        names = [ask.name for ask in asks([self.DESCRIBED])]
        self.assertIn("a1_request", names)
        self.assertIn("a1_request_adds", names)
        self.assertIn("a1_reads_out", names)
        self.assertNotIn("a1_kind_only", names)
        self.assertTrue(self.verdict(self.DESCRIBED, a1_request=0.9).passed)
        left_out = self.verdict(self.DESCRIBED, a1_request=0.1)
        self.assertEqual(left_out.failed, ("a1_request",))
        added = self.verdict(self.DESCRIBED, a1_request=0.9, a1_request_adds=0.9)
        self.assertEqual(added.failed, ("a1_request_adds",))
        read_out = self.verdict(self.DESCRIBED, a1_request=0.9, a1_reads_out=0.9)
        self.assertEqual(read_out.failed, ("a1_reads_out",))

    def test_a_line_with_no_description_says_only_the_kind_of_request(self):
        self.assertEqual([ask.name for ask in asks([self.KIND_ONLY]) if ask.name.startswith("a1_k")], ["a1_kind_only"])
        self.assertTrue(self.verdict(self.KIND_ONLY, a1_need_kind=0.95).passed)
        self.assertEqual(self.verdict(self.KIND_ONLY, a1_need_kind=0.8).failed, ("a1_need_kind",),
                         "a wrong kind of request is refused")
        invented = self.verdict(self.KIND_ONLY, a1_kind_only=0.9, a1_need_kind=0.95)
        self.assertEqual(invented.failed, ("a1_kind_only",))
        self.assertIn("kind of thing", invented.problems[0])

    def test_an_approval_asked_with_no_description_is_checked_for_its_kind_and_names_the_tool(self):
        base = dict(thread="Add clamp helper", decision="approve", need="permission")
        by_kind = act(Kind.CONFIRM_RESPOND, **base, tool="list_pages")
        names = lambda said: [ask.name for ask in asks([said]) if ask.name.startswith("a1_k") or ask.name == "a1_need_kind"]
        self.assertEqual(names(by_kind), ["a1_kind_only", "a1_need_kind"])
        self.assertIn('the tool "list_pages"', next(ask.question for ask in asks([by_kind]) if ask.name == "a1_kind_only"))
        self.assertEqual(names(act(Kind.CONFIRM_RESPOND, **base, summary="It lists pages.")), [], "a description is checked")
        self.assertEqual(names(act(Kind.CONFIRM_RESPOND, **{**base, "decision": "deny"})), [], "a denial says no kind")

    def test_a_thread_with_no_title_is_asked_about_as_the_one_started_for_them(self):
        said = act(Kind.NEEDS_YOU, begun_in="Voice sandbox", need="command_approval")
        self.assertIn("started for them in Voice sandbox", act_meaning(said))
        self.assertNotIn("a1_about", [ask.name for ask in asks([said])])

    def test_no_other_thread_is_what_the_act_means(self):
        self.assertIn("no other thread", act_meaning(act(Kind.NO_THREAD, other=True)))
        self.assertIn("9 recent threads", act_meaning(act(Kind.NO_THREAD, recent=9)))


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
        self.assertEqual(sum(not row["should_pass"] for row in rows), 43,
                         "3 escapes, 17 old word-list claims, 3 beside smoke-run lines, 3 takes of a false done, "
                         "a status said again without its summary, 7 wrong lines beside summary paraphrases and 9 "
                         "wrong lines for a dropped pick")

    def test_a_dropped_pick_is_refused_on_its_own_terms_not_only_by_the_catch_all(self):
        """The line that drops a taken-back pick: a line naming or offering a thread, dropping another action or
        saying the thread stopped fails a question about that, not only `invents`."""
        rows = [row for row in FIXTURE if row["case"].startswith("dropped pick")]
        for row in rows:
            with self.subTest(row["line"]):
                verdict = Decider(Recorded([row])).verify(acts_of(row), row["line"])
                self.assertEqual(verdict.passed, row["should_pass"], verdict)
                if not row["should_pass"]:
                    self.assertTrue(set(verdict.failed) - {"invents"}, verdict)
        self.assertEqual(sum(not row["should_pass"] for row in rows), 9)

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


class KindOnly(unittest.TestCase):
    """On recorded d1.2 answers (voice/t3/eval/record_kind_only.py): with the thread's own name or account quoted in the
    question, a line that says only the kind of request passes, and one that says what the request does is refused."""

    FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "kind_only.json").read_text(encoding="utf-8"))

    def test_the_question_sent_is_the_recorded_one_and_reads_each_line_as_its_case_says(self):
        from voice.t3.acts import Act
        from voice.t3.eval.record_kind_only import bodies
        passed = {True: [], False: []}
        for row in self.FIXTURE["rows"]:
            said = Act.from_json(row["act"])
            self.assertEqual(bodies(said, row["line"])["shipped"]["questions"]["a1_kind_only"]["instructions"],
                             next(a.question for a in asks([said]) if a.name == "a1_kind_only"))
            passed[row["kind_only"]] += [p < 0.5 for p in row["shipped"]]
        self.assertEqual(sum(passed[False]), 0, "a line that says what the request does is never passed")
        self.assertEqual((sum(passed[True]), len(passed[True])), (14, 15),
                         "one sample of a title that reads as the work itself sits over the bar")

    def test_a_line_that_names_the_wrong_kind_of_request_is_refused(self):
        from voice.t3.verify import NEED_KIND_YES
        for row in self.FIXTURE["kinds"]:
            said = Act.from_json(row["act"])
            self.assertEqual(request((said,), row["line"])["questions"]["a1_need_kind"]["instructions"],
                             next(a.question for a in asks([said]) if a.name == "a1_need_kind"))
            with self.subTest(row["line"]):
                self.assertEqual([p >= NEED_KIND_YES for p in row["samples"]], [row["right"]] * len(row["samples"]))
