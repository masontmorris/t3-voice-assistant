"""The prototype hook's side of live mode's one-session-per-call: a caller's voice session is added to, not replaced.

The voice session is the only thing a live call shares between turns. These tests pin the three things that making it
shared costs: the session is not reopened, the settings are sent once, and the session is not closed at the end of a
turn. They stand in for llama.cpp, so they say nothing about what the model remembers.
"""

import asyncio
import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from voice.prototype import run as prototype
from voice.live.facts import names_machinery, ungrounded
from voice.prototype.segmenter import use_decider_cache
from voice.prototype.transcript import COMPLETED

CLOCK = 0.0


REFUSAL = "session.update: instructions cannot change after the first audio or message"


def heard_by(into, with_bytes=False):
    """A `speak` that records each reply once it ends: its text, and with `with_bytes` how many bytes it released."""
    pending = bytearray()

    def speak(text, pcm, done):
        pending.extend(pcm)
        if done:
            into.append((text, len(pending)) if with_bytes else text)
            pending.clear()
    return speak


class FakeSession:
    """A realtime session that answers the few requests run_clip makes, in memory. Like llama.cpp, it refuses new
    instructions once it holds audio or a message."""

    transcript = "what's the weather in Denver"

    def __init__(self, name, on_event):
        self.name, self.on_event = name, on_event
        self.events, self.sent, self.audio, self.configs = [], [], [], []
        self.closed, self._changed = False, asyncio.Condition()
        self.instructions, self.holds_content = None, False

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
        if kind in ("conversation.item.create", "input_audio_buffer.append"):
            self.holds_content = True
        if kind == "session.update":
            instructions = event["session"].get("instructions", self.instructions)
            if self.holds_content and instructions != self.instructions:
                await self.push({"type": "error", "error": {"type": "invalid_request_error", "message": REFUSAL}})
                return
            self.configs.append(event["session"])
            self.instructions = instructions
            await self.push({"type": "session.updated"})
        elif kind == "conversation.item.create":
            await self.push({"type": "conversation.item.added", "item": event["item"]})
        elif kind == "input_audio_buffer.commit" and self.name == "asr":
            await self.push({"type": COMPLETED, "item_id": "item_1", "transcript": self.transcript})
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

    async def test_conversation_does_not_delegate_or_report_a_mock_task(self):
        voice = await FakeSession.open("ws://unused", "voice", lambda: CLOCK)
        with mock.patch.object(FakeSession, "transcript", "Hey, hey, can you hear me?"):
            record = await self.turn(voice)
        self.assertEqual(record["tasks"], [])
        self.assertEqual(record["task_updates"], [])
        self.assertIsNone(record["tool_turn"])
        self.assertEqual(len(record["replies"]), 1)

    async def test_failed_final_decider_does_not_bypass_gate_via_voice_tool(self):
        voice = await FakeSession.open("ws://unused", "voice", lambda: CLOCK)
        record = await self.turn(voice, final_decider=True)
        self.assertIsNone(record["tool_turn"])
        self.assertEqual(record["executions"], {})
        self.assertTrue(any(event["kind"] == "final_decision_error" for event in record["timeline"]))

    async def test_the_shipped_snapshot_is_the_newest_item_before_the_reply(self):
        voice = await FakeSession.open("ws://unused", "voice", lambda: CLOCK)
        record = await self.turn(voice)
        sent = self.voice_sent(voice)
        self.assertEqual([e["type"] for _, e in sent], ["input_audio_buffer.append", "conversation.item.create"],
                         "the shipped mode puts the snapshot after the audio, as the newest user item")
        text = sent[1][1]["item"]["content"][0]["text"]
        self.assertIn(record["final_transcript"], text, "it carries the turn's own transcript")

    async def test_the_snapshot_carries_the_earlier_turns_numbered_oldest_first(self):
        voice = await FakeSession.open("ws://unused", "voice", lambda: CLOCK)
        await self.turn(voice, prior_utterances=("first thing I asked", "second thing I asked"))
        sent = self.voice_sent(voice)
        self.assertEqual([e["type"] for _, e in sent], ["input_audio_buffer.append", "conversation.item.create"],
                         "one machine item after the audio, and it stays the newest user item")
        text = sent[1][1]["item"]["content"][0]["text"]
        self.assertIn("task service snapshot", text)
        self.assertLess(text.index("first thing I asked"), text.index("second thing I asked"))
        self.assertIn('"turn": 1', text)
        self.assertIn('"turn": 2', text)

    async def test_the_call_turns_keep_only_the_last_turns_and_say_how_many_it_dropped(self):
        voice = await FakeSession.open("ws://unused", "voice", lambda: CLOCK)
        turns = tuple(f"turn {i}" for i in range(prototype.HISTORY_TURNS + 2))
        await self.turn(voice, prior_utterances=turns)
        text = self.voice_sent(voice)[1][1]["item"]["content"][0]["text"]
        self.assertNotIn("turn 0", text)
        self.assertIn(f"turn {prototype.HISTORY_TURNS + 1}", text)
        self.assertIn('"omitted_turns": 2', text)
        self.assertIn('"turn": 3', text, "the numbers stay the call's own turn numbers")

    async def test_the_recall_note_is_the_newest_item_before_the_reply(self):
        voice = await FakeSession.open("ws://unused", "voice", lambda: CLOCK)
        prior = ("Hey, can you hear me?", "What time is it in Tokyo right now?",
                 "What's the weather in Denver tomorrow?")
        with mock.patch.object(FakeSession, "transcript", "What was the first thing I asked you?"):
            await self.turn(voice, prior_utterances=prior, recall="note")
        sent = self.voice_sent(voice)
        self.assertEqual([e["type"] for _, e in sent],
                         ["input_audio_buffer.append", "conversation.item.create", "conversation.item.create"],
                         "the note goes after the snapshot and after the audio")
        note = sent[2][1]["item"]["content"][0]["text"]
        self.assertIn("the first request was turn 2 of 3", note)
        self.assertIn("Tokyo", note)

    async def test_no_recall_note_when_the_question_is_not_about_the_call(self):
        voice = await FakeSession.open("ws://unused", "voice", lambda: CLOCK)
        await self.turn(voice, prior_utterances=("Hey, can you hear me?",), recall="note")
        self.assertEqual(len(self.voice_sent(voice)), 2, "only the audio and the snapshot item")

    async def test_the_snapshot_still_carries_the_call_turns_beside_the_note(self):
        voice = await FakeSession.open("ws://unused", "voice", lambda: CLOCK)
        prior = ("Hey, can you hear me?", "What time is it in Tokyo right now?")
        with mock.patch.object(FakeSession, "transcript", "What did I ask first?"):
            await self.turn(voice, prior_utterances=prior, recall="note")
        snapshot = self.voice_sent(voice)[1][1]["item"]["content"][0]["text"]
        self.assertIn('"call_turns"', snapshot)
        self.assertIn('"turn": 2', snapshot)

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

    async def test_the_task_update_item_loses_the_persons_words(self):
        voice = await FakeSession.open("ws://unused", "voice", lambda: CLOCK)
        with mock.patch.object(prototype.MockTaskService, "take_updates", return_value=[UPDATE]):
            await self.turn(voice, snapshot_mode="reply-no-utterance")
        texts = self.item_texts(voice)
        update = next(text for text in texts if text.startswith(prototype.UPDATE_PREFIX))
        self.assertNotIn("weather in denver", update.lower(), "the task's description is the person's request")
        self.assertIn("High of 69 F.", update, "the result the person asked for still reaches the model")
        self.assertIn('"status": "completed"', update)

    async def test_off_sends_no_task_update_either(self):
        voice = await FakeSession.open("ws://unused", "voice", lambda: CLOCK)
        with mock.patch.object(prototype.MockTaskService, "take_updates", return_value=[UPDATE]):
            record = await self.turn(voice, snapshot_mode="off")
        self.assertEqual(self.item_texts(voice), [], "a finished task does not bring machine text back in")
        self.assertEqual(record["task_updates"], [UPDATE], "the update existed, so the test could have seen it")

    async def test_off_holds_for_the_decider_turn_too(self):
        voice = await FakeSession.open("ws://unused", "voice", lambda: CLOCK)
        await prototype.run_clip(Streams(), "c", False, "ws://unused", None, 0.0, Path(self.tmp.name), {}, "hybrid",
                                 None, "decider", voice=voice, snapshot_mode="off")
        self.assertEqual(self.item_texts(voice), [], "--eou decider sends no snapshot under off")
        await prototype.run_clip(Streams(), "c", False, "ws://unused", None, 0.0, Path(self.tmp.name), {}, "hybrid",
                                 None, "decider", voice=voice)
        self.assertEqual(len(self.item_texts(voice)), 1, "and the same turn under reply sends one")

    def item_texts(self, voice):
        return [e["item"]["content"][0]["text"] for _, e in self.voice_sent(voice)
                if e["type"] == "conversation.item.create"]


