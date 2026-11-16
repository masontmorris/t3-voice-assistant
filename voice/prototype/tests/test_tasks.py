import unittest
from concurrent.futures import Future, wait

from voice.prototype.tasks import DEFAULT_RESULT, Execution, MockTaskService, Run, build_snapshot
from voice.segmentation.contract import (Correction, CorrectionMode, Decisions, Kind, Readiness, SegmentDecision,
                                         TranscriptUpdate)
from voice.segmentation.spans import clause_segments
from voice.segmentation.tasks import Orchestrator, Status
from voice.tools.tests.replay import replay_registry


def decide(sid, kind="request", ready="dispatch_now", depends=None, corrects=None, side_effect=False):
    correction = None
    if corrects:
        mode, _, target = corrects.partition("_")
        correction = Correction(target, CorrectionMode(mode))
    return SegmentDecision(sid, Kind(kind), Readiness(ready), depends, correction, side_effect)


class ScriptedSegmenter:
    """The real orchestrator, with decisions written by the test instead of the decider."""

    def __init__(self):
        self.orch = Orchestrator()

    @property
    def dispatch_log(self):
        return self.orch.dispatch_log

    def records(self):
        return self.orch.task_records()

    def submit_delegate(self, instruction):
        self.orch.submit_delegate(instruction)

    def step(self, revision, text, final=False, delegates=None, **by_sid):
        self.orch.apply(TranscriptUpdate(revision, text, final), clause_segments(text, final),
                        Decisions({sid: decide(sid, **kw) for sid, kw in by_sid.items()}, delegates or {}, []))


def service_after(text, delay_s=4.0, **by_sid):
    segmenter = ScriptedSegmenter()
    service = MockTaskService(segmenter, "conv_test", delay_s)
    segmenter.step(1, text, final=True, **by_sid)
    service.sync(now=10.0)
    return segmenter, service


def statuses(snapshot):
    return {t["description"]: t["status"] for t in snapshot["tasks"]}


