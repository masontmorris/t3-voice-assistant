"""The scenarios' scoring and gate rules, and the caller that holds a call against a stand-in server."""

import asyncio
import dataclasses
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np
import soundfile as sf
import websockets

from voice.live import rehearse as R
from voice.live import scenarios as S
from voice.live.scenario_score import Call, Judge, Result, Turn, Verdict, turn_of
from voice.t3.acts import Kind


def turn(index, kinds=(), delivery="sent", free=(), idle=False, tools=(), cancelled=None, barge=None, replies=None):
    entries = tuple({"kinds": [k], "delivery": delivery, "draft": "", "draft_problems": [], "reply": "", "status": None,
                     "relays": 1} for k in kinds)
    said = replies if replies is not None else [("snapshot", None, line) for line in free]
    audio = 1.0 if said or (entries and delivery in ("sent", "partial")) else None
    return Turn(index, index + 1, idle, None, entries, tuple(said), tuple(tools), cancelled, barge, audio, 1)


class Yes(Judge):
    """A judge that answers every question the same way."""

    def __init__(self, answer):
        super().__init__(client=object())
        self.answer = answer

    def p_yes(self, question, text, context):
        self.asked.append({"question": question, "p_yes": self.answer})
        return self.answer


def call(turns, world=None, judge=None):
    return Call(tuple(turns), world or {"seeded": {}, "made": {}}, judge or Yes(0.0))


def failed(verdicts):
    return [v.check for v in verdicts if not v.ok]


class Withdrawn(unittest.TestCase):
    def plan(self):
        return S.withdrawn(S.Pool())

    def held(self, *later):
        return call([turn(0, [Kind.ASK_WHICH]), *later])

    def test_a_call_that_moves_on_passes(self):
        plan = self.plan()
        turns = [turn(1, cancelled="continued"), *(turn(i, free=("Sure.",)) for i in range(2, 6))]
        self.assertEqual(failed(plan.check(self.held(*turns))), [])

    def test_the_old_choices_offered_again_fail_on_the_thread_line_and_on_the_judged_reply(self):
        plan = self.plan()
        repeat = [turn(1, [Kind.ASK_WHICH]), turn(2, free=("Which one?",))]
        found = failed(plan.check(call([turn(0, [Kind.ASK_WHICH]), *repeat], judge=Yes(0.9))))
        self.assertEqual(found, ["no which-thread question after the withdrawal", "no free reply offers the old choices"])

    def test_a_call_whose_first_turn_asked_nothing_fails_its_precondition(self):
        plan = self.plan()
        found = plan.check(call([turn(0, [Kind.THREAD_STATUS])]))
        self.assertEqual(failed(found)[0], "asked a thread question")
        self.assertTrue(all(v.precondition for v in found[1:]))

    def test_a_fallback_does_not_answer_the_turn_after_withdrawal(self):
        held = self.held(turn(1, [Kind.UNANSWERED]))
        self.assertEqual(failed(self.plan().check(held)), ["every completed turn was answered"])
        self.assertTrue(S.no_silent_turn(held).ok)


class Approval(unittest.TestCase):
    def world(self, waiting):
        return {"seeded": {"x": {"waiting_on": waiting}}, "made": {}}

    def test_a_claim_while_pending_fails_and_a_claim_after_it_was_resolved_does_not(self):
        turns = [turn(0, free=("It's approved.",))]
        pending = S.claims_approval(call(turns, self.world("approval"), Yes(0.9)), "x", tuple(turns))
        resolved = S.claims_approval(call(turns, self.world(None), Yes(0.9)), "x", tuple(turns))
        self.assertEqual((failed(pending), failed(resolved)), (["no approval claimed while T3 still holds it"], []))

    def test_a_response_said_done_while_T3_still_holds_the_request_fails(self):
        turns = [turn(0, [Kind.RESPONDED])]
        found = S.claims_approval(call(turns, self.world("approval")), "x", tuple(turns))
        self.assertEqual(failed(found), ["a response said done is done in T3"])
        self.assertEqual(failed(S.claims_approval(call(turns, self.world(None)), "x", tuple(turns))), [])

    def test_no_claim_passes_while_pending(self):
        turns = [turn(0, free=("I can't approve that.",))]
        self.assertEqual(failed(S.claims_approval(call(turns, self.world("approval"), Yes(0.1)), "x", tuple(turns))), [])


