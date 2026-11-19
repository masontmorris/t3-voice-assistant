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
from voice.prototype.tasks import MockTaskService, Run, amended
from voice.prototype.tests import isolate_decider_cache
from voice.prototype.tests.test_run_shared_session import CLOCK, FakeSession, Scripted, Source, heard_by
from voice.segmentation.hybrid import HybridSegmenter
from voice.tools.tests.replay import replay_registry

DEMO = ("What's the weather in Denver tomorrow?", "And what about Saturday?")
ASK_PCM = b"\x02\x00" * 480


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


class Call(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(isolate_decider_cache(Path(self.tmp.name) / "decider-cache", 20))
        original = prototype.Session.open

        async def open_session(url, name, clock, on_event=None):
            return await DemoSession.open(url, name, clock, on_event)

        prototype.Session.open = open_session
        self.addCleanup(setattr, prototype.Session, "open", original)

    async def call(self, *said, ask_pcm=ASK_PCM, tools=None):
        """One call on one voice session, carrying what the live server carries from turn to turn."""
        self.voice = await DemoSession.open("ws://unused", "voice", lambda: CLOCK)
        records, carried, items, self.spoken, self.asked = [], [], [], [], []
        for n, words in enumerate(said, 1):
            DemoSession.heard = words
            mark, spoken = len(self.voice.sent), []
            record = await prototype.run_clip(
                Turn(n), "c", False, "ws://unused", None, 0.0, Path(self.tmp.name), {}, "hybrid", tools, "gated",
                voice=self.voice, snapshot_mode="reply-no-utterance", ask_pcm=ask_pcm,
                speak=heard_by(spoken, with_bytes=True),
                prior_utterances=tuple(r["final_transcript"] for r in records), prior_tasks=tuple(carried))
            carried += prototype.carried_tasks(record)
            records.append(record)
            items.append([e["item"]["content"][0]["text"] for e in self.voice.sent[mark:]
                          if e.get("type") == "conversation.item.create"])
            self.spoken.append(spoken)
            self.asked.append(sum(e.get("type") == "response.create" for e in self.voice.sent[mark:]))
        self.carried = carried
        return records, items

    def assert_no_item_holds(self, words, items):
        for text in items:
            self.assertNotIn(f" {normalised(words)} ", f" {normalised(text)} ",
                             "no injected item holds the current turn's words")


class CorrectionTest(Call):
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

    async def test_a_bare_correction_speaks_the_fixed_line(self):
        records, _ = await self.call(*DEMO, "That's not what I meant.")
        self.assertEqual(self.asked[2], 0, "the voice model composes nothing on this turn")
        self.assertEqual(self.spoken[2], [(prototype.ASK_INSTEAD_LINE, len(ASK_PCM))], "the rendered line is played")
        self.assertEqual([r["text"] for r in records[2]["replies"]], [prototype.ASK_INSTEAD_LINE])
        self.assertTrue(records[2]["replies"][0]["fixed"], "the record says the line was fixed, not composed")

    async def test_without_a_clip_the_fixed_line_is_silent(self):
        records, _ = await self.call(*DEMO, "That's not what I meant.", ask_pcm=b"")
        self.assertEqual(self.asked[2], 0, "no other voice and no composed reply stands in for the clip")
        self.assertEqual(self.spoken[2], [], "nothing is played")
        self.assertEqual([r["text"] for r in records[2]["replies"]], [prototype.ASK_INSTEAD_LINE],
                         "the record still says which line the turn meant")

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


class FollowUpTest(Call):
    """A follow-up that names no place runs the real lookup for the place of the task it continues. The replayed
    registry has no default place, so a place can only come from the call."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.tools, self.fetch, _ = replay_registry(None)

    def lookup(self, record):
        return [(e["tool"]["tool"], e["tool"]["arguments"], e["tool"]["outcome"]) if e["tool"] else None
                for e in record["executions"].values()]

    async def test_a_follow_up_with_no_place_inherits_it_and_runs_the_real_tool(self):
        records, items = await self.call(*DEMO, tools=self.tools)
        self.assertEqual(self.lookup(records[0]), [("weather", {"place": "Denver", "when": "tomorrow"}, "ok")])
        self.assertEqual(self.lookup(records[1]), [("weather", {"place": "Denver", "when": "saturday"}, "ok")],
                         "turn 2 asks about Saturday in turn 1's place, and the real lookup answers it")
        result = records[1]["executions"]["task_1"]["result_summary"]
        self.assertTrue(result.startswith("Forecast for Denver, Colorado. Saturday"), result)
        self.assert_no_item_holds("And what about Saturday?", items[1])

    async def test_an_amended_correction_inherits_the_place(self):
        records, items = await self.call(*DEMO, "No, Sunday.", tools=self.tools)
        self.assertEqual(self.lookup(records[2]), [("weather", {"place": "Denver", "when": "sunday"}, "ok")],
                         "the amended task keeps the place and changes only the day")
        self.assert_no_item_holds("No, Sunday.", items[2])

    async def test_a_correction_that_names_the_rejected_place_runs_the_new_one(self):
        records, _ = await self.call("What time is it in Tokyo?", "No, not Tokyo, Denver.", tools=self.tools)
        self.assertEqual(self.lookup(records[1]), [("time", {"place": "Denver"}, "ok")],
                         "the corrected task runs again, in the place the correction states")

    async def test_with_no_earlier_place_a_follow_up_stays_empty(self):
        records, _ = await self.call("What's the weather tomorrow?", "And what about Saturday?", tools=self.tools)
        self.assertEqual(self.lookup(records[0]), [("weather", {"place": None, "when": "tomorrow"}, "failed")],
                         "the fixture's turn 1 resolves no place")
        self.assertEqual(self.lookup(records[1]), [None], "turn 2 is not given a place nobody said")
        self.assertEqual(self.fetch.urls, [], "no place is looked up at all")


class ThirdPersonTest(unittest.IsolatedAsyncioTestCase):
    """A reply that talks about the person instead of to them is spoken and logged, never asked again."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(isolate_decider_cache(Path(self.tmp.name) / "decider-cache", 20))
        original = prototype.Session.open

        async def open_session(url, name, clock, on_event=None):
            if name == "voice":
                return self.voice
            return await FakeSession.open(url, name, clock, on_event)

        prototype.Session.open = open_session
        self.addCleanup(setattr, prototype.Session, "open", original)
        self.voice = await Scripted.open("ws://unused", "voice", lambda: CLOCK)
        self.spoken = []

    async def turn(self, script):
        self.voice.script, self.voice.asked = list(script), 0
        return await prototype.run_clip(
            Source(), "c", False, "ws://unused", None, 0.0, Path(self.tmp.name), {}, "hybrid", None, "gated",
            voice=self.voice, snapshot_mode="reply-no-utterance",
            speak=heard_by(self.spoken))

    async def test_the_user_is_spoken_and_logged(self):
        record = await self.turn(["The user wants the weather in Denver.", "I'm checking the weather in Denver."])
        self.assertEqual(self.voice.asked, 1, "one reply, with no re-ask")
        self.assertEqual(self.spoken, ["The user wants the weather in Denver."])
        check = next(e for e in record["timeline"] if e["kind"] == "fact_check")
        self.assertEqual(check["third_person"], ["the user"])

    async def test_a_reply_to_the_person_passes(self):
        record = await self.turn(["I'm checking the weather in Denver for you."])
        self.assertEqual(self.voice.asked, 1)
        self.assertEqual(self.spoken, ["I'm checking the weather in Denver for you."])
        self.assertEqual([e for e in record["timeline"] if e["kind"] == "fact_check"], [])


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
