"""The shadow decider: the question it asks, its per-task comparison, and a call that cannot reach the turn.

These stand in for the decider, so they say nothing about how often the two sides really agree. They pin what the
record means: which action each side took on each task, what the four verdicts are, and that a broken call is one
line with `error` set rather than an exception into the turn that already played its reply.
"""

import asyncio
import contextlib
import io
import json
import tempfile
import threading
import unittest
from unittest import mock

from voice.live import shadow, shadow_report
from voice.segmentation.contract import (Correction, CorrectionMode, Decisions, Kind, Readiness, SegmentDecision,
                                         TranscriptUpdate, decision_request, Segment)


def body(segments, tasks=()):
    """A decisions body shaped like `decider` mode's final call."""
    pieces = [Segment(f"s{i + 1}", 0, len(text), text, open=False) for i, text in enumerate(segments)]
    return decision_request(TranscriptUpdate(9, " ".join(segments), True), pieces, list(tasks), {}, tools=None,
                            arguments=False)


def decided(sid, kind=Kind.REQUEST, ready=Readiness.DISPATCH_NOW, side=0.0, confidence=None, corrects=None):
    return SegmentDecision(sid=sid, kind=kind, readiness=ready, depends_on=None, corrects=corrects,
                           side_effect=side >= 0.5, tool=None,
                           confidence=confidence or {"kind": 0.8, "ready": 0.9, "side_effect": side})


def task(task_id, instruction):
    return {"task": task_id, "instruction": instruction, "status": "proposed", "sources": ["orchestrator"]}


class Answer:
    """A decider that answers with one canned response, or raises, and counts its calls."""

    def __init__(self, response=None, error=None):
        self.response, self.error, self.calls = response, error, []

    def call(self, route, body, label="", fresh=False):
        self.calls.append({"label": label, "fresh": fresh, "questions": len(body["questions"])})
        if self.error is not None:
            raise self.error
        return {"route": route, "label": label, "status": 200, "model": "test:free",
                "response": self.response or {"answers": {}}, "timing": {"ms": 1.0, "started_at": "x"}}


class TaskDecisionTest(unittest.TestCase):
    def test_a_ready_request_passes_the_gate_and_a_near_tie_waits(self):
        answers = Decisions({"s1": decided("s1"), "s2": decided("s2", confidence={"ready": 0.3, "side_effect": 0.0})},
                            {}, [])
        decisions = shadow.task_decisions(answers, body(["check the weather", "and the news"]))
        self.assertEqual([d["action"] for d in decisions], ["run", "hold"])
        self.assertTrue(decisions[0]["gate"]["passed"])
        self.assertEqual(decisions[1]["gate"]["weakest"], "ready")

    def test_a_side_effect_needs_the_stricter_bar(self):
        answers = Decisions({"s1": decided("s1", side=1.0, confidence={"ready": 0.9, "side_effect": 1.0})}, {}, [])
        decisions = shadow.task_decisions(answers, body(["send Sam the slides"]))
        self.assertTrue(decisions[0]["side_effect"])
        self.assertEqual(decisions[0]["action"], "run")
        weak = Decisions({"s1": decided("s1", side=1.0, confidence={"ready": 0.9, "side_effect": 0.7})}, {}, [])
        self.assertEqual(shadow.task_decisions(weak, body(["send Sam the slides"]))[0]["action"], "hold")

    def test_a_segment_that_is_not_a_request_decides_nothing(self):
        answers = Decisions({"s1": decided("s1", kind=Kind.CHITCHAT)}, {}, [])
        decisions = shadow.task_decisions(answers, body(["hey, can you hear me"]))
        self.assertEqual(decisions[0]["action"], "drop")

    def test_a_correction_acts_on_the_task_it_names(self):
        answers = Decisions({"s1": decided("s1", kind=Kind.CORRECTION,
                                          corrects=Correction("s1", CorrectionMode.CANCEL))}, {}, [])
        tasks = [task("task_1", "set a timer for ten minutes")]
        decisions = shadow.task_decisions(answers, body(["set a timer for ten minutes"], tasks))
        self.assertEqual(decisions[0]["key"], "task_1", "the words tie it to the task the turn already made")
        self.assertEqual(decisions[0]["action"], "cancel")