class Judged(Judge):
    """A judge that answers each stated question as the test says, and raises on one it was not told about."""

    def __init__(self, **answers):
        super().__init__(client=object())
        self.answers = answers

    def p_yes(self, question, text, context):
        self.asked.append({"question": question, "text": text, "context": context})
        return {S.DESCRIBED: self.answers["described"], S.REFUSES: self.answers["refuses"],
                S.READS_OUT: self.answers["reads_out"]}[question]


class RespondByVoice(unittest.TestCase):
    REQUEST = "printf 'x' > .probe.txt"

    def held(self, build, *, line="It wants to write a short line into a hidden file.", asked=True, app=False,
             responded=True, decision=None, status="resolved", pending=None, record=True, request_known=True,
             judge=None):
        plan = build(S.Pool())
        title = plan.seeds[0].title
        decision = decision or ("accept" if build is S.approve_waiting else "decline")
        turns = [turn(0, [Kind.CONFIRM_RESPOND] if asked else [Kind.NEEDS_THE_APP] if app else []),
                 turn(1, [Kind.RESPONDED] if responded else [])]
        turns[0] = dataclasses.replace(turns[0], t3=tuple({**e, "draft": line} for e in turns[0].t3))
        plan.notes.update(asked=asked, line=line if asked else None, start={"seeded": {title: {
            "need": self.REQUEST if request_known else None, "request_id": "q1"}}})
        now = {title: {"waiting_on": pending, "request_id": None,
                       "requests": [{"id": "q1", "status": status, "decision": decision}]}} if record else {}
        judge = judge or Judged(described=0.9, refuses=0.9, reads_out=0.1)
        return plan.check(Call(tuple(turns), {"seeded": now, "made": {}}, judge, plan.notes))

    def test_an_approval_passes_only_with_a_described_confirmation_a_yes_and_the_decision_recorded_for_that_request(self):
        self.assertEqual(failed(self.held(S.approve_waiting)), [])
        judge = Judged(described=0.9, refuses=0.9, reads_out=0.1)
        self.held(S.approve_waiting, judge=judge)
        self.assertEqual([a["question"] for a in judge.asked], [S.DESCRIBED, S.READS_OUT])
        self.assertIn(self.REQUEST, judge.asked[0]["context"], "the line is judged against the request itself")

    def test_each_of_the_things_that_make_an_approval_wrong_fails_its_own_check(self):
        wrong = {"a description the judge rejects": (dict(judge=Judged(described=0.1, refuses=0.9, reads_out=0.1)),
                                                     "the spoken description says what the request does"),
                 "a line that reads the request out": (dict(judge=Judged(described=0.9, refuses=0.9, reads_out=0.9)),
                                                       "the confirmation reads out no command, path or code"),
                 "a decision other than the one asked for": (dict(decision="decline"),
                                                             "T3 resolved that request as accept"),
                 "a request still pending": (dict(status="pending", decision=None), "T3 resolved that request as accept"),
                 "no answer said": (dict(responded=False), "the yes was answered by saying the answer was sent"),
                 "the thread still waiting": (dict(pending="approval"), "T3 no longer holds a pending request")}
        for name, (options, check) in wrong.items():
            with self.subTest(name):
                self.assertEqual(failed(self.held(S.approve_waiting, **options)), [check])

    def test_a_missing_record_fails_and_never_passes(self):
        self.assertEqual(failed(self.held(S.approve_waiting, record=False)),
                         ["the sandbox holds the thread and the request it waited on"])
        self.assertEqual(failed(self.held(S.deny_waiting, request_known=False)),
                         ["the sandbox holds the thread and the request it waited on"])
        self.assertEqual(failed(self.held(S.approve_waiting, status="expired", decision=None)),
                         ["T3 resolved that request as accept"])

    def test_an_approval_the_gate_sends_to_the_app_is_the_one_known_failure_and_nothing_downstream_is_scored(self):
        found = self.held(S.approve_waiting, asked=False, app=True)
        self.assertEqual(failed(found)[0], "a description of the request passed the gate")
        self.assertTrue(all(v.precondition and v.blocked_by == "a description of the request passed the gate"
                            for v in found if not v.ok and v.precondition), "what is behind it names the marked check")
        scenario = S.Scenario("approve-waiting", "", S.approve_waiting)
        table, green = S.rows({"approve-waiting": [Result("approve-waiting", 1, tuple(found), (), 0, 0, 0)]}, [scenario])
        self.assertEqual((table[0][1], green), ("XFAIL", True))
        silent = self.held(S.approve_waiting, asked=False, app=False)
        self.assertEqual(failed(silent)[:2], ["the answer was confirmed or sent to the app",
                                              "a description of the request passed the gate"])
        self.assertEqual(S.rows({"approve-waiting": [Result("approve-waiting", 1, tuple(silent), (), 0, 0, 0)]},
                                [scenario])[0][0][1], "FAIL")

    def test_a_denial_is_judged_as_a_refusal_that_says_nothing_of_the_request_and_resolves_it_declined(self):
        self.assertEqual(failed(self.held(S.deny_waiting)), [])
        self.assertEqual(failed(self.held(S.deny_waiting, judge=Judged(described=0.9, refuses=0.1, reads_out=0.1))),
                         ["the denial asks whether to say no and says nothing of what the request does"])
        self.assertEqual(failed(self.held(S.deny_waiting, decision="accept")), ["T3 resolved that request as decline"])
        self.assertEqual(failed(self.held(S.deny_waiting, asked=False, app=True)),
                         ["the denial was confirmed back"])


