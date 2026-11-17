"""Streamed release: reply audio reaches the speaker while the reply is generated, never ahead of the screens.

The fake session answers like LFM3-Audio: a run of text deltas, then that block's audio frames, then the next run.
Every frame holds its block's number, so a test reads back exactly which blocks the speaker got. These stand in for
llama.cpp: they pin what the turn releases and asks, not what the model says.
"""

import asyncio
import base64
import unittest

import numpy as np

from voice.prototype import run as prototype
from voice.prototype.release import Release, whole_words
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


class ReleaseGateTest(unittest.TestCase):
    """The gate on its own, fed one response's deltas."""

    def feed(self, runs, passes, finish=True):
        out = []
        gate = Release(passes, lambda text, pcm: out.append((text, blocks_in(pcm))))
        for block, run in enumerate(runs):
            for word in run:
                gate.on_text(word)
            for _ in range(2):
                gate.on_audio(frame(block), float(block))
        if finish:
            gate.finish(9.0)
        return gate, out

    def test_a_block_is_released_once_the_next_runs_text_has_passed(self):
        gate, out = self.feed([["It's", " sunny"], [" in", " Denver"], [" today", "."]], lambda t, f: True,
                              finish=False)
        self.assertEqual([blocks for _, blocks in out], [[0], [1]], "block 2 waits for a run that never came")
        self.assertEqual(out[0][0], "It's sunny in ", "block 0 went out on the whole words of run 1")

    def test_no_audio_for_text_that_has_not_passed(self):
        screened = []

        def passes(text, final):
            screened.append(text)
            return "Sunday" not in text

        gate, out = self.feed([["I'm", " checking"], [" Denver", " for"], [" Sunday", " now"], [" okay", "."]],
                              passes)
        self.assertEqual([b for _, blocks in out for b in blocks], [0], "only block 0, screened with run 1 after it")
        self.assertFalse(gate.passed)
        self.assertEqual(gate.heard(), "I'm checking")
        for text, blocks in out:
            self.assertNotIn("Sunday", text)

    def test_a_word_still_arriving_is_not_screened(self):
        screened = []
        self.feed([["The", " weather", " in", " Ral"], ["eigh", " today"], ["."]],
                  lambda t, f: screened.append(t) or True)
        self.assertNotIn("Ral", " ".join(screened[:1]), "the half word stays out of the first screen")
        self.assertEqual(whole_words("The weather in Ral"), "The weather in ")

    def test_a_prefix_that_fails_and_then_passes_releases_again(self):
        gate, out = self.feed([["It's", " sunny"], [" in", " New", " "], ["York", " and"], [" warm", "."]],
                              lambda t, f: not t.rstrip().endswith("New"))
        self.assertEqual([blocks for _, blocks in out], [[0, 1], [2], [3]], "held at 'in New', released at 'York'")

    def test_a_whole_reply_that_fails_releases_nothing_more(self):
        gate, out = self.feed([["Sure", "."]], lambda t, f: not f)
        self.assertEqual(out, [])
        self.assertEqual(gate.heard(), "")


class StreamedAnswerTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.voice = await Blocks.open("ws://unused", "voice", lambda: CLOCK)
        self.speaker, self.timeline = Speaker(self.voice), []

    async def answer(self, *script, **more):
        self.voice.script = [list(map(list, runs)) for runs in script]
        return await prototype.answer(self.voice, 24_000, CLOCK, [PERSON], lambda kind, **d: self.timeline.append(
            {"kind": kind, **d}), speak=self.speaker, **more)

    def notes(self):
        return [event["item"]["content"][0]["text"] for event in self.voice.sent
                if event.get("type") == "conversation.item.create"]

    def asks(self):
        return sum(event.get("type") == "response.create" for event in self.voice.sent)

    async def test_audio_reaches_the_speaker_before_the_reply_is_done(self):
        reply, pcm = await self.answer([["It's", " sunny"], [" in", " Denver"], [" tomorrow", "."]])
        first = self.speaker.calls[0]
        self.assertEqual(first["responses_done"], 0, "the first block went out while the reply was generating")
        self.assertEqual(self.speaker.blocks(), [0, 1, 2])
        self.assertEqual([call["done"] for call in self.speaker.calls], [False, False, False, True])
        self.assertEqual(reply["text"], "It's sunny in Denver tomorrow.")
        self.assertEqual(blocks_in(pcm), [0, 1, 2])

    async def test_a_late_failure_stops_at_the_last_boundary_that_passed_and_goes_on_without_repeating(self):
        reply, pcm = await self.answer(
            [["It's", " sunny"], [" in", " Denver"], [" tomorrow", " and"], [" on", " Sunday"], [" too", "."]],
            [[" I", " don't"], [" have", " the"], [" rest", " yet."]])
        self.assertEqual(self.asks(), 2, "asked again once")
        note, = self.notes()
        self.assertIn('they heard only the start of your last reply, "It\'s sunny in Denver tomorrow and"', note)
        self.assertIn("nothing gave you Sunday", note)
        self.assertIn("without saying again what they heard", note)
        first_blocks = [b for call in self.speaker.calls if call["responses_done"] == 0 for b in call["blocks"]]
        self.assertEqual(first_blocks, [0, 1, 2], "block 3, whose text runs into Sunday, never went out")
        self.assertEqual(reply["text"], "It's sunny in Denver tomorrow and I don't have the rest yet.")
        self.assertNotIn("Sunday", " ".join(call["text"] for call in self.speaker.calls))
        self.assertEqual([call["done"] for call in self.speaker.calls].count(True), 1, "one reply, ended once")
        stop, = [e for e in self.timeline if e["kind"] == "fact_check"]
        self.assertEqual(stop["heard"], "It's sunny in Denver tomorrow and")

    async def test_the_continuation_quotes_what_the_released_audio_said_not_what_it_was_released_on(self):
        """The audio lags the text it is released on (measured: 7 words at the median), so the note quotes the
        transcript of the released audio, matched back to the reply's own words."""
        heard_audio = []

        async def transcribe(pcm):
            heard_audio.append(blocks_in(pcm))
            return "it's sunny in"

        await self.answer(
            [["It's", " sunny"], [" in", " Denver"], [" tomorrow", " and"], [" on", " Sunday"], [" too", "."]],
            [[" Denver", " has"], [" no", " more"], [" yet", "."]], transcribe=transcribe)
        self.assertEqual(heard_audio, [[0, 1, 2]], "the transcript is of the audio that went out")
        note, = self.notes()
        self.assertIn('they heard only the start of your last reply, "It\'s sunny in"', note)

    def test_heard_prefix_is_never_past_the_last_word_matched(self):
        self.assertEqual(prototype.heard_prefix("The weather in Raleigh today is a high of 57 degrees.",
                                                "the weather in raleigh today"), "The weather in Raleigh today")
        self.assertEqual(prototype.heard_prefix("It's sunny.", ""), "")

    async def test_a_continuation_that_says_the_heard_part_again_is_not_spoken(self):
        reply, _ = await self.answer(
            [["It's", " sunny"], [" in", " Denver"], [" on", " Sunday"], [" too", "."]],
            [["It's", " sunny"], [" in", " Denver"], [" tomorrow", "."]])
        self.assertEqual(self.asks(), 2, "no third ask")
        self.assertEqual(reply["text"], "It's sunny in Denver", "only what the first reply released is heard")
        self.assertEqual(reply["blocked"]["restated"], ["it", "is", "sunny", "in"])
        self.assertEqual(self.speaker.calls[-1]["done"], True)

    async def test_a_reply_refused_before_any_audio_is_asked_again_from_the_top(self):
        reply, _ = await self.answer([["The", " user"], [" wants", " Sunday"], ["."]],
                                     [["It's", " sunny"], [" tomorrow", "."]],
                                     retry_item=lambda found, repeats=False: prototype.plain_text(prototype.RETRY_NOTE))
        self.assertEqual(self.notes(), [prototype.RETRY_NOTE], "nothing was heard, so there is nothing to continue")
        self.assertEqual(reply["text"], "It's sunny tomorrow.")
        self.assertEqual(self.speaker.blocks(), [0, 1], "only the retry's blocks were spoken")

    async def test_the_fixed_line_plays_only_when_the_turn_said_nothing(self):
        fallback = prototype.Fallback(frame(7))
        reply, _ = await self.answer([["It's", " sunny"], [" in", " Denver"], [" on", " Sunday"], ["."]],
                                     [["On", " Sunday"], ["."]], fallback=fallback)
        self.assertNotIn(prototype.FALLBACK_LINE, reply["text"])
        self.assertFalse(fallback.spoken)
        self.voice.sent.clear()
        self.speaker.calls.clear()
        reply, _ = await self.answer([["Sunday", "."]], [["Sunday", " again."]], fallback=fallback)
        self.assertEqual(reply["text"], prototype.FALLBACK_LINE)
        self.assertEqual(self.speaker.blocks(), [7])

    STALE = "Hello! I'm checking the weather in a city on the East Coast. Which one?"

    async def test_a_stale_repeat_releases_nothing_before_it_is_asked_again(self):
        """The stale line of tool-pick arm 1, generated block by block: every prefix could still be the old line,
        so none of it is released, and the retry is told it repeated itself."""
        reply, _ = await self.answer(
            [["Hello!", " I'm"], [" checking", " the"], [" weather", " in"], [" a", " city"], [" on", " the"],
             [" East", " Coast."], [" Which", " one?"]],
            [["I", " can"], [" hear", " you"], [" fine", "."]],
            previous=(self.STALE,),
            retry_item=lambda found, repeats=False: prototype.plain_text(
                prototype.REPEAT_NOTE if repeats else prototype.RETRY_NOTE))
        self.assertEqual([call for call in self.speaker.calls if call["responses_done"] == 0], [],
                         "nothing of the stale line reached the speaker")
        self.assertEqual(self.notes(), [prototype.REPEAT_NOTE])
        self.assertEqual(reply["text"], "I can hear you fine.")

    async def test_a_reply_that_opens_like_an_earlier_line_streams_once_it_differs(self):
        reply, _ = await self.answer(
            [["Hello!", " I'm"], [" checking", " the"], [" news", " for"], [" you", " now"], ["."]],
            previous=(self.STALE,))
        held = self.speaker.calls[0]
        self.assertEqual(held["responses_done"], 0, "released while generating, once 'news' parted from the old line")
        self.assertEqual(held["blocks"], [0, 1])
        self.assertEqual(reply["text"], "Hello! I'm checking the news for you now.")


if __name__ == "__main__":
    unittest.main()