class MockWorkerTest(unittest.TestCase):
    def test_snapshot_reports_the_actual_running_lookup(self):
        segmenter = ScriptedSegmenter()
        segmenter.step(1, "What about Saturday?", final=True, s1={})
        execution = Execution("task_1", Run.RUNNING, 0, 5,
                              tool={"tool": "weather", "arguments": {"place": "Denver", "when": "saturday"}})
        snapshot = build_snapshot(segmenter.records(), {"task_1": execution}, "conv", "utt", "What about Saturday?")
        self.assertEqual(snapshot["tasks"][0]["capability"], "weather")
        self.assertEqual(snapshot["tasks"][0]["arguments"], {"place": "Denver", "when": "saturday"})

    def test_live_unavailable_task_is_failed_without_mock_completion(self):
        segmenter = ScriptedSegmenter()
        service = MockTaskService(segmenter, "conv_test", simulate=False)
        segmenter.step(1, "Translate this page.", final=True, s1={})
        service.sync(now=10.0)
        task = service.snapshot("utt_1", "Translate this page.")["tasks"][0]
        self.assertEqual(task["status"], "failed")
        self.assertIn("can't carry out", task["result_summary"])
        self.assertEqual(service.executions["task_1"].mode, "unavailable")
        self.assertEqual(service.poll(now=100), [])

    def test_a_read_only_task_completes_after_the_delay_with_a_canned_result(self):
        _, service = service_after("What's the weather in Denver tomorrow?", s1={})
        self.assertEqual(service.poll(now=13.9), [])
        self.assertEqual(statuses(service.snapshot("utt_1", "")), {"What's the weather in Denver tomorrow?": "running"})
        self.assertEqual(service.poll(now=14.0), ["task_1"])
        task = service.snapshot("utt_1", "")["tasks"][0]
        self.assertEqual((task["status"], task["result_summary"]), ("completed", "Mock forecast: sunny, high of 54 degrees."))

    def test_an_unknown_request_gets_the_default_result(self):
        _, service = service_after("Translate this page.", s1={})
        service.poll(now=20.0)
        self.assertEqual(service.snapshot("utt_1", "")["tasks"][0]["result_summary"], DEFAULT_RESULT)

    def test_side_effect_tasks_are_held_and_never_run(self):
        _, service = service_after("Tell my dad I'm home and turn the lights on.",
                                   s1={"side_effect": True}, s2={"side_effect": True})
        self.assertEqual(service.poll(now=1000.0), [])
        self.assertIsNone(service.next_due())
        self.assertEqual(set(statuses(service.snapshot("utt_1", "")).values()), {"held_for_confirmation"})
        self.assertEqual(service.take_updates(), [])

    def test_a_delegate_that_matches_no_segment_is_held(self):
        segmenter = ScriptedSegmenter()
        service = MockTaskService(segmenter, "conv_test")
        segmenter.submit_delegate("Find the report and email Dana.")
        segmenter.step(1, "Find the report and email Dana.", final=True, delegates={"d1": None},
                       s1={}, s2={"side_effect": True, "depends": "s1"})
        service.sync(now=0.0)
        held = [t for t in service.snapshot("utt_1", "")["tasks"] if t["status"] == "held_for_confirmation"]
        self.assertEqual([t["description"] for t in held], ["Find the report and email Dana."])

    def test_a_delegate_for_a_dispatched_segment_runs_once(self):
        segmenter = ScriptedSegmenter()
        service = MockTaskService(segmenter, "conv_test")
        segmenter.step(1, "Add milk to the shopping list. And how long", s1={}, s2={})
        service.sync(now=0.0)
        segmenter.submit_delegate("add milk to the shopping list")
        segmenter.step(2, "Add milk to the shopping list. And how long", delegates={"d1": "s1"}, s1={}, s2={})
        service.sync(now=1.0)
        self.assertEqual(list(service.executions), ["task_1"])
        self.assertEqual(service.executions["task_1"].dispatched_at, 0.0)

    def test_a_cancel_request_stops_a_running_task(self):
        segmenter = ScriptedSegmenter()
        service = MockTaskService(segmenter, "conv_test")
        segmenter.step(1, "Check my calendar for Friday and text Sam", s1={}, s2={"side_effect": True})
        service.sync(now=0.0)
        segmenter.step(2, "Check my calendar for Friday and text Sam, actually never mind the calendar.", final=True,
                       s1={}, s2={"side_effect": True}, s3={"kind": "correction", "corrects": "cancel_s1"})
        service.sync(now=1.0)
        self.assertEqual(service.poll(now=100.0), [])
        self.assertEqual(statuses(service.snapshot("utt_1", ""))["Check my calendar for Friday"], "cancelled")


class UpdatesTest(unittest.TestCase):
    def test_a_completion_after_the_snapshot_is_delivered_exactly_once(self):
        _, service = service_after("What time is it in Tokyo right now?", s1={})
        self.assertEqual(statuses(service.snapshot("utt_1", "")), {"What time is it in Tokyo right now?": "running"})
        service.poll(now=15.0)
        updates = service.take_updates()
        self.assertEqual([(u["type"], u["task_id"], u["status"]) for u in updates], [("task_update", "task_1", "completed")])
        self.assertEqual(updates[0]["result_summary"], "Mock clock: it is 9:15 in the morning in Tokyo.")
        self.assertEqual(service.take_updates(), [])

    def test_a_completion_the_snapshot_carried_gets_no_update(self):
        _, service = service_after("What time is it in Tokyo right now?", delay_s=0.0, s1={})
        service.poll(now=10.0)
        self.assertEqual(statuses(service.snapshot("utt_1", "")), {"What time is it in Tokyo right now?": "completed"})
        self.assertEqual(service.take_updates(), [])


