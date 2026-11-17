"""The shadow decider: the question it asks, its per-task comparison, and a call that cannot reach the turn.

These stand in for the decider, so they say nothing about how often the two sides really agree. They pin what the
record means: which action each side took on each task, what the four verdicts are, and that a broken call is one
line with `error` set rather than an exception into the turn that already played its reply.
"""

import asyncio
import contextlib
import io
import tempfile
import threading
import unittest

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


def delegate(instruction, task_id, linked=False):
    """One `tool_results` entry for LFM3's delegate, with the task the service gave it."""
    result = {"ok": True, "task_id": task_id, **({"duplicate_of": task_id} if linked else {})}
    return {"name": "delegate", "arguments": {"instruction": instruction, "side_effect": False}, "result": result}


class AgreementTest(unittest.TestCase):
    def test_the_verdicts(self):
        answers = Decisions({
            "s1": decided("s1"),
            "s2": decided("s2", confidence={"ready": 0.2, "side_effect": 0.0}),
            "s3": decided("s3"),
            "s4": decided("s4", kind=Kind.CORRECTION, corrects=Correction("s4", CorrectionMode.CANCEL)),
            "s5": decided("s5", kind=Kind.CORRECTION, corrects=Correction("s5", CorrectionMode.REPLACE)),
        }, {}, [])
        texts = ["the weather", "the news", "a timer", "the lights", "the radio", "the alarm"]
        tasks = [task(f"task_{i}", text) for i, text in enumerate(texts, start=1)]
        decisions = shadow.task_decisions(answers, body(texts[:5], tasks))
        lfm = shadow.lfm_calls([{"name": "run_task", "arguments": {"task_id": "task_1"}, "result": {"ok": True}},
                                {"name": "run_task", "arguments": {"task_id": "task_2"}, "result": {"ok": True}},
                                {"name": "run_task", "arguments": {"task_id": "task_4"}, "result": {"ok": True}},
                                {"name": "amend_task", "arguments": {"task_id": "task_6", "new_request": "at 7"},
                                 "result": {"ok": True}}])
        result = shadow.agreement(lfm, decisions)
        self.assertEqual({row["task"]: row["verdict"] for row in result["rows"]},
                         {"task_1": "agree", "task_2": "lfm_only", "task_3": "decider_only", "task_4": "disagree",
                          "task_5": "decider_correction", "task_6": "lfm_correction"})
        self.assertEqual([result[name] for name in shadow.VERDICTS], [1, 1, 1, 1, 1, 1])

    def test_a_correction_with_nothing_on_the_other_side_is_not_agreement(self):
        answers = Decisions({"s1": decided("s1", kind=Kind.CORRECTION,
                                          corrects=Correction("s1", CorrectionMode.CANCEL))}, {}, [])
        result = shadow.agreement([], shadow.task_decisions(answers, body(["never mind"])))
        self.assertEqual(result["agree"], 0)
        self.assertEqual(result["decider_correction"], 1, "a withdrawal only the decider makes is visible")

    def test_a_row_is_keyed_by_the_task_the_turn_made_for_the_same_words(self):
        answers = Decisions({"s1": decided("s1")}, {}, [])
        asked = shadow.request("turn_001", "turn_001.json", "decider", body(["the weather"]),
                               outcomes=[{"task": "task_1", "status": "dispatched", "instruction": "the weather"}])
        result = shadow.agreement([], shadow.task_decisions(answers, asked["body"]), outcomes=asked["outcomes"])
        self.assertEqual([(row["task"], row["turn"]) for row in result["rows"]], [("task_1", "dispatched")])

    def test_a_delegate_and_the_piece_it_rewords_are_one_task(self):
        answers = Decisions({"s1": decided("s1")}, {}, [])
        lfm = shadow.lfm_calls([delegate("Order more coffee for the office.", "task_7")])
        outcomes = [{"task": "task_7", "status": "dispatched", "instruction": "Order more coffee for the office."}]
        result = shadow.agreement(lfm, shadow.task_decisions(answers, body(["and finally, order more coffee"])),
                                  outcomes=outcomes)
        self.assertEqual([(row["task"], row["decider"], row["lfm"], row["verdict"]) for row in result["rows"]],
                         [("task_7", "run", "run", "agree")], "one task, not a decider_only and an lfm_only")

    def test_a_linked_delegate_is_keyed_by_the_task_the_orchestrator_linked(self):
        answers = Decisions({"s1": decided("s1", confidence={"ready": 0.2, "side_effect": 0.0})}, {}, [])
        decisions = shadow.task_decisions(answers, body(["and send them to the team"],
                                                        [task("task_4", "and send them to the team")]))
        lfm = shadow.lfm_calls([delegate("Send the slides to the whole team.", "task_4", linked=True)])
        result = shadow.agreement(lfm, decisions)
        self.assertEqual([(row["task"], row["decider"], row["lfm"], row["verdict"]) for row in result["rows"]],
                         [("task_4", "hold", "run", "lfm_only")])

    def test_a_decider_piece_that_makes_no_task_is_not_a_row(self):
        answers = Decisions({"s1": decided("s1", kind=Kind.CHITCHAT)}, {}, [])
        result = shadow.agreement([], shadow.task_decisions(answers, body(["mm-hm"])))
        self.assertEqual(result["rows"], [])
        self.assertEqual(sum(result[name] for name in shadow.VERDICTS), 0)