UPDATE = {"task_id": "task_1", "description": "get the weather in Denver", "status": "completed",
          "result_summary": "High of 69 F."}


class WithoutUtteranceTest(unittest.TestCase):
    """The filter by itself. Each case is a leak measured in the live ablation runs or named in the review."""

    def test_the_persons_fields_go_whatever_they_hold(self):
        transcript = "What did I just ask you?"
        snapshot = {"type": "utterance_snapshot", "final_transcript": "something else entirely",
                    "unreviewed_speech": "you", "clarification_needed": ["which city do you mean"],
                    "tasks": [{"task_id": "task_1", "description": "get the weather in Denver", "status": "completed",
                               "result_summary": "High of 69 F.", "segment_text": "weather in Denver",
                               "instruction": "look it up"}],
                    "decider": {"read": [{"revision": 3, "text": "what time is it"}]}}
        clean = prototype.without_utterance(snapshot, transcript)
        self.assertEqual(clean, {"type": "utterance_snapshot",
                                 "tasks": [{"task_id": "task_1", "status": "completed",
                                            "result_summary": "High of 69 F."}],
                                 "decider": {"read": [{"revision": 3}]}})

    def test_a_list_element_that_repeats_them_goes(self):
        transcript = "What's the weather in Denver?"
        clean = prototype.without_utterance({"asked": ["What's the weather in Denver?", "High of 69 F."]}, transcript)
        self.assertEqual(clean, {"asked": ["High of 69 F."]})

    def test_a_fragment_of_a_two_request_turn_goes(self):
        transcript = "What's the weather in Denver and what time is it in Tokyo?"
        clean = prototype.without_utterance({"heard": "what time is it in Tokyo?", "summary": "Tokyo: 09:12."},
                                            transcript)
        self.assertEqual(clean, {"summary": "Tokyo: 09:12."})

    def test_a_near_copy_goes(self):
        clean = prototype.without_utterance({"heard": "What is the weather in Denver", "status": "running"},
                                            "What\N{RIGHT SINGLE QUOTATION MARK}s the  weather in DENVER?")
        self.assertEqual(clean, {"status": "running"})

    def test_a_single_word_they_happened_to_say_stays(self):
        clean = prototype.without_utterance({"status": "running", "place": "Denver"}, "I'm running late in Denver")
        self.assertEqual(clean, {"status": "running", "place": "Denver"})