class AgreementTest(unittest.TestCase):
    def test_the_four_verdicts(self):
        answers = Decisions({
            "s1": decided("s1"),
            "s2": decided("s2", confidence={"ready": 0.2, "side_effect": 0.0}),
            "s3": decided("s3"),
            "s4": decided("s4", kind=Kind.CORRECTION, corrects=Correction("s4", CorrectionMode.CANCEL)),
        }, {}, [])
        tasks = [task(f"task_{i}", text) for i, text in
                 enumerate(["the weather", "the news", "a timer", "the lights"], start=1)]
        asked = {"body": body(["the weather", "the news", "a timer", "the lights"], tasks)}
        decisions = shadow.task_decisions(answers, asked["body"])
        lfm = [{"name": "run_task", "action": "run", "key": "task_1", "task": "task_1", "instruction": None},
               {"name": "run_task", "action": "run", "key": "task_2", "task": "task_2", "instruction": None},
               {"name": "run_task", "action": "run", "key": "task_4", "task": "task_4", "instruction": None}]
        result = shadow.agreement(lfm, decisions)
        self.assertEqual((result["agree"], result["disagree"], result["decider_only"], result["lfm_only"]),
                         (1, 1, 1, 1))
        self.assertEqual({row["task"]: row["verdict"] for row in result["rows"]},
                         {"task_1": "agree", "task_2": "lfm_only", "task_3": "decider_only", "task_4": "disagree"})

    def test_a_row_carries_what_the_turn_did_when_the_task_had_no_id_yet(self):
        answers = Decisions({"s1": decided("s1")}, {}, [])
        asked = shadow.request("turn_001", "turn_001.json", "decider", body(["the weather"]),
                               outcomes=[{"task": "task_1", "status": "dispatched", "instruction": "the weather"}])
        result = shadow.agreement([], shadow.task_decisions(answers, asked["body"]), outcomes=asked["outcomes"])
        self.assertEqual(result["rows"][0]["task"], "new:s1", "the shadow saw no task before the turn's own call")
        self.assertEqual(result["rows"][0]["turn"], "dispatched", "the same words tie it to what the turn did")

    def test_a_decider_piece_that_makes_no_task_is_not_a_row(self):
        answers = Decisions({"s1": decided("s1", kind=Kind.CHITCHAT)}, {}, [])
        result = shadow.agreement([], shadow.task_decisions(answers, body(["mm-hm"])))
        self.assertEqual(result["rows"], [])
        self.assertEqual(sum(result[name] for name in ("agree", "disagree", "decider_only", "lfm_only")), 0)


class LfmCallsTest(unittest.TestCase):
    def test_task_calls_are_read_and_other_calls_ignored(self):
        turn = {"function_calls": [
            {"name": "get_task_status", "arguments": "{}"},
            {"name": "run_task", "arguments": json.dumps({"task_id": "task_2"})},
            {"name": "delegate", "arguments": json.dumps({"instruction": "find the forecast", "side_effect": False})},
            {"name": "cancel_task", "arguments": "{not json"},
        ]}
        calls = shadow.lfm_calls(turn)
        self.assertEqual([(call["name"], call["action"]) for call in calls],
                         [("run_task", "run"), ("delegate", "run"), ("cancel_task", "cancel")])
        self.assertEqual(calls[0]["task"], "task_2")
        self.assertEqual(calls[2]["task"], None, "a broken argument block names no task")

    def test_a_turn_with_no_tool_session_names_no_calls(self):
        self.assertEqual(shadow.lfm_calls(None), [])