class LfmCallsTest(unittest.TestCase):
    def test_task_calls_are_read_and_other_calls_ignored(self):
        calls = shadow.lfm_calls([
            {"name": "get_task_status", "arguments": {}, "result": {"ok": True}},
            {"name": "run_task", "arguments": {"task_id": "task_2"}, "result": {"ok": True, "task_id": "task_2"}},
            delegate("find the forecast", "task_3"),
            {"name": "delegate", "arguments": {"instruction": "say hi"}, "result": {"ok": False, "error": "nothing"}},
            {"name": "cancel_task", "arguments": {}, "result": {"ok": False, "error": "bad call to cancel_task"}},
        ])
        self.assertEqual([(call["name"], call["action"], call["key"]) for call in calls],
                         [("run_task", "run", "task_2"), ("delegate", "run", "task_3"),
                          ("delegate", "run", "new:lfm3"), ("cancel_task", "cancel", "new:lfm4")])
        self.assertEqual(calls[1]["task"], "task_3", "a delegate reaches the task its call returned")

    def test_a_turn_with_no_tool_session_names_no_calls(self):
        self.assertEqual(shadow.lfm_calls([]), [])


class Blocking:
    """A shadow client whose call waits until it is aborted, as a request in flight on a slow backend does."""

    def __init__(self):
        self.entered, self.aborted, self.closed = threading.Event(), threading.Event(), False

    def call(self, route, body, label="", fresh=False):
        self.entered.set()
        if not self.aborted.wait(5):
            raise AssertionError("never aborted")
        raise ConnectionAbortedError("the client was aborted")

    def abort(self):
        self.aborted.set()

    def close(self):
        self.closed = True


class LaneTest(unittest.TestCase):
    def test_the_lane_asks_on_a_client_of_its_own_and_closes_it(self):
        own = Blocking()

        class Turns(Answer):
            def lane(self, timeout_s):
                self.timeout_s = timeout_s
                return own

        turns, lane = Turns(), shadow.Lane()
        self.assertIs(lane.open(turns), own)
        self.assertEqual(turns.timeout_s, shadow.TIMEOUT_S)
        self.assertTrue(lane.busy)
        lane.finish()
        self.assertTrue(own.closed)
        self.assertFalse(lane.busy)

    def test_abort_ends_the_call_in_flight_and_its_line_says_so(self):
        own, lane = Blocking(), shadow.Lane()
        lane.open(own)
        asked = shadow.request("turn_001", "turn_001.json", "decider", body(["check the weather"]))
        result = {}
        worker = threading.Thread(target=lambda: result.update(shadow.run(lane.client, asked)))
        worker.start()
        self.assertTrue(own.entered.wait(5))
        lane.abort("a new utterance started")
        worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(lane.ended, "a new utterance started")
        self.assertIn("aborted", result["error"])
        self.assertFalse(result["call"]["ok"])


class RunTest(unittest.TestCase):
    def test_a_call_parses_the_answers_and_times_itself(self):
        asked = shadow.request("turn_001", "turn_001.json", "lfm",
                               body(["what's the weather in Denver"], [task("task_1", "what's the weather in Denver")]),
                               lfm=shadow.lfm_calls([{"name": "run_task", "arguments": {"task_id": "task_1"},
                                                      "result": {"ok": True, "task_id": "task_1"}}]),
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

    def test_an_answer_that_is_not_a_200_is_a_failed_call(self):
        class Refused(Answer):
            def call(self, route, body, label="", fresh=False):
                return {**super().call(route, body, label, fresh), "status": None, "response": None}

        entry = shadow.run(Refused(), shadow.request("turn_003", "turn_003.json", "lfm", body(["hello"])))
        self.assertFalse(entry["call"]["ok"])
        self.assertIn("http None", entry["error"])

    def test_the_timeout_line_says_so(self):
        asked = shadow.request("turn_004", "turn_004.json", "decider", body(["check the weather"]))
        entry = shadow.timed_out(asked, asyncio.TimeoutError())
        self.assertTrue(entry["call"]["timed_out"])
        self.assertFalse(entry["call"]["ok"])
        self.assertIn("timed out", entry["error"])

    def test_a_skipped_utterance_is_one_line_with_the_reason(self):
        asked = shadow.request("turn_005", "turn_005.json", "decider", body(["check the weather"]))
        entry = shadow.skipped(asked, "a turn was running")
        self.assertEqual(entry["skipped"], "a turn was running")
        self.assertFalse(entry["call"]["ok"])
        self.assertEqual(entry["decider"]["tasks"], [])


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
            self.assertIn("1 of 1 utterances have no shadow answer", out.getvalue())
            self.assertIn("error", out.getvalue())

    def test_the_table_derives_the_verdicts_from_the_stored_sides(self):
        with tempfile.TemporaryDirectory() as directory:
            log = shadow.Log(directory)
            entry = shadow.run(Answer({"answers": {
                "s1_kind": {"type": "choice", "choice": "request", "confidence": 0.8},
                "s1_ready": {"type": "choice", "choice": "dispatch_now", "confidence": 0.9},
                "s1_side_effect": {"type": "noul", "noul": 0.0, "confidence": 0.0}}}),
                shadow.request("turn_001", "turn_001.json", "lfm", body(["the weather"]),
                               lfm=shadow.lfm_calls([delegate("the weather", "task_1")]),
                               outcomes=[{"task": "task_1", "status": "dispatched", "instruction": "the weather"}]))
            entry["agreement"] = {**dict.fromkeys(shadow.VERDICTS, 0), "rows": []}
            log.write(entry)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                shadow_report.main([directory, "--rows"])
            self.assertIn("agree              task_1", out.getvalue(),
                          "the stored verdicts are the call's reading; the table reads the stored sides")
