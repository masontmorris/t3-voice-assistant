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