class RunTest(unittest.TestCase):
    def test_a_call_parses_the_answers_and_times_itself(self):
        asked = shadow.request("turn_001", "turn_001.json", "lfm",
                               body(["what's the weather in Denver"], [task("task_1", "what's the weather in Denver")]),
                               lfm=shadow.lfm_calls({"function_calls": [
                                   {"name": "run_task", "arguments": '{"task_id": "task_1"}'}]}),
                               outcomes=[{"task": "task_1", "status": "dispatched"}], intent="request")
        decider = Answer({"answers": {
            "s1_kind": {"type": "choice", "choice": "request", "confidence": 0.8},
            "s1_ready": {"type": "choice", "choice": "dispatch_now", "confidence": 0.9},
            "s1_side_effect": {"type": "noul", "noul": 0.0, "confidence": 0.0}}})
        entry = shadow.run(decider, asked)
        self.assertTrue(decider.calls[0]["fresh"], "a cached answer would report no latency and could not disagree")
        self.assertEqual(entry["transcript"]["words"], 6)
        self.assertEqual(len(entry["transcript"]["sha256"]), 12)
        self.assertEqual(entry["decider"]["tasks"][0]["action"], "run")
        self.assertEqual(entry["decider"]["tasks"][0]["key"], "task_1")
        self.assertEqual(entry["agreement"]["agree"], 1)
        self.assertEqual(entry["call"]["status"], 200)
        self.assertNotIn("error", entry)

    def test_a_failed_call_is_one_line_with_the_error_and_no_decisions(self):
        asked = shadow.request("turn_002", "turn_002.json", "lfm", body(["check the weather"]))
        entry = shadow.run(Answer(error=ConnectionError("decider down")), asked)
        self.assertIn("decider down", entry["error"])
        self.assertEqual(entry["decider"]["tasks"], [])
        self.assertFalse(entry["call"]["ok"])
        self.assertEqual(entry["agreement"]["agree"], 0)

    def test_a_client_without_a_cache_still_answers(self):
        class NoFresh:
            def call(self, route, body, label=""):
                return {"status": 200, "response": {"answers": {}}, "timing": {"ms": 1.0}}

        entry = shadow.run(NoFresh(), shadow.request("turn_003", "turn_003.json", "lfm", body(["hello"])))
        self.assertTrue(entry["call"]["ok"])

    def test_the_timeout_line_says_so(self):
        asked = shadow.request("turn_004", "turn_004.json", "decider", body(["check the weather"]))
        entry = shadow.timed_out(asked, asyncio.TimeoutError())
        self.assertTrue(entry["call"]["timed_out"])
        self.assertFalse(entry["call"]["ok"])
        self.assertIn("timed out", entry["error"])

    def test_a_busy_decider_is_given_up_on_rather_than_queued(self):
        asked = shadow.request("turn_005", "turn_005.json", "lfm", body(["check the weather"]))
        decider, lock = Answer(), threading.Lock()
        lock.acquire()
        try:
            with mock.patch.object(shadow, "TIMEOUT_S", 0.05):
                entry = shadow.run(decider, asked, lock=lock)
        finally:
            lock.release()
        self.assertEqual(decider.calls, [], "no second request goes in flight")
        self.assertTrue(entry["call"]["timed_out"])
        self.assertIn("busy", entry["error"])


class LogTest(unittest.TestCase):
    def test_one_line_per_utterance_and_the_report_reads_them_back(self):
        with tempfile.TemporaryDirectory() as directory:
            log = shadow.Log(directory)
            asked = shadow.request("turn_001", "turn_001.json", "decider", body(["check the weather"]))
            log.write(shadow.run(Answer(), asked))
            log.write(shadow.run(Answer(), asked))
            self.assertEqual(len(log.lines()), 2)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(shadow_report.main([directory]), 0)
            table = out.getvalue()
            self.assertIn("turn_001", table)
            self.assertIn("authority: decider", table)
            self.assertIn("totals", table)

    def test_a_call_that_fails_is_counted_as_an_error(self):
        with tempfile.TemporaryDirectory() as directory:
            log = shadow.Log(directory)
            log.write(shadow.run(Answer(error=TimeoutError("slow")),
                                 shadow.request("turn_001", "turn_001.json", "lfm", body(["hello"]))))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                shadow_report.main([directory])
            self.assertIn("1 of 1 shadow calls failed", out.getvalue())
            self.assertIn("error", out.getvalue())
