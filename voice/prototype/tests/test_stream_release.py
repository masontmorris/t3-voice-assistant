"""Streamed reply: reply audio reaches the speaker the moment the model makes it, and the fact check never holds it.

The fake session answers like LFM3-Audio: a run of text deltas, then that block's audio frames, then the next run.
Every frame holds its block's number, so a test reads back exactly which blocks the speaker got. These stand in for
llama.cpp: they pin what the turn sends and asks, not what the model says.
"""

import asyncio
import base64
import unittest

import numpy as np

from voice.prototype import run as prototype
from voice.prototype.tests.test_run_shared_session import CLOCK, FakeSession

FRAME_BYTES = 480
PERSON = "What's the weather in Denver tomorrow?"


def frame(block):
    return np.full(FRAME_BYTES // 2, block + 1, dtype="<i2").tobytes()


def blocks_in(pcm):
    return sorted(set(int(v) - 1 for v in np.frombuffer(pcm, dtype="<i2")))


class Blocks(FakeSession):
    """A voice session that answers each `response.create` with the next reply in `script`, a list of text runs,
    one event at a time in the background, as llama.cpp streams it."""

    script = []

    async def send(self, **event):
        if event.get("type") != "response.create":
            await super().send(**event)
            return
        self.sent.append(event)
        self.streaming = asyncio.create_task(self.generate(self.script.pop(0)))

    async def generate(self, runs):
        for block, run in enumerate(runs):
            for word in run:
                await self.push({"type": "response.output_text.delta", "delta": word})
                await asyncio.sleep(0)
            for _ in range(2):
                await self.push({"type": "response.output_audio.delta",
                                 "delta": base64.b64encode(frame(block)).decode()})
                await asyncio.sleep(0)
        await self.push({"type": "response.done", "response": {"status": "completed", "output": []}})


class Speaker:
    """Records every `speak` call: the text, the blocks in its audio, whether it ended the reply, and how many
    response.done events the session had seen when it came."""

    def __init__(self, session):
        self.session, self.calls = session, []

    def __call__(self, text, pcm, done):
        finished = sum(e.get("type") == "response.done" for e in self.session.events)
        self.calls.append({"text": text, "blocks": blocks_in(pcm), "done": done, "responses_done": finished})

    def blocks(self):
        return [b for call in self.calls for b in call["blocks"]]


class StreamedAnswerTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.voice = await Blocks.open("ws://unused", "voice", lambda: CLOCK)
        self.speaker, self.timeline = Speaker(self.voice), []

    async def answer(self, *script, **more):
        self.voice.script = [list(map(list, runs)) for runs in script]
        return await prototype.answer(self.voice, 24_000, CLOCK, [PERSON], lambda kind, **d: self.timeline.append(
            {"kind": kind, **d}), speak=self.speaker, **more)

    def asks(self):
        return sum(event.get("type") == "response.create" for event in self.voice.sent)

    async def test_each_block_reaches_the_speaker_as_it_arrives_with_no_lookahead(self):
        reply, pcm = await self.answer([["It's", " sunny"], [" in", " Denver"], [" tomorrow", "."]])
        first = self.speaker.calls[0]
        self.assertEqual(first["responses_done"], 0, "the first block went out while the reply was generating")
        self.assertEqual(first["blocks"], [0], "the first block is out before the second block's text arrives")
        self.assertEqual(first["text"], "It's sunny", "with only the text that came before it")
        self.assertEqual(sorted(set(self.speaker.blocks())), [0, 1, 2])
        self.assertEqual([call["done"] for call in self.speaker.calls], [False, False, False, False, False, False, True])
        self.assertEqual(reply["text"], "It's sunny in Denver tomorrow.")
        self.assertEqual(blocks_in(pcm), [0, 1, 2])
        self.assertEqual(reply["speech_s"], len(pcm) / 2 / 24_000)

    async def test_fact_logging_never_stops_or_reasks_the_reply(self):
        reply, pcm = await self.answer(
            [["It's", " sunny"], [" in", " Denver"], [" tomorrow", " and"], [" on", " Sunday"], [" too", "."]],
            [[" I", " don't"], [" know", "."]])
        self.assertEqual(blocks_in(pcm), [0, 1, 2, 3, 4])
        self.assertEqual(reply["text"], "It's sunny in Denver tomorrow and on Sunday too.")
        self.assertEqual(self.asks(), 1)
        check, = [event for event in self.timeline if event["kind"] == "fact_check"]
        self.assertEqual(check["ungrounded"], ["weekday Sunday"])

    async def test_a_place_the_call_never_named_is_not_cut(self):
        """The 2026-11-18 call: the second reply was cut after "but I" because "Voice sandbox" read as a place."""
        reply, pcm = await self.answer([["I couldn't", " find a project"], [" matching that,", " but I"],
                                        [" can start a", " thread in Voice sandbox."]])
        self.assertEqual(blocks_in(pcm), [0, 1, 2])
        self.assertEqual(self.asks(), 1)
        self.assertEqual(reply["text"], "I couldn't find a project matching that, but I can start a thread in Voice sandbox.")

    async def test_a_reply_with_no_audio_never_calls_the_speaker(self):
        self.voice.script = [[]]
        await prototype.answer(self.voice, 24_000, CLOCK, [PERSON], lambda kind, **d: None, speak=self.speaker)
        self.assertEqual(self.speaker.calls, [])


if __name__ == "__main__":
    unittest.main()