class Lines(unittest.TestCase):
    def test_every_thread_turn_needs_a_said_line_and_a_refused_one_fails(self):
        plan = S.thread_lines(S.Pool())
        good = [turn(0, [Kind.THREAD_STATUS]), turn(1, [Kind.THREAD_STATUS]), turn(2, [Kind.THREAD_STATUS]), turn(3)]
        self.assertEqual(failed(plan.check(call(good))), [])
        refused = [turn(0, [Kind.THREAD_STATUS], delivery="held"), *good[1:]]
        self.assertEqual(len(failed(plan.check(call(refused)))), 1)
        free_beside = [turn(0, [Kind.THREAD_STATUS], free=("It is going well.",)), *good[1:]]
        self.assertEqual(failed(plan.check(call(free_beside))), ["no free line beside a status line"])


class FollowUp(unittest.TestCase):
    def lookup(self, place, day):
        return {"tool": "weather", "outcome": "ok", "arguments": {"place": place, "when": "x"},
                "detail": {"days": [{"date": day}]}}

    def test_the_follow_up_must_keep_the_place_and_land_six_days_after_tomorrow(self):
        plan = S.follow_up(S.Pool())
        city = S.CITIES[0]
        first = turn(0, tools=[self.lookup(city, "2026-11-21")])
        self.assertEqual(failed(plan.check(call([first, turn(1, tools=[self.lookup(city, "2026-11-27")])]))), [])
        self.assertEqual(len(failed(plan.check(call([first, turn(1, tools=[self.lookup("Paris", "2026-11-27")])])))), 3)
        self.assertEqual(len(failed(plan.check(call([first, turn(1, tools=[self.lookup(city, "2026-11-20")])])))), 1)
        self.assertEqual(len(failed(plan.check(call([first, turn(1)])))), 3)


