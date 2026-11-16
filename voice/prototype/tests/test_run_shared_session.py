"""The prototype hook's side of live mode's one-session-per-call: a caller's voice session is added to, not replaced.

The voice session is the only thing a live call shares between turns. These tests pin the three things that making it
shared costs: the session is not reopened, the settings are sent once, and the session is not closed at the end of a
turn. They stand in for llama.cpp, so they say nothing about what the model remembers.
"""

import asyncio
import base64
import tempfile
import unittest
from pathlib import Path

from voice.prototype import run as prototype
from voice.prototype.segmenter import use_decider_cache
from voice.prototype.transcript import COMPLETED

CLOCK = 0.0


class FakeSession:
    """A realtime session that answers the few requests run_clip makes, in memory."""

    def __init__(self, name, on_event):
        self.name, self.on_event = name, on_event
        self.events, self.sent, self.audio, self.configs = [], [], [], []
        self.closed, self._changed = False, asyncio.Condition()

    @classmethod
    async def open(cls, url, name, clock, on_event=None):
        session = cls(name, on_event)
        await session.push({"type": "session.created",
                            "session": {"audio": {"output": {"format": {"rate": 24_000}}}}})
        return session

    async def push(self, event):
        event["_t"] = CLOCK
        if event.get("type") == "response.output_audio.delta":
            self.audio.append((len(self.events), base64.b64decode(event["delta"])))
        self.events.append(event)
        if self.on_event is not None:
            self.on_event(event)
        async with self._changed:
            self._changed.notify_all()

    async def send(self, **event):
        self.sent.append(event)
        kind = event.get("type")
        if kind == "session.update":
            self.configs.append(event["session"])
            await self.push({"type": "session.updated"})
        elif kind == "conversation.item.create":
            await self.push({"type": "conversation.item.added", "item": event["item"]})
        elif kind == "input_audio_buffer.commit" and self.name == "asr":
            await self.push({"type": COMPLETED, "item_id": "item_1", "transcript": "what's the weather in Denver"})
        elif kind == "response.create":
            await self.push({"type": "response.output_text.delta", "delta": "Sunny."})
            await self.push({"type": "response.output_audio.delta",
                             "delta": base64.b64encode(b"\x01\x00" * 480).decode()})
            await self.push({"type": "response.done", "response": {"status": "completed", "output": []}})

    async def wait_for(self, *types, after=0, timeout=120):
        async def find():
            async with self._changed:
                while True:
                    hit = next((e for e in self.events[after:] if e.get("type") in types), None)
                    if hit is not None:
                        return hit
                    await self._changed.wait()
        return await asyncio.wait_for(find(), timeout)

    def clock(self):
        return CLOCK

    async def stream(self, pcm, rate):
        return CLOCK, CLOCK

    async def close(self):
        self.closed = True


class Source:
    """One turn's microphone audio, already streamed."""

    name = stem = "turn_001"
    rate, audio_s = 16_000, 0.0

    async def stream(self, voice, asr):
        return CLOCK


class Streams(Source):
    """A turn whose audio really goes to the voice session, so where an item lands relative to it is observable."""

    async def stream(self, voice, asr):
        await voice.send(type="input_audio_buffer.append", audio="AA==")
        return CLOCK