class SnapshotTest(unittest.TestCase):
    def test_statuses_follow_the_records(self):
        segmenter, service = service_after(
            "Find last week's sales report and then email a summary to Dana. And what ferry?",
            s1={}, s2={"depends": "s1", "side_effect": True}, s3={"ready": "wait"})
        snapshot = service.snapshot("utt_1", "the transcript")
        self.assertEqual(snapshot["tasks"], [
            {"task_id": "task_1", "description": "Find last week's sales report", "status": "running"},
            {"task_id": "task_2", "description": "and then email a summary to Dana.", "status": "pending",
             "depends_on": ["task_1"]},
            {"task_id": "task_3", "description": "And what ferry?", "status": "needs_clarification"},
        ])
        self.assertEqual(snapshot["clarification_needed"], ["And what ferry?"])
        self.assertEqual((snapshot["type"], snapshot["conversation_id"], snapshot["final_transcript"]),
                         ("utterance_snapshot", "conv_test", "the transcript"))

    def test_corrected_tasks_are_left_out_and_the_correction_is_reported(self):
        segmenter = ScriptedSegmenter()
        segmenter.step(1, "Set a reminder for Tuesday at three, no wait, make it Wednesday.", final=True,
                       s1={"side_effect": True}, s2={"kind": "correction", "corrects": "replace_s1"})
        service = MockTaskService(segmenter, "conv_test")
        service.sync(now=0.0)
        tasks = service.snapshot("utt_1", "")["tasks"]
        self.assertEqual([(t["task_id"], t["status"]) for t in tasks], [("task_2", "held_for_confirmation")])
        self.assertIn("make it Wednesday", tasks[0]["description"])

    def test_the_builder_reports_only_what_the_records_say(self):
        records = [{"task_id": "task_1", "status": "proposed", "instruction": "look it up", "depends_on": []}]
        snapshot = build_snapshot(records, {}, "conv", "utt", "look it up")
        self.assertEqual(snapshot["tasks"], [{"task_id": "task_1", "description": "look it up", "status": "pending"}])
        self.assertIsNone(snapshot["clarification_needed"])
        self.assertEqual(build_snapshot([], {}, "conv", "utt", "")["tasks"], [])


class HandoffToolsTest(unittest.TestCase):
    def setUp(self):
        self.segmenter, self.service = service_after("Find the sales report.", s1={})

    def test_a_read_only_delegate_runs_and_a_side_effect_delegate_is_held(self):
        read, _ = self.service.call_tool("delegate", {"instruction": "check the weather in Denver",
                                                      "side_effect": False}, "check the weather in Denver", 10.0)
        send, _ = self.service.call_tool("delegate", {"instruction": "email Dana the notes", "side_effect": True},
                                         "email Dana the notes", 10.0)
        unflagged, _ = self.service.call_tool("delegate", {"instruction": "post the photo"}, "post the photo", 10.0)
        states = {k: v.state.value for k, v in self.service.executions.items()}
        self.assertEqual(states[read["task_id"]], "running")
        self.assertEqual(states[send["task_id"]], "held_for_confirmation")
        self.assertEqual(states[unflagged["task_id"]], "held_for_confirmation")

    def test_a_completed_task_cannot_be_cancelled(self):
        self.service.poll(now=14.0)
        result, _ = self.service.call_tool("cancel_task", {"task_id": "task_1"}, None, 15.0)
        self.assertFalse(result["ok"])
        self.assertEqual(self.service.executions["task_1"].state.value, "completed")

    def test_a_cancel_stops_the_running_task_and_a_bad_call_changes_nothing(self):
        bad, _ = self.service.call_tool("cancel_task", {}, None, 11.0)
        result, events = self.service.call_tool("cancel_task", {"task_id": "task_1"}, None, 11.0)
        self.assertFalse(bad["ok"])
        self.assertTrue(result["ok"])
        self.assertEqual(events, [{"task_id": "task_1", "event": "cancelled"}])

    def test_the_snapshot_reports_a_dispatch_hold_as_pending_and_keeps_unreviewed_speech(self):
        orch = self.segmenter.orch
        held = orch._new_task("email dana", 0, {"orchestrator"}, side_effect=True)
        orch._move(held, Status.HELD, "test")
        snapshot = self.service.snapshot("utt_1", "", "and email Dana")
        self.assertEqual(statuses(snapshot)["email dana"], "pending")
        self.assertEqual(snapshot["unreviewed_speech"], "and email Dana")


