"""A turn over a task that has started opens with one fixed line, and the model is asked for nothing else.

Measured cost before this line existed (evidence live/memory-20261116/ablation/demo-follow-up, 2026-11-16): the model
composed "I'm running the task to get the weather in Denver tomorrow." and spoke it for 3.44 s, and it was the last
place on the turn where a day, a place or a number could reach the speaker before any tool returned one. These tests
say what the turn speaks, what it asks, and what the task update still carries. They say nothing about what the model
would have said.
"""

import base64
import json
import tempfile
import time
import unittest
from pathlib import Path

from voice.live.facts import names_machinery, ungrounded
from voice.prototype import run as prototype
from voice.prototype.segmenter import use_decider_cache
from voice.prototype.tests.test_corrections import ASK_PCM, DEMO, DemoSession, Turn
from voice.prototype.tests.test_run_shared_session import CLOCK, FakeSession
from voice.tools import registry
from voice.tools.tests.replay import ReplayFetch, ReplayMcp

OPENER_PCM = b"\x03\x00" * 480
CHIT_CHAT = "Thanks, that's all."
WEATHER = "What's the weather in Denver tomorrow?"
LOOKUP_S = 0.05


class Slow(ReplayFetch):
    """The recorded answer, after a delay, so the task is still running when the turn reads its snapshot, as on the
    demo run, and its forecast arrives as a task update a moment later."""

    def __call__(self, url, timeout_s):
        time.sleep(LOOKUP_S)
        return super().__call__(url, timeout_s)


def recorded_tools():
    return registry.build(Slow(), ReplayMcp(), None, 5.0)


class Voice(FakeSession):
    """The voice model answers a task update with the facts that update carries, as LFM3 did on the demo run, and
    anything else with a line that names nothing."""

    def __init__(self, name, on_event):
        super().__init__(name, on_event)
        self.new_items = []

    async def send(self, **event):
        if event.get("type") == "conversation.item.create":
            self.new_items.append(event["item"])
        if event.get("type") != "response.create":
            return await super().send(**event)
        self.sent.append(event)
        text, self.new_items = self.facts(), []
        await self.push({"type": "response.output_text.delta", "delta": text})
        await self.push({"type": "response.output_audio.delta",
                         "delta": base64.b64encode(b"\x01\x00" * 480).decode()})
        await self.push({"type": "response.done", "response": {"status": "completed", "output": []}})

    def facts(self):
        """The facts of the newest task update this turn was given, or a line that names nothing."""
        for item in reversed(self.new_items):
            text = "".join(part.get("text", "") for part in item.get("content") or ())
            if text.startswith(prototype.UPDATE_PREFIX):
                return json.loads(text.split("] ", 1)[1])["updates"][0]["result_summary"]
        return "Sunny."


class Recording(DemoSession):
    """The tools session delegates the unreviewed speech, as LFM3 did on every turn of the demo, and notes how many
    requests the voice session had taken when the tools turn began."""

    voice, at_tools = None, []

    async def send(self, **event):
        if self.name == "tools" and event.get("type") == "response.create":
            Recording.at_tools.append(sum(e.get("type") == "response.create" for e in Recording.voice.sent))
        return await super().send(**event)


class Quiet(DemoSession):
    """The tools session decides nothing needs doing: a chit-chat turn."""

    async def send(self, **event):
        if self.name == "tools" and event.get("type") == "response.create":
            self.sent.append(event)
            await self.push({"type": "response.done", "response": {"status": "completed", "output": []}})
            return
        await super().send(**event)


class Call(unittest.IsolatedAsyncioTestCase):
    """One call, one voice session, one turn per utterance, carrying what the live server carries between turns."""

    tools_session = Recording

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        use_decider_cache(Path(self.tmp.name) / "decider-cache", 20)
        original = prototype.Session.open

        async def open_session(url, name, clock, on_event=None):
            return await (Voice if name == "voice" else self.tools_session).open(url, name, clock, on_event)

        prototype.Session.open = open_session
        self.addCleanup(setattr, prototype.Session, "open", original)
        Recording.at_tools = []

    async def call(self, *said, opener_pcm=OPENER_PCM):
        """`said` is one utterance per turn. Returns each turn's record, what it spoke, and how many requests the
        voice session took. `Recording.at_tools` accumulates one entry per tools turn, across the call."""
        self.voice = await Voice.open("ws://unused", "voice", lambda: CLOCK)
        tools = recorded_tools()
        Recording.voice = self.voice
        records, carried, spoken, asked = [], [], [], []
        for n, words in enumerate(said, 1):
            DemoSession.heard = words
            mark, turn_spoken = len(self.voice.sent), []
            record = await prototype.run_clip(
                Turn(n), "c", False, "ws://unused", None, 0.0, Path(self.tmp.name), {}, "hybrid", tools, "gated",
                voice=self.voice, snapshot_mode="reply-no-utterance", opener_pcm=opener_pcm, ask_pcm=ASK_PCM,
                speak=lambda text, pcm: turn_spoken.append((text, len(pcm))),
                prior_utterances=tuple(r["final_transcript"] for r in records), prior_tasks=tuple(carried))
            carried += prototype.carried_tasks(record)
            spoken.append(turn_spoken)
            asked.append(sum(e.get("type") == "response.create" for e in self.voice.sent[mark:]))
            records.append(record)
        return records, spoken, asked