class SnapshotPlacementTest(unittest.IsolatedAsyncioTestCase):
    """Where --snapshot puts the one user item that carries the task state, against the turn's own audio."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        use_decider_cache(Path(self.tmp.name) / "decider-cache", 20)
        self.original_open = prototype.Session.open

        async def open_session(url, name, clock, on_event=None):
            return await FakeSession.open(url, name, clock, on_event)

        prototype.Session.open = open_session
        self.addCleanup(setattr, prototype.Session, "open", self.original_open)

    def voice_sent(self, voice):
        return [(index, e) for index, e in enumerate(voice.sent)
                if e.get("type") in ("conversation.item.create", "input_audio_buffer.append")]

    async def turn(self, voice, **snapshot):
        return await prototype.run_clip(Streams(), "c", False, "ws://unused", None, 0.0, Path(self.tmp.name), {},
                                        "hybrid", None, "gated", voice=voice, **snapshot)

    async def test_the_shipped_snapshot_is_the_newest_item_before_the_reply(self):
        voice = await FakeSession.open("ws://unused", "voice", lambda: CLOCK)
        record = await self.turn(voice)
        sent = self.voice_sent(voice)
        self.assertEqual([e["type"] for _, e in sent], ["input_audio_buffer.append", "conversation.item.create"],
                         "the shipped mode puts the snapshot after the audio, as the newest user item")
        text = sent[1][1]["item"]["content"][0]["text"]
        self.assertIn(record["final_transcript"], text, "it carries the turn's own transcript")

    async def test_before_audio_sends_the_prior_state_ahead_of_the_turn_audio(self):
        voice = await FakeSession.open("ws://unused", "voice", lambda: CLOCK)
        record = await self.turn(voice, snapshot_mode="before-audio",
                                 prior_snapshot={"final_transcript": "earlier words", "tasks": []})
        sent = self.voice_sent(voice)
        self.assertEqual([e["type"] for _, e in sent], ["conversation.item.create", "input_audio_buffer.append"],
                         "the snapshot goes in before the audio, so it is not the newest user item")
        text = sent[0][1]["item"]["content"][0]["text"]
        self.assertIn("earlier words", text)
        self.assertNotIn(record["final_transcript"], text, "the turn's own words reach the model as audio only")

    async def test_before_audio_sends_nothing_after_the_tools_turn(self):
        voice = await FakeSession.open("ws://unused", "voice", lambda: CLOCK)
        await self.turn(voice, snapshot_mode="before-audio", prior_snapshot={"final_transcript": "earlier words"})
        items = [e for _, e in self.voice_sent(voice) if e["type"] == "conversation.item.create"]
        self.assertEqual(len(items), 1, "one item, moved, not a second one added")

    async def test_off_sends_no_machine_text_at_all(self):
        voice = await FakeSession.open("ws://unused", "voice", lambda: CLOCK)
        await self.turn(voice, snapshot_mode="off", prior_snapshot={"final_transcript": "earlier words"})
        items = [e for _, e in self.voice_sent(voice) if e["type"] == "conversation.item.create"]
        self.assertEqual(items, [], "no snapshot, and no task update either")

    async def test_reply_no_utterance_keeps_the_task_list_and_drops_their_words(self):
        voice = await FakeSession.open("ws://unused", "voice", lambda: CLOCK)
        record = await self.turn(voice, snapshot_mode="reply-no-utterance")
        texts = [e["item"]["content"][0]["text"] for _, e in self.voice_sent(voice)
                 if e["type"] == "conversation.item.create"]
        self.assertTrue(texts, "the shipped place, one item before the reply")
        for text in texts:
            self.assertNotIn(record["final_transcript"].lower(), text.lower(),
                             "no copy of the person's words in any item the model reads")
        self.assertIn("[task service", texts[0], "the machine prefix stays")
        self.assertIn('"tasks"', texts[0], "the item keeps its shape, task list included")

    def test_without_utterance_drops_every_copy_at_any_depth(self):
        transcript = "What did I just ask you?"
        snapshot = {"type": "utterance_snapshot", "final_transcript": transcript,
                    "unreviewed_speech": transcript, "clarification_needed": None,
                    "tasks": [{"task_id": "task_1", "description": transcript, "status": "running"},
                              {"task_id": "task_2", "description": "get the weather in Denver", "status": "completed",
                               "result_summary": "High of 69 F."}]}
        clean = prototype.without_utterance(snapshot, transcript)
        self.assertNotIn("final_transcript", clean, "the turn's own transcript goes")
        self.assertNotIn("unreviewed_speech", clean, "and the second copy of it goes")
        self.assertEqual(clean["tasks"][0], {"task_id": "task_1", "status": "running"},
                         "a description that is the utterance goes, the task and its status stay")
        self.assertEqual(clean["tasks"][1]["description"], "get the weather in Denver",
                         "a description that is not the utterance stays")
        self.assertEqual(clean["tasks"][1]["result_summary"], "High of 69 F.")
        self.assertEqual(clean["type"], "utterance_snapshot", "the shape of the item is unchanged")


class SharedVoiceSessionTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        use_decider_cache(Path(self.tmp.name) / "decider-cache", 20)
        self.opened = []
        self.original_open = prototype.Session.open

        async def open_session(url, name, clock, on_event=None):
            session = await FakeSession.open(url, name, clock, on_event)
            self.opened.append(session)
            return session

        prototype.Session.open = open_session
        self.addCleanup(setattr, prototype.Session, "open", self.original_open)

    async def turn(self, voice):
        return await prototype.run_clip(Source(), "c", False, "ws://unused", None, 0.0, Path(self.tmp.name), {},
                                        "hybrid", None, "gated", voice=voice)

    async def test_a_callers_session_is_added_to_and_left_open(self):
        voice = await prototype.Session.open("ws://unused", "voice", lambda: CLOCK)
        first = await self.turn(voice)
        second = await self.turn(voice)
        self.assertEqual([s.name for s in self.opened], ["voice", "asr", "tools", "asr", "tools"],
                         "one voice session for the call, and a fresh ASR and tools session per turn")
        self.assertEqual(len(voice.configs), 1, "the settings go once, so a later turn cannot reset the conversation")
        self.assertIn("instructions", voice.configs[0])
        self.assertFalse(voice.closed, "the caller keeps the session, so the turn must not close it")
        self.assertEqual(second["final_transcript"], "what's the weather in Denver")
        self.assertEqual([r["text"] for r in second["replies"]], ["Sunny."])
        self.assertEqual([r["text"] for r in first["replies"]], ["Sunny."])

    async def test_a_turns_record_holds_only_that_turns_voice_events(self):
        voice = await prototype.Session.open("ws://unused", "voice", lambda: CLOCK)
        first = await self.turn(voice)
        second = await self.turn(voice)
        for record in (first, second):
            self.assertEqual([e["type"] for e in record["events"]["voice"]].count("response.done"), 1,
                             "a turn's record is not the whole call's event list")
            self.assertNotIn("session.created", [e["type"] for e in record["events"]["voice"]],
                             "the caller opened this session before the turn")
        self.assertNotIn("session.updated", [e["type"] for e in second["events"]["voice"]],
                         "the settings went out on the first turn only")
        self.assertIn("conversation.item.added", [e["type"] for e in second["events"]["voice"]])

    async def test_a_turn_of_its_own_session_still_closes_it(self):
        record = await self.turn(None)
        voice = next(s for s in self.opened if s.name == "voice")
        self.assertTrue(voice.closed, "a turn that opened the session still closes it, as the prototype needs")
        self.assertEqual([r["text"] for r in record["replies"]], ["Sunny."])


if __name__ == "__main__":
    unittest.main()