class Silence(unittest.TestCase):
    def test_a_thread_turn_that_says_nothing_fails_unless_something_else_was_heard(self):
        self.assertTrue(S.no_silent_turn(call([turn(0, [Kind.CONFIRM_TELL], delivery="unsent")])).ok is False)
        self.assertTrue(S.no_silent_turn(call([turn(0, [Kind.CONFIRM_TELL], delivery="unsent", free=("Hm.",))])).ok)
        self.assertTrue(S.no_silent_turn(call([turn(0, [Kind.THREAD_STATUS])])).ok)
        self.assertTrue(S.no_silent_turn(call([turn(0, [Kind.CANCELLED], delivery="unsent", cancelled="continued")])).ok)

    def test_a_text_only_ordinary_reply_was_not_heard(self):
        record = {"clip": "turn_001", "final_transcript": "Hello.",
                  "replies": [{"kind": "snapshot", "text": "Hi.", "speech_s": 0.0}]}
        silent = turn_of(0, record, lambda _: [])
        self.assertFalse(silent.answered)
        self.assertFalse(S.no_silent_turn(call([silent])).ok)

        absorbed = turn_of(0, {"clip": "turn_001", "final_transcript": "And keep it tidy.", "replies": []},
                           lambda _: [])
        self.assertFalse(S.no_silent_turn(call([absorbed])).ok)

        record["live"] = {"speech_end_to_first_audio_sent_s": 1.0}
        record["replies"].append({"kind": "t3", "text": "I couldn't answer.", "delivery": "sent", "speech_s": 1.0})
        heard = turn_of(0, record, lambda _: [{"kinds": [Kind.UNANSWERED], "delivery": "sent"}])
        self.assertFalse(heard.answered)
        self.assertTrue(S.no_silent_turn(call([heard])).ok)


class ShortYes(unittest.TestCase):
    def test_both_yeses_must_start_a_thread_and_say_so(self):
        plan = S.short_yes(S.Pool())
        plan.notes.update(asked=[True] * 2, answered=[True] * 2)
        turns = [turn(i, [Kind.LAUNCHED]) for i in range(2)]
        made = {f"t{i}": {} for i in range(2)}
        self.assertEqual(failed(plan.check(call(turns, {"seeded": {}, "made": made}))), [])
        self.assertEqual(len(failed(plan.check(call(turns, {"seeded": {}, "made": {"t0": {}}})))), 2)


class Speaking(unittest.TestCase):
    def test_the_first_reply_is_stopped_and_the_second_answered(self):
        plan = S.speak_over(S.Pool())
        answered = turn(1, barge={"heard": True}, replies=[("snapshot", "sent", "Four.")])
        self.assertEqual(failed(plan.check(call([turn(0), answered], judge=Yes(0.9)))), [])
        without_barge = turn(1, replies=[("snapshot", "sent", "Four.")])
        self.assertEqual(failed(plan.check(call([turn(0), without_barge], judge=Yes(0.9)))),
                         ["the first reply was stopped"])

    def test_an_acknowledgment_breaks_silence_but_does_not_answer(self):
        plan = S.speak_over(S.Pool())
        last = turn(1, [Kind.UNANSWERED], barge={"heard": True},
                    replies=[("t3", "sent", "I couldn't answer.")])
        held = call([turn(0), last])
        self.assertEqual(failed(plan.check(held)), ["the new words were answered"])
        self.assertTrue(S.no_silent_turn(held).ok)
        self.assertFalse(S.no_unexpected_fallback(held).ok)


class LongSet(unittest.TestCase):
    def test_a_withdrawn_launch_starts_nothing_and_is_not_asked_again(self):
        plan = S.withdrawn_launch(S.Pool())
        good = [turn(0, [Kind.CONFIRM_LAUNCH]), turn(1, free=("Okay.",)), turn(2, free=("Sure.",)), turn(3, free=("Hi.",))]
        self.assertEqual(failed(plan.check(call(good))), [])
        started = call([*good[:1], turn(1, [Kind.LAUNCHED])], {"seeded": {}, "made": {"t": {}}})
        self.assertEqual(failed(plan.check(started)), ["nothing was started", "no launch line after the withdrawal"])
        asked = call([good[0], turn(1, [Kind.CONFIRM_LAUNCH])])
        self.assertEqual(failed(plan.check(asked)), ["the question was not asked again"])

    def test_a_message_must_be_taken_and_reach_the_thread(self):
        plan = S.short_yes_tell(S.Pool())
        title = S.SINGLES[0][1]
        plan.notes["asked"] = True
        turns = [turn(0, [Kind.CONFIRM_TELL]), turn(1, [Kind.SENT_FOLLOWUP])]
        self.assertEqual(failed(plan.check(call(turns, {"seeded": {title: {"new_run": True}}, "made": {}}))), [])
        self.assertEqual(failed(plan.check(call(turns, {"seeded": {title: {"new_run": False}}, "made": {}}))),
                         ["the thread got the message"])

    def test_news_of_the_end_is_said_once(self):
        plan = S.news_done(S.Pool())
        plan.notes.update(confirmed=True, timed_out=False)
        world = {"seeded": {}, "made": {"t": {"activity": "finished"}}}
        said = [turn(0), turn(1, [Kind.DONE], idle=True)]
        self.assertEqual(failed(plan.check(call(said, world))), [])
        self.assertEqual(failed(plan.check(call([turn(0)], world))), ["the assistant said it finished, unprompted"])
        twice = [*said, turn(2, [Kind.DONE], idle=True)]
        self.assertEqual(failed(plan.check(call(twice, world))), ["no duplicate done notices"])