class SessionOrderTest(unittest.IsolatedAsyncioTestCase):
    """llama.cpp refuses new instructions once a session holds a message, and the turn must say so."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        use_decider_cache(Path(self.tmp.name) / "decider-cache", 20)
        patch = mock.patch.object(prototype.Session, "open", FakeSession.open)
        patch.start()
        self.addCleanup(patch.stop)

    async def turn(self, voice, **kwargs):
        return await asyncio.wait_for(prototype.run_clip(
            Streams(), "c", False, "ws://unused", None, 0.0, Path(self.tmp.name), {}, "hybrid", None, "gated",
            voice=voice, **kwargs), 10)

    async def test_a_refused_update_ends_the_turn_with_the_servers_words(self):
        voice = await FakeSession.open("ws://unused", "voice", lambda: CLOCK)
        await voice.send(type="conversation.item.create", item=prototype.user_text("", {}))
        with self.assertRaises(prototype.SessionRefused) as refused:
            await self.turn(voice)
        self.assertIn(REFUSAL, str(refused.exception), "the record gets the reason, not a 120 s timeout")


class Scripted(FakeSession):
    """A voice session that answers each `response.create` with the next line in `script`, plus 20 ms of audio."""

    script = ["Sunny."]
    asked = 0

    async def send(self, **event):
        if event.get("type") == "response.create":
            self.sent.append(event)
            self.asked += 1
            text = self.script.pop(0) if self.script else "I'm still on it."
            await self.push({"type": "response.output_text.delta", "delta": text})
            await self.push({"type": "response.output_audio.delta",
                             "delta": base64.b64encode(b"\x01\x00" * 480).decode()})
            await self.push({"type": "response.done", "response": {"status": "completed", "output": []}})
            return
        await super().send(**event)


class Delegating(FakeSession):
    """The gated tool session: it hands the person's own words to the task service, as the demo's turn 2 did."""

    async def send(self, **event):
        if event.get("type") == "response.create":
            self.sent.append(event)
            call = {"type": "function_call", "name": "delegate", "call_id": "call_1",
                    "arguments": json.dumps({"instruction": "And what about Saturday?", "side_effect": False})}
            await self.push({"type": "response.done", "response": {"status": "completed", "output": [call]}})
            return
        await super().send(**event)


MACHINERY_WORDS = ("fact check", "snapshot", "machine note", "machine-generated", "task description", "task update",
                   "note", "lookup", "no answer yet")


def spelled_out(text):
    return [(word, text.lower().count(word)) for word in MACHINERY_WORDS if word in text.lower()]


class FactCheckTest(unittest.IsolatedAsyncioTestCase):
    """The three turns of the demo call, with the voice session scripted to answer as the model did on turn 3."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        use_decider_cache(Path(self.tmp.name) / "decider-cache", 20)
        self.original_open = prototype.Session.open
        self.voice = None

        async def open_session(url, name, clock, on_event=None):
            if name == "voice":
                self.voice = await Scripted.open(url, name, clock, on_event)
                return self.voice
            return await FakeSession.open(url, name, clock, on_event)

        prototype.Session.open = open_session
        self.addCleanup(setattr, prototype.Session, "open", self.original_open)
        self.spoken = []
        self.voice = await prototype.Session.open("ws://unused", "voice", lambda: CLOCK)

    async def turn(self, script, snapshot_mode="reply-no-utterance", fallback_pcm=b"\x01\x00" * 480):
        self.voice.script = script
        self.voice.asked = 0
        return await prototype.run_clip(
            Source(), "c", False, "ws://unused", None, 0.0, Path(self.tmp.name), {}, "hybrid", None, "gated",
            voice=self.voice, snapshot_mode=snapshot_mode, fallback_pcm=fallback_pcm,
            prior_utterances=("What's the weather in Denver tomorrow?", "And what about Saturday?"),
            speak=heard_by(self.spoken, with_bytes=True))

    async def test_weekday_invented_from_correction_is_asked_again(self):
        """Sunday is not in the call, so the model's first answer is dropped and the turn asks again."""
        record = await self.turn(["I'm running the task to get the weather in Denver for Sunday.",
                                  "I don't have the day for that yet."])
        self.assertEqual(self.voice.asked, 2, "the bad reply, and the one re-ask")
        self.assertEqual(self.spoken[0][0], "I don't have the day for that yet.")
        self.assertEqual([reply["text"] for reply in record["replies"]], ["I don't have the day for that yet."],
                         "the record says what was spoken, not what the model first wrote")
        notes = [item["content"][0]["text"] for item in [event["item"] for event in self.voice.sent
                                                        if event.get("type") == "conversation.item.create"]
                 if item["content"][0]["text"].startswith(prototype.FACT_CHECK_PREFIX)]
        self.assertEqual(len(notes), 1, "one machine note says the value is not available, and it names no value")
        self.assertNotIn("Sunday", notes[0])
        checks = [entry for entry in record["timeline"] if entry["kind"] == "fact_check"]
        self.assertEqual(checks[0]["ungrounded"], ["weekday Sunday"])
        self.assertTrue(checks[0]["asked_again"])

    async def test_a_second_bad_reply_is_not_spoken_and_the_turn_says_one_line(self):
        """If the re-ask invents again, the turn says the fixed line rather than a different wrong day."""
        record = await self.turn(["I'm running the task to get the weather in Denver for Sunday.",
                                  "I'm running the task to get the weather in Denver for Monday."])
        self.assertEqual([text for text, _ in self.spoken], [prototype.FALLBACK_LINE],
                         "one short line reaches the speaker, not the invented day")
        self.assertGreater(self.spoken[0][1], 0, "and it has audio, so the turn is not silent")
        self.assertEqual([reply["text"] for reply in record["replies"]], [prototype.FALLBACK_LINE])
        self.assertEqual(record["replies"][0]["blocked"]["ungrounded"], ["weekday Monday"],
                         "the record keeps the text it refused to say")
        self.assertTrue([entry for entry in record["timeline"] if entry.get("blocked")])

    async def test_a_reply_that_names_the_check_is_replaced_by_the_line(self):
        """The recorded pair: the invented day, then an answer about the check itself. Neither is said."""
        record = await self.turn(["I'm running the task to get the weather in Denver for Sunday.",
                                  "I do not have the information needed to answer the fact check."])
        self.assertEqual(self.voice.asked, 2, "the bad reply, and the one re-ask")
        self.assertEqual([text for text, _ in self.spoken], [prototype.FALLBACK_LINE])
        self.assertEqual(spelled_out(self.spoken[0][0]), [], "the spoken line names no machinery")
        self.assertEqual(record["replies"][0]["blocked"]["machinery"], ["fact check"],
                         "the record says why the second answer was refused")

    async def test_no_spoken_text_names_the_machinery(self):
        """Every line this turn speaks is clean, whatever the model wrote on the way there."""
        await self.turn(["I'm running the task to get the weather in Denver for Sunday.",
                         "This snapshot says the task description was not available, so check the note."])
        self.assertTrue(self.spoken, "the turn still says something")
        for text, _ in self.spoken:
            self.assertEqual(spelled_out(text), [], f"{text!r} names the machinery")

    async def test_a_grounded_reply_is_spoken_once(self):
        record = await self.turn(["I'm running the task to get the weather in Denver for Saturday."])
        self.assertEqual(self.voice.asked, 1, "one reply, with no re-ask")
        self.assertEqual(self.spoken[0][0], "I'm running the task to get the weather in Denver for Saturday.")
        self.assertEqual([entry["kind"] for entry in record["timeline"] if entry["kind"] == "fact_check"], [])
        self.assertEqual([reply["text"] for reply in record["replies"]],
                         ["I'm running the task to get the weather in Denver for Saturday."])

    def test_a_refusal_is_refused_only_while_a_task_is_running(self):
        refusal = "I don't have access to real-time weather data. You might want to check a weather app."
        self.assertTrue(prototype.refused(refusal, [], running=True)["contradicts"])
        self.assertFalse(prototype.refused(refusal, [], running=False).get("contradicts", []),
                         "a task that already failed may be refused")
        self.assertFalse(prototype.refused("I'm checking the weather in Denver for tomorrow.", [], running=True)
                         .get("contradicts", []), "an acknowledgement is not a refusal")

    def test_is_running_reads_the_snapshot_status(self):
        self.assertTrue(prototype.is_running({"tasks": [{"status": "running"}]}))
        self.assertFalse(prototype.is_running({"tasks": [{"status": "completed"}]}))
        self.assertFalse(prototype.is_running(None))

    def test_unavailable_request_may_be_reported_while_another_task_runs(self):
        snapshot = {"tasks": [{"status": "running"}, {"status": "failed"}]}
        self.assertFalse(prototype.is_running(snapshot))
        self.assertFalse(prototype.refused("I can't access your email while I check the weather.", [],
                                           running=prototype.is_running(snapshot)).get("contradicts", []))


