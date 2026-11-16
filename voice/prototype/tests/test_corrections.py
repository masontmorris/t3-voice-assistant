"""A correction across turns, through the gated turn the live server runs, with in-memory sessions.

The calls are the stack's measured demo (evidence demo-stack, 2026-11-16): "What's the weather in Denver tomorrow?",
"And what about Saturday?", then a correction. The fake tools session does what LFM3 did on every turn of that run:
it delegates the unreviewed speech as a new request. These tests say nothing about what the voice model says, only
what the turn runs, what it reports, and what text it gives the voice model.
"""

import json
import tempfile
import unittest
from pathlib import Path

from voice.prototype import run as prototype
from voice.prototype.run import normalised
from voice.prototype.segmenter import use_decider_cache
from voice.prototype.tasks import MockTaskService, Run, amended
from voice.prototype.tests.test_run_shared_session import CLOCK, FakeSession
from voice.segmentation.hybrid import HybridSegmenter

DEMO = ("What's the weather in Denver tomorrow?", "And what about Saturday?")


class DemoSession(FakeSession):
    """The ASR hears this turn's words, and the tools session delegates its unreviewed speech, as LFM3 did."""

    heard = ""

    async def send(self, **event):
        if self.name == "asr" and event.get("type") == "input_audio_buffer.commit":
            self.transcript = DemoSession.heard
        if self.name == "tools" and event.get("type") == "response.create":
            self.sent.append(event)
            snapshot = json.loads(self.last_item_text().split("] ", 1)[1])
            call = {"type": "function_call", "name": "delegate", "call_id": "call_1",
                    "arguments": json.dumps({"instruction": snapshot.get("unreviewed_speech") or "",
                                             "side_effect": False})}
            await self.push({"type": "response.done", "response": {"status": "completed", "output": [call]}})
            return
        await super().send(**event)

    def last_item_text(self):
        item = next(e["item"] for e in reversed(self.sent) if e.get("type") == "conversation.item.create")
        return item["content"][0]["text"]


class Turn:
    rate, audio_s = 16_000, 0.0

    def __init__(self, n):
        self.name = self.stem = f"turn_{n:03d}"

    async def stream(self, voice, asr):
        await voice.send(type="input_audio_buffer.append", audio="AA==")
        return CLOCK


class CorrectionTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        use_decider_cache(Path(self.tmp.name) / "decider-cache", 20)
        original = prototype.Session.open

        async def open_session(url, name, clock, on_event=None):
            return await DemoSession.open(url, name, clock, on_event)

        prototype.Session.open = open_session
        self.addCleanup(setattr, prototype.Session, "open", original)

    async def call(self, *said):
        """One call on one voice session, carrying what the live server carries from turn to turn."""
        self.voice = await DemoSession.open("ws://unused", "voice", lambda: CLOCK)
        records, carried, items = [], [], []
        for n, words in enumerate(said, 1):
            DemoSession.heard = words
            mark = len(self.voice.sent)
            record = await prototype.run_clip(
                Turn(n), "c", False, "ws://unused", None, 0.0, Path(self.tmp.name), {}, "hybrid", None, "gated",
                voice=self.voice, snapshot_mode="reply-no-utterance",
                prior_utterances=tuple(r["final_transcript"] for r in records), prior_tasks=tuple(carried))
            carried += prototype.carried_tasks(record)
            records.append(record)
            items.append([e["item"]["content"][0]["text"] for e in self.voice.sent[mark:]
                          if e.get("type") == "conversation.item.create"])
        self.carried = carried
        return records, items

    def assert_no_item_holds(self, words, items):
        for text in items:
            self.assertNotIn(f" {normalised(words)} ", f" {normalised(text)} ",
                             "no injected item holds the current turn's words")

    async def test_correction_does_not_rerun_the_rejected_request(self):
        records, items = await self.call(*DEMO, "That's not what I meant.")
        self.assertEqual(len(records[1]["dispatch_log"]), 1, "the fixture's turn 2 did run a task to reject")
        third = records[2]
        self.assertEqual(third["dispatch_log"], [], "nothing runs: not the rejected request, not the correction")
        self.assertEqual(third["task_updates"], [], "no task is reported done")
        self.assertEqual([r["kind"] for r in third["replies"]], ["snapshot"], "one reply, and no task update reply")
        snapshot = items[2][-1]
        self.assertIn('"needs_clarification"', snapshot, "the voice is told to ask what they want changed")
        self.assertIn('"tasks": []', snapshot, "and that nothing is running")
        self.assert_no_item_holds("That's not what I meant.", items[2])
        self.assertEqual(len(self.carried), 2, "the call's task list gains nothing from the correction")

    async def test_correction_with_a_value_amends_the_rejected_task(self):
        records, items = await self.call(*DEMO, "No, Sunday.")
        third = records[2]
        self.assertEqual([d["instruction"] for d in third["dispatch_log"]], ["And what about Sunday?"],
                         "one task runs: turn 2's request with the day changed, not a repeat and not the correction")
        amended = next(t for t in third["tasks"] if t["status"] == "dispatched")
        self.assertEqual(amended["revises"], "turn_002:task_1", "it says which earlier task it replaces")
        snapshot = items[2][0]
        self.assertIn('"amended"', snapshot)
        self.assertIn('"Sunday"', snapshot)
        self.assert_no_item_holds("No, Sunday.", items[2])
        self.assertEqual([t["instruction"] for t in self.carried][-1], "And what about Sunday?",
                         "a later correction amends the amended task")


class ServiceTest(unittest.TestCase):
    def test_a_task_made_from_the_correction_itself_is_stopped(self):
        """The decider can read a short utterance during speech and make a task of it before the turn ends."""
        service = MockTaskService(HybridSegmenter(None, "t"), "conv", delay_s=4.0, holds_side_effects=False)
        service.call_tool("delegate", {"instruction": "That's not what I meant.", "side_effect": False}, None, 0.0)
        self.assertEqual(service.executions["task_1"].state, Run.RUNNING, "the fixture has a running task to stop")
        service.correct(None, (), 0.1)
        self.assertEqual(service.executions["task_1"].state, Run.CANCELLED)
        self.assertEqual(service.snapshot("utt_1", "")["correction"]["status"], "needs_clarification")


class AmendedTest(unittest.TestCase):
    def test_values(self):
        self.assertEqual(amended("What's the weather in Denver tomorrow?", "Boulder"),
                         ("What's the weather in Boulder tomorrow?", "Denver", "Boulder"))
        self.assertEqual(amended("Book a table at 7 pm", "8 pm"), ("Book a table at 8 pm", "7 pm", "8 pm"))
        self.assertIsNone(amended("And what about Saturday?", "Saturday"), "the same value changes nothing")
        self.assertIsNone(amended("What's the weather in Denver tomorrow?", "Sunday"), "no day to change")
        self.assertIsNone(amended("And what about Saturday?", "the other one"), "no value stated")


if __name__ == "__main__":
    unittest.main()