class RealLookupTest(unittest.TestCase):
    def service(self, default_place="Denver"):
        self.tools, self.fetch, self.mcp = replay_registry(default_place)
        self.segmenter = ScriptedSegmenter()
        service = MockTaskService(self.segmenter, "conv_test", tools=self.tools)
        self.addCleanup(service.close)
        return service

    def finish(self, service, now):
        wait(service.pending_calls(), timeout=5)
        return service.poll(now)

    def test_a_segmented_weather_task_completes_with_the_lookups_summary_and_record(self):
        service = self.service()
        self.segmenter.step(1, "What's the weather going to be in Denver this weekend, and what time is it in Tokyo?",
                            final=True, s1={}, s2={})
        service.sync(now=1.0)
        self.assertEqual(self.finish(service, now=1.5), ["task_1", "task_2"])
        tasks = service.snapshot("utt_1", "")["tasks"]
        self.assertEqual([t["status"] for t in tasks], ["completed", "completed"])
        self.assertTrue(tasks[0]["result_summary"].startswith("Forecast for Denver, Colorado. Saturday"))
        self.assertTrue(tasks[1]["result_summary"].startswith("In Tokyo, Japan it is"))
        record = service.executions["task_1"].tool
        self.assertEqual((record["tool"], record["arguments"], record["outcome"]),
                         ("weather", {"place": "Denver", "when": "weekend"}, "ok"))
        self.assertEqual(record["summary"], tasks[0]["result_summary"])
        self.assertGreaterEqual(record["latency_s"], 0.0)
        self.assertEqual(service.executions["task_2"].tool["tool"], "time")

    def test_a_delegated_request_takes_the_same_registry(self):
        service = self.service()
        self.segmenter.submit_delegate("what time is it in Tokyo")
        self.segmenter.step(1, "What time is it in Tokyo right now?", final=True, delegates={"d1": "s1"}, s1={})
        service.sync(now=0.0)
        self.finish(service, now=0.5)
        self.assertEqual(self.mcp.calls, [("get_current_time", {"timezone": "Asia/Tokyo"})])
        self.assertEqual(service.executions["task_1"].state, "completed")

    def test_a_failed_lookup_is_reported_as_failed_with_its_reason_exactly_once(self):
        service = self.service(default_place=None)
        self.segmenter.step(1, "I wonder if it's going to rain on Saturday.", final=True, s1={})
        service.sync(now=0.0)
        self.assertEqual(statuses(service.snapshot("utt_1", "")), {"I wonder if it's going to rain on Saturday.": "running"})
        self.finish(service, now=0.2)
        updates = service.take_updates()
        self.assertEqual([(u["status"], u["result_summary"]) for u in updates],
                         [("failed", "The weather lookup needs a place, and none was given.")])
        self.assertEqual(service.take_updates(), [])

    def test_requests_no_tool_covers_still_run_on_the_mock(self):
        service = self.service()
        self.segmenter.step(1, "Find last week's sales report", final=True, s1={})
        service.sync(now=0.0)
        self.assertEqual(service.pending_calls(), [])
        self.assertEqual(service.poll(now=4.0), ["task_1"])
        self.assertEqual(service.executions["task_1"].result_summary, "Mock file search: found Sales_Week45.xlsx.")
        self.assertEqual(service.executions["task_1"].mode, "simulated")

    def test_side_effect_tasks_never_reach_a_tool(self):
        service = self.service()
        self.segmenter.step(1, "Email Dana the weather in Denver.", final=True, s1={"side_effect": True})
        service.sync(now=0.0)
        self.assertEqual((service.pending_calls(), self.fetch.urls), ([], []))
        self.assertEqual(service.executions["task_1"].state, "held_for_confirmation")

    def test_a_lookup_that_never_returns_fails_at_its_deadline(self):
        service = self.service()
        self.segmenter.step(1, "What's the weather in Denver?", final=True, s1={})
        service._pool.submit = lambda *args: Future()
        service.sync(now=0.0)
        self.assertEqual(service.poll(now=5.9), [])
        self.assertEqual(service.poll(now=6.0), ["task_1"])
        execution = service.executions["task_1"]
        self.assertEqual((execution.state, execution.tool["outcome"]), ("failed", "timeout"))
        self.assertEqual(execution.result_summary, "The weather lookup did not answer in time.")