class RunningTaskTest(Call):
    async def test_a_running_task_opens_with_the_fixed_line_and_asks_the_model_nothing_for_it(self):
        records, spoken, asked = await self.call(WEATHER)
        self.assertEqual(spoken[0][0], (prototype.OPENER_LINE, len(OPENER_PCM)), "the opener is the turn's first sound")
        self.assertEqual(asked, [1], "one request, the task update's: the opener costs the model nothing")
        self.assertEqual(Recording.at_tools, [0], "and no request runs before the tools turn")
        self.assertEqual([r["kind"] for r in records[0]["replies"]], ["snapshot", "update"])
        self.assertEqual(records[0]["replies"][0]["text"], prototype.OPENER_LINE)
        self.assertTrue(records[0]["replies"][0]["fixed"], "the record says the line was fixed, not composed")
        self.assertFalse(records[0]["replies"][1].get("fixed"), "the task update is still the model's")

    async def test_the_update_reply_still_states_the_forecast_facts(self):
        records, spoken, _ = await self.call(WEATHER)
        update = records[0]["task_updates"][0]["result_summary"]
        self.assertTrue(update.startswith("Forecast for Denver, Colorado. Tuesday, November 17"), update)
        self.assertEqual(spoken[0], [(prototype.OPENER_LINE, len(OPENER_PCM)), (update, 960)],
                         "the opener, then the facts word for word")
        self.assertEqual([e for e in records[0]["timeline"] if e["kind"] == "fact_check"], [],
                         "the facts still pass the check")
        self.assertEqual([r["kind"] for r in records[0]["replies"]], ["snapshot", "update"])

    async def test_without_the_clip_the_turn_opens_with_nothing(self):
        records, spoken, asked = await self.call(WEATHER, opener_pcm=b"")
        self.assertEqual(spoken[0][0], (records[0]["task_updates"][0]["result_summary"], 960),
                         "no other voice stands in for the clip, and the facts still go out")
        self.assertEqual(records[0]["replies"][0]["text"], prototype.OPENER_LINE,
                         "the record still says which line the turn meant")
        self.assertEqual(asked, [1], "and the turn asks the model for nothing extra")

    async def test_a_follow_up_over_a_running_task_opens_the_same_way(self):
        _, spoken, asked = await self.call(*DEMO)
        self.assertEqual([turn[0] for turn in spoken],
                         [(prototype.OPENER_LINE, len(OPENER_PCM))] * 2, "both turns, not only the first")
        self.assertEqual(asked, [1, 1])

    async def test_a_correction_still_speaks_its_own_line_and_runs_no_task(self):
        records, spoken, asked = await self.call(*DEMO, "That's not what I meant.")
        self.assertEqual(spoken[2], [(prototype.ASK_INSTEAD_LINE, len(ASK_PCM))], "the correction's own line")
        self.assertEqual(asked[2], 0, "the voice model composes nothing on a bare correction")
        self.assertEqual(records[2]["dispatch_log"], [], "and no task runs")
        self.assertEqual([r["kind"] for r in records[2]["replies"]], ["snapshot"])


class ChitChatTest(Call):
    tools_session = Quiet

    async def test_a_chit_chat_turn_is_unchanged(self):
        records, spoken, asked = await self.call(CHIT_CHAT)
        self.assertEqual(spoken[0], [("Sunny.", 960)], "the model's own line, once, as before")
        self.assertEqual(asked, [1], "one request, the reply's")
        self.assertEqual([r["kind"] for r in records[0]["replies"]], ["snapshot"])
        self.assertFalse(records[0]["replies"][0].get("fixed"), "the reply is the model's, not the opener")
        self.assertEqual(records[0]["task_updates"], [])


class OpenerLineTest(unittest.TestCase):
    def test_the_opener_is_under_four_words_and_names_nothing(self):
        self.assertLess(len(prototype.OPENER_LINE.split()), 4, "under four words")
        self.assertEqual(ungrounded(prototype.OPENER_LINE, []), [], "it names no day, date, place or number")
        self.assertEqual(names_machinery(prototype.OPENER_LINE), [], "and no machinery")


if __name__ == "__main__":
    unittest.main()