class FalseStart(unittest.TestCase):
    def test_a_free_claim_of_a_start_fails_only_when_no_thread_was_made(self):
        turns = [turn(0, free=("I will start a new coding thread.",))]
        none = {"seeded": {}, "made": {}}
        self.assertFalse(S.no_false_start(call(turns, none, Yes(0.9))).ok)
        self.assertTrue(S.no_false_start(call(turns, none, Yes(0.1))).ok)
        self.assertTrue(S.no_false_start(call(turns, {"seeded": {}, "made": {"t": {}}}, Yes(0.9))).ok)


class Pooling(unittest.TestCase):
    def test_a_name_is_handed_out_once_and_a_spent_pool_stops_the_run(self):
        pool = S.Pool()
        names = [pool.single()[0] for _ in range(len(S.SINGLES))]
        self.assertEqual(len(set(names)), len(S.SINGLES))
        with self.assertRaises(SystemExit):
            pool.single()

    def test_no_name_is_a_word_run_of_another_title(self):
        spoken = [s for s, _ in S.SINGLES]
        self.assertEqual(len(set(spoken)), len(spoken))

    def test_every_plan_builds_and_its_lines_are_distinct_text(self):
        pool = S.Pool()
        for sc in S.SCENARIOS:
            plan = sc.build(pool)
            self.assertEqual(len(set(plan.lines.values())), len(plan.lines), sc.id)


def result(scenario, ok, error=None):
    verdicts = () if error else (Verdict("c", ok, "" if ok else "why", None if ok else 2),)
    return Result(scenario, 1, verdicts, (1.0,), 5, 2, 10.0, error)