class GatedServiceTest(unittest.TestCase):
    def test_confidence_holds_are_pending_not_confirmation_requests(self):
        for instruction, side_effect in [("check the weather in Denver", False), ("email Dana the notes", True)]:
            with self.subTest(instruction=instruction):
                segmenter = ScriptedSegmenter()
                service = MockTaskService(segmenter, "conv_test", holds_side_effects=False)
                self.addCleanup(service.close)
                task = segmenter.orch._new_task(instruction, 0, {"orchestrator"}, side_effect=side_effect)
                segmenter.orch._log(task, "gate", passed=False, score=0.31, weakest="ready", threshold=0.8)
                segmenter.orch._move(task, Status.HELD, "gate")
                self.assertEqual(service.snapshot("utt_1", instruction, notes=True)["tasks"][0]["status"], "pending")
                self.assertEqual(segmenter.orch.snapshot("conv_test", "utt_1")["tasks"][0]["status"], "pending")

    def test_a_dispatched_side_effect_runs_on_the_mock_and_never_reaches_a_tool(self):
        tools, fetch, _ = replay_registry("Denver")
        segmenter = ScriptedSegmenter()
        service = MockTaskService(segmenter, "conv_test", tools=tools, holds_side_effects=False)
        self.addCleanup(service.close)
        segmenter.step(1, "Email Dana the weather in Denver.", final=True, s1={"side_effect": True})
        service.sync(now=0.0)
        self.assertEqual((service.pending_calls(), fetch.urls), ([], []))
        self.assertEqual(service.executions["task_1"].state, "running")
        self.assertEqual(service.poll(now=4.0), ["task_1"])

    def test_a_task_that_has_not_run_carries_a_note_for_the_tools_session(self):
        segmenter = ScriptedSegmenter()
        service = MockTaskService(segmenter, "conv_test", holds_side_effects=False)
        orch = segmenter.orch
        held = orch._new_task("email dana", 0, {"orchestrator"}, side_effect=True)
        orch._log(held, "gate", passed=False, score=0.31, weakest="ready", threshold=0.8)
        orch._move(held, Status.HELD, "gate")
        entry = service.snapshot("utt_1", "", notes=True)["tasks"][0]
        self.assertEqual(entry["note"], "Not started: the service's confidence in its ready answer was 0.31, "
                                        "below 0.80.")
        self.assertNotIn("note", service.snapshot("utt_1", "")["tasks"][0])

    def test_run_task_reaches_the_orchestrator(self):
        segmenter = ScriptedSegmenter()
        service = MockTaskService(segmenter, "conv_test", holds_side_effects=False)
        held = segmenter.orch._new_task("find the report", 0, {"orchestrator"})
        segmenter.orch._move(held, Status.HELD, "gate")
        result, events = service.call_tool("run_task", {"task_id": held.task_id}, None, 1.0)
        self.assertTrue(result["ok"])
        self.assertEqual(events[0]["event"], "running")


if __name__ == "__main__":
    unittest.main()