class TurnNoteTest(unittest.IsolatedAsyncioTestCase):
    """A turn that ends with a task update of its own, so the reply and the update can each lose the check."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        use_decider_cache(Path(self.tmp.name) / "decider-cache", 20)
        self.original_open = prototype.Session.open
        self.voice = None

        async def open_session(url, name, clock, on_event=None):
            if name == "voice":
                self.voice = await Scripted.open(url, name, clock, on_event)
                return self.voice
            session = await (Delegating if name == "tools" else FakeSession).open(url, name, clock, on_event)
            if name == "asr":
                session.transcript = "And what about Saturday?"
            return session

        prototype.Session.open = open_session
        self.addCleanup(setattr, prototype.Session, "open", self.original_open)
        self.spoken = []
        self.voice = await prototype.Session.open("ws://unused", "voice", lambda: CLOCK)

    async def turn(self, script):
        self.voice.script = script
        self.voice.asked = 0
        return await prototype.run_clip(
            Source(), "c", False, "ws://unused", None, 0.05, Path(self.tmp.name), {}, "hybrid", None, "gated",
            voice=self.voice, snapshot_mode="reply-no-utterance", fallback_pcm=b"\x01\x00" * 480,
            prior_utterances=("What's the weather in Denver tomorrow?", "And what about Saturday?"),
            speak=heard_by(self.spoken, with_bytes=True))

    async def test_a_reply_and_an_update_that_both_fail_say_one_line(self):
        record = await self.turn(["I'm running the task to get the weather in Denver for Sunday.",
                                  "I do not have the information needed to answer the fact check.",
                                  "The task to get the weather in Denver for Monday has been completed.",
                                  "I cannot read the task description in the note."])
        self.assertEqual(self.voice.asked, 4, "two bad answers and two re-asks")
        self.assertEqual([text for text, _ in self.spoken], [prototype.FALLBACK_LINE],
                         "the turn says the line once, not once per failed reply")
        self.assertEqual([reply["kind"] for reply in record["replies"]], ["snapshot", "update"],
                         "both replies are still in the record")
        self.assertEqual([reply["text"] for reply in record["replies"]],
                         [prototype.FALLBACK_LINE, ""], "the update is dropped, the line already went out")

    async def test_a_failed_reply_and_a_clean_update_say_the_line_and_the_update(self):
        record = await self.turn(["I'm running the task to get the weather in Denver for Sunday.",
                                  "I do not have the information needed to answer the fact check.",
                                  "The task to get the weather in Denver for Saturday is done."])
        self.assertEqual([text for text, _ in self.spoken],
                         [prototype.FALLBACK_LINE, "The task to get the weather in Denver for Saturday is done."],
                         "the line comes once, and the update still reaches the speaker")
        self.assertEqual([reply["kind"] for reply in record["replies"]], ["snapshot", "update"])


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


class FallbackLineTest(unittest.TestCase):
    """The one line a failed turn says, judged by the reply rules it has to meet."""

    def test_it_is_short_persona_speech(self):
        self.assertLess(len(prototype.FALLBACK_LINE.split()), 8, "under eight words")
        self.assertEqual(names_machinery(prototype.FALLBACK_LINE), [], "it names no machinery")
        self.assertEqual(ungrounded(prototype.FALLBACK_LINE, []), [], "it names no fact")


if __name__ == "__main__":
    unittest.main()