class Gate(unittest.TestCase):
    def sc(self, passes=1, runs=1):
        return S.Scenario("x", "w", lambda pool: None, runs, passes)

    def table(self, results, passes=1, known=()):
        with patch.object(S, "KNOWN", {("x", check): "a fault" for check in known}):
            return S.rows({"x": results}, [self.sc(passes, len(results))])

    def test_green_for_a_pass_and_red_for_a_fail(self):
        self.assertEqual((self.table([result("x", True)])[0][0][1], self.table([result("x", True)])[1]), ("PASS", True))
        table, green = self.table([result("x", False)])
        self.assertEqual((table[0][1], green), ("FAIL", False))
        self.assertIn("turn 2", table[0][6])

    def test_a_known_fault_is_green_while_it_fails_and_says_when_it_passes(self):
        self.assertEqual(self.table([result("x", False)], known=["c"])[0][0][1], "XFAIL")
        self.assertTrue(self.table([result("x", False)], known=["c"])[1])
        self.assertEqual(self.table([result("x", True)], known=["c"])[0][0][1], "XPASS")

    def test_a_known_fault_does_not_hide_another_check_failing(self):
        run = Result("x", 1, (Verdict("c", False, "a"), Verdict("d", False, "b")), (), 0, 0, 1.0)
        table, green = self.table([run], known=["c"])
        self.assertEqual((table[0][1], green), ("FAIL", False))

    def test_news_blocked_by_the_marked_yes_is_expected_but_missing_exchange_is_an_error(self):
        plan = S.news_permission(S.Pool())
        plan.notes.update(confirmed=True, timed_out=True)
        turns = [turn(0, [Kind.CONFIRM_LAUNCH]), turn(1), turn(2), turn(3)]

        def unexpected(items, world=None):
            verdicts = plan.check(call(items, world))
            return S.unexpected(Result("news-permission", 1, tuple(verdicts), (), 0, 0, 0))

        self.assertEqual(unexpected(turns), [])
        self.assertIn("no approval claimed while T3 still holds it", failed(unexpected(turns[:2])))
        self.assertIn("no approval claimed while T3 still holds it", failed(unexpected(
            turns[:2], {"seeded": {}, "made": {"t": {"title": "Example", "waiting_on": "approval"}}})))

    def test_permission_news_must_be_delivered_once_the_wait_happened(self):
        plan = S.news_permission(S.Pool())
        plan.notes.update(confirmed=True, timed_out=False)
        world = {"seeded": {}, "made": {"t": {"title": "Example", "waiting_on": "approval"}}}
        lost = "permission news was not lost to a refused line"
        silent = [turn(0, [Kind.CONFIRM_LAUNCH]), turn(1, [Kind.LAUNCHED]), turn(2), turn(3), turn(4)]
        self.assertIn(lost, failed(plan.check(call(silent, world))), "no news and no refusal is no news")
        refused = [*silent[:2], dataclasses.replace(turn(2, [Kind.NEEDS_YOU], delivery="unsent"), idle=True), *silent[3:]]
        self.assertIn(lost, failed(plan.check(call(refused, world))))
        said = [*silent[:2], turn(2, [Kind.NEEDS_YOU], idle=True), *silent[3:]]
        self.assertNotIn(lost, failed(plan.check(call(said, world))))
        plan.notes.pop("timed_out")
        self.assertNotIn(lost, [v.check for v in plan.check(call(silent, world)) if not v.ok and not v.precondition],
                         "with no wait the check is a failed precondition, not a failed check")

    def test_k_of_n_and_an_error_that_no_run_passed(self):
        two = [result("x", True), result("x", False), result("x", True)]
        self.assertEqual(self.table(two, passes=2)[0][0][1], "PASS")
        self.assertEqual(self.table(two[:2], passes=2)[0][0][1], "FAIL")
        broken = self.table([result("x", False, error="RuntimeError: the server died")])
        self.assertEqual((broken[0][0][1], broken[1]), ("ERROR", False))

    def test_an_error_followed_by_a_clean_run_still_fails_the_scenario(self):
        runs = [result("x", False, "RuntimeError: the server died"), result("x", True)]
        table, green = self.table(runs, known=["c"])
        self.assertEqual((table[0][1], green), ("ERROR", False))
        self.assertIn("the server died", table[0][6])

    def test_withdrawn_question_has_no_retry_to_hide_an_unmarked_failure(self):
        scenario = next(sc for sc in S.SCENARIOS if sc.id == "withdrawn-question")
        self.assertEqual((scenario.passes, scenario.runs), (1, 1))

    def test_skipping_a_confirmation_leaves_a_failed_unmarked_precondition(self):
        for build in (S.news_permission, S.short_yes, S.news_done, S.short_yes_tell):
            with self.subTest(scenario=build.__name__):
                scenario = next(sc for sc in S.SCENARIOS if sc.build == build)
                plan = build(S.Pool())
                next(plan.script)
                with self.assertRaises(StopIteration):
                    while True:
                        plan.script.send(R.Heard((turn(0),), None, False))
                held = call([turn(0), turn(1)] if build == S.short_yes else [turn(0)])
                verdicts = plan.check(held)
                self.assertTrue(any(not v.ok and (scenario.id, v.check) not in S.KNOWN for v in verdicts))
                self.assertTrue(any(v.precondition and not v.ok for v in verdicts))
                run = Result(scenario.id, 1, tuple(verdicts), (), 0, 0, 0)
                table, green = S.rows({scenario.id: [run]}, [scenario])
                self.assertEqual((table[0][1], green), ("FAIL", False))

    def run_call(self, out, play, judge=None, build=S.approval, sid="approval-claim"):
        world = Mock()
        world.titles.return_value = {}
        world.snapshot.return_value = {"seeded": {"x": {"waiting_on": "approval"}}, "made": {}}
        rig = Mock(out=out)
        rig.render.return_value = [None] * 5
        with patch.object(R, "hold_call", side_effect=play), patch.object(S.time, "sleep"):
            return S.run_once(S.Scenario(sid, "", build), 1, world, rig, judge or Yes(0.0), S.Pool(), out, 0)

    def test_a_missing_or_unreadable_completed_turn_is_an_error(self):
        for record in (None, "broken JSON", "{}", '{"clip":"turn_002","error":"model server died"}'):
            with self.subTest(record=record), tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp)
                server = out / "server"
                server.mkdir()
                (server / "turn_001.json").write_text('{"clip":"turn_001"}', encoding="utf-8")
                if record is not None:
                    (server / "turn_002.json").write_text(record, encoding="utf-8")

                def play(_rig, _script, _clips, load, *_):
                    load(1)
                    load(2)

                run = self.run_call(out, play)
                self.assertIsNotNone(run.error)
                self.assertIn("turn_002.json", run.error)

    def test_unanswered_judge_questions_are_errors_even_for_marked_checks(self):
        cases = [(S.approval, "approval-claim", "It's approved."),
                 (S.withdrawn, "withdrawn-question", "Which one?"),
                 (S.withdrawn_launch, "withdrawn-launch", "I'm starting it now.")]
        for build, sid, reply in cases:
            for response in (TimeoutError("hosted judge timed out"), {"status": 200, "response": {}}):
                with self.subTest(scenario=sid, response=response), tempfile.TemporaryDirectory() as tmp:
                    out = Path(tmp)
                    (out / "server").mkdir()

                    def play(_rig, _script, _clips, load, *_):
                        for n in range(1, 6 if build == S.withdrawn else 5):
                            name = f"turn_{n:03d}"
                            record = {"clip": name, "replies": [{"text": reply}] if n == 2 else [], "timeline": []}
                            if n == 1:
                                kind = Kind.ASK_WHICH if build == S.withdrawn else Kind.CONFIRM_LAUNCH
                                record["timeline"] = [{"kind": "t3_words", "acts": [{"kind": kind}]},
                                                      {"kind": "t3_delivery", "delivery": "sent"}]
                            (out / "server" / f"{name}.json").write_text(json.dumps(record), encoding="utf-8")
                            load(n)

                    client = Mock()
                    if isinstance(response, Exception):
                        client.call.side_effect = response
                    else:
                        client.call.return_value = response
                    run = self.run_call(out, play, Judge(client), build, sid=sid)
                    table, green = S.rows({sid: [run]}, [S.Scenario(sid, "", build)])
                    self.assertEqual((table[0][1], green), ("ERROR", False))
                    self.assertIn("judge", run.error.lower())

    def test_a_live_shutdown_timeout_still_stops_t3_and_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = SimpleNamespace(out=Path(tmp), long=False, only=None, spread=None, port=0, lfm_port=0,
                                   max_minutes=1)
            world = Mock()
            rig = R.Rig(args.out / "live", args.port, args.max_minutes)
            rig.__enter__ = Mock(return_value=rig)
            rig.child = Mock()
            rig.child.poll.return_value = None
            rig.child.wait.side_effect = [subprocess.TimeoutExpired("live child", 30),
                                          subprocess.TimeoutExpired("live child", 15), 0]
            with (patch.object(S, "SCENARIOS", (self.sc(),)), patch.object(S, "World", return_value=world),
                  patch.object(R, "Rig", return_value=rig), patch.object(S, "default_decider", return_value=True),
                  patch.object(S.lc, "use_cache"), patch.object(S, "ledger", return_value=[]),
                  patch.object(S, "run_once", return_value=result("x", True)), patch.object(S, "show") as show):
                try:
                    status = S.gate(args)
                finally:
                    world.down.assert_called_once()
            rig.child.kill.assert_called_once()
            self.assertEqual(status, 1)
            self.assertEqual(show.call_args.args[0][0][1], "ERROR")


class FakeServer:
    """What the client sees of the live server: after speech and 700 ms of silence it sends one turn's control messages
    and a short reply, and when `news_after` is set it says news by itself that long after a turn."""

    def __init__(self, news_after=None):
        self.turns, self.speaking, self.quiet, self.news_after = 0, False, 0, news_after

    async def handle(self, ws):
        await ws.send(json.dumps({"type": "ready", "mode": "vad", "silence_ms": 700}))
        async for message in ws:
            if isinstance(message, str):
                data = json.loads(message)
                if data.get("type") == "ping":
                    await ws.send(json.dumps({"type": "pong", "t": data["t"]}))
                continue
            loud = np.abs(np.frombuffer(message, dtype="<i2")).max() > 500
            if loud:
                self.speaking, self.quiet = True, 0
            elif self.speaking:
                self.quiet += 20
                if self.quiet >= 700:
                    self.speaking = False
                    await self.reply(ws)

    async def reply(self, ws):
        self.turns += 1
        n = self.turns
        await ws.send(json.dumps({"type": "turn_start", "turn": n}))
        await ws.send(json.dumps({"type": "turn_end", "turn": n, "trailing_ms": 700}))
        await ws.send(json.dumps({"type": "reply_start", "turn": n, "rate": 24000}))
        await ws.send(b"\x00\x00" * 4800)
        await ws.send(json.dumps({"type": "reply_end", "turn": n}))
        done = {"type": "turn_done", "turn": n, "transcript": "x", "replies": ["ok"], "rows": [], "metrics": {},
                "cancelled": None}
        await ws.send(json.dumps(done))
        if self.news_after is not None and n == 2:
            await asyncio.sleep(self.news_after)
            self.turns += 1
            await ws.send(json.dumps({"type": "turn_done", "turn": self.turns, "idle": True, "replies": ["news"],
                                      "rows": ["done-event"], "metrics": {}, "cancelled": None}))


class Holding(unittest.TestCase):
    def clips(self, tmp, names):
        out = {}
        for name in names:
            path = Path(tmp) / f"{name}.wav"
            tone = (np.sin(np.linspace(0, 2000, 4800)) * 12000).astype("<i2")
            sf.write(str(path), tone, 16000, subtype="PCM_16")
            out[name] = path
        return out

    def hold(self, script, names, news_after=None):
        with tempfile.TemporaryDirectory() as tmp:
            secret = Path(tmp) / "secret"
            secret.write_text("s", encoding="utf-8")
            fake = FakeServer(news_after)
            seen = []

            async def main():
                async with websockets.serve(fake.handle, "127.0.0.1", 0) as server:
                    port = server.sockets[0].getsockname()[1]
                    args = R.client_module.build_parser().parse_args(
                        ["--server", f"ws://127.0.0.1:{port}", "--secret-file", str(secret), "--log-dir", tmp,
                         "--output", "null", "--no-record"])
                    caller = R.CallClient(args, script(seen), self.clips(tmp, names), lambda n: SimpleNamespace(n=n))
                    await asyncio.wait_for(caller.run(), 60)

            asyncio.run(main())
            return seen, fake

    def test_the_caller_picks_its_next_line_from_what_the_server_did(self):
        def script(seen):
            heard = yield R.Say("a")
            seen.append([t.n for t in heard.turns])
            heard = yield R.Say("b" if heard.turns else "c")
            seen.append([t.n for t in heard.turns])

        seen, fake = self.hold(script, ["a", "b", "c"])
        self.assertEqual((seen, fake.turns), ([[1], [2]], 2))

    def test_a_line_said_over_the_reply_ends_the_step_with_its_own_turn(self):
        def script(seen):
            heard = yield R.Say("a", over=("b", 300))
            seen.append([t.n for t in heard.turns])

        seen, fake = self.hold(script, ["a", "b"])
        self.assertEqual((seen, fake.turns), ([[1, 2]], 2))

    def test_a_caller_waits_for_news_the_assistant_says_by_itself(self):
        def script(seen):
            yield R.Say("a")
            yield R.Say("a")
            heard = yield R.WaitNews(20)
            seen.append((heard.timed_out, [t.n for t in heard.turns]))

        seen, _ = self.hold(script, ["a"], news_after=1.0)
        self.assertEqual(seen, [(False, [3])])

    def test_waiting_for_news_that_never_comes_runs_out(self):
        def script(seen):
            yield R.Say("a")
            heard = yield R.WaitNews(1.5)
            seen.append(heard.timed_out)

        seen, _ = self.hold(script, ["a"])
        self.assertEqual(seen, [True])


if __name__ == "__main__":
    unittest.main()
