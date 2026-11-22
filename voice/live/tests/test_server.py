import asyncio
import base64
import contextlib
import inspect
import json
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import websockets
from websockets.asyncio.server import serve

from voice.live import server
from voice.live.t3_tasks import T3Tasks
from voice.live.tests.test_endpoint import noise, tone
from voice.live.tests.test_t3_tasks import FakeText, worded
from voice.prototype import run as prototype_run
from voice.prototype.realtime import read_pcm16
from voice.prototype.run import run_clip
from voice.t3 import fakes as F
from voice.t3 import types as T
from voice.t3.acts import act
from voice.t3.acts import Kind as S
from voice.t3.speech import Speech
from voice.t3.journal import T3Journal, Update
from voice.t3.lifecycle import Follow

SECRET = "s" * 64
OUT_RATE = 24_000
PERSONAS = []
CALLS = []
VOICES = []
SENT_BEFORE = []


class FakeSession:
    def __init__(self):
        self.audio_bytes = 0

    def clock(self):
        return 0.0

    async def send(self, **event):
        self.audio_bytes += len(event.get("audio", ""))


class FakeVoice:
    """The call's voice session, standing in for llama-server's realtime socket."""

    def __init__(self, name, on_event):
        self.name, self.on_event, self.config = name, on_event, None
        self.events, self.sent, self.audio = [], [], []
        self.closed = False
        self.responses = []
        self.default_audio = bytes(960)
        self.reply_gate = None

    def clock(self):
        return 0.0

    @classmethod
    async def open(cls, url, name, clock, on_event=None):
        return cls(name, on_event)

    async def send(self, **event):
        self.sent.append(event)
        if event["type"] == "response.create":
            draft = next(e["item"]["content"][0]["text"] for e in reversed(self.sent)
                         if e["type"] == "conversation.item.create")
            draft = draft.split("The words: ", 1)[1].removeprefix('"').removesuffix('"]')
            text, pcm = self.responses.pop(0) if self.responses else (draft, self.default_audio)
            self.events.append({"type": "response.output_text.delta", "delta": text, "_t": 0})
            if pcm:
                self.audio.append((len(self.events), pcm))
                self.events.append({"type": "response.output_audio.delta", "_t": 0})
            if self.reply_gate is not None:
                self.reply_gate[0].set()
                await self.reply_gate[1].wait()
            self.events.append({"type": "response.done", "response": {"status": "completed"}, "_t": 0})

    async def wait_for(self, *types, after=0, timeout=120):
        if "session.created" in types:
            return {"type": "session.created",
                    "session": {"audio": {"output": {"format": {"rate": OUT_RATE}}}}}
        return next((e for e in self.events[after:] if e["type"] in types),
                    {"type": "session.updated" if "session.updated" in types else "conversation.item.added"})

    async def close(self):
        self.closed = True


async def fake_run_clip(source, *args, persona, voice=None, speak=None, prior_utterances=(), **kwargs):
    """Streams the turn's input, then hands the turn one checked 2 s reply through the live server's own transport,
    the way `run_clip` does after the fact check."""
    PERSONAS.append(persona)
    CALLS.append((*args, kwargs))
    VOICES.append(voice)
    SENT_BEFORE.append(list(voice.sent))
    voice.on_event({"type": "session.created", "session": {"audio": {"output": {"format": {"rate": OUT_RATE}}}}})
    await source.stream(FakeSession(), FakeSession())
    if kwargs.get("on_heard") is not None:
        kwargs["on_heard"](f"words in {source.name}")
    await asyncio.sleep(0.3)
    await prototype_run.floor_free(voice)
    speak(f"reply to {source.name}",b"\x01\x00" * (OUT_RATE * 2), True)
    return {"clip": source.name, "final_transcript": f"words in {source.name}",
            "replies": [{"text": f"reply to {source.name}"}], "metrics": {},
            "snapshot": {"final_transcript": f"state after {source.name}"}}


async def streaming_run_clip(source, *args, persona, voice=None, speak=None, **kwargs):
    """A turn whose 2 s reply is released in eight quarter-second pieces 0.2 s apart, as the release gate hands
    them over while the model generates."""
    VOICES.append(voice)
    voice.on_event({"type": "session.created", "session": {"audio": {"output": {"format": {"rate": OUT_RATE}}}}})
    await source.stream(FakeSession(), FakeSession())
    for _ in range(8):
        speak(f"reply to {source.name}", b"\x01\x00" * (OUT_RATE // 4), False)
        await asyncio.sleep(0.2)
    speak(f"reply to {source.name}", b"", True)
    return {"clip": source.name, "final_transcript": f"words in {source.name}",
            "replies": [{"text": f"reply to {source.name}"}], "metrics": {}}


async def talking_run_clip(source, *args, persona, voice=None, speak=None, **kwargs):
    """Turn 1 speaks a 2 s reply and turn 2 hands its audio to the voice session, as run_clip does at turn end."""
    VOICES.append(voice)
    SENT_BEFORE.append(list(voice.sent))
    voice.on_event({"type": "session.created", "session": {"audio": {"output": {"format": {"rate": OUT_RATE}}}}})
    await source.stream(FakeSession(), FakeSession())
    if source.n == 1:
        speak(f"reply to {source.name}", b"\x01\x00" * (OUT_RATE * 2), True)
    else:
        await source.send_voice(voice)
    return {"clip": source.name, "final_transcript": f"words in {source.name}", "replies": [], "metrics": {}}


async def held_run_clip(source, *args, persona, voice=None, speak=None, **kwargs):
    """Turn 1 speaks a 2 s reply; turn 2 never answers, so speech after it ends continues it."""
    VOICES.append(voice)
    SENT_BEFORE.append(list(voice.sent))
    voice.on_event({"type": "session.created", "session": {"audio": {"output": {"format": {"rate": OUT_RATE}}}}})
    await source.stream(FakeSession(), FakeSession())
    if source.n == 1:
        speak(f"reply to {source.name}", b"\x01\x00" * (OUT_RATE * 2), True)
    elif source.n == 2:
        await asyncio.Event().wait()
    return {"clip": source.name, "final_transcript": f"words in {source.name}", "replies": [], "metrics": {}}


LONG_REPLY = "The forecast for Durham today shows a high of 57 degrees and a 0 percent chance of rain at all."
RELEASE_REST = []


async def unfinished_run_clip(source, *args, persona, voice=None, speak=None, **kwargs):
    """Turn 1 releases 1 s of audio on a 19-word text and is still speaking until the test releases the rest."""
    VOICES.append(voice)
    voice.on_event({"type": "session.created", "session": {"audio": {"output": {"format": {"rate": OUT_RATE}}}}})
    await source.stream(FakeSession(), FakeSession())
    if source.n == 1:
        speak(LONG_REPLY, b"\x01\x00" * OUT_RATE, False)
        await RELEASE_REST[0].wait()
        speak(LONG_REPLY, b"", True)
    return {"clip": source.name, "final_transcript": f"words in {source.name}", "replies": [], "metrics": {}}


class LiveServerTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.frames = []
        PERSONAS.clear()
        CALLS.clear()
        VOICES.clear()
        SENT_BEFORE.clear()
        self.tmp = tempfile.TemporaryDirectory()
        args = SimpleNamespace(silence_ms=700, start_ms=100, preroll_ms=300, floor_dbfs=-50.0, margin_db=10.0,
                               max_turn_s=30.0, prompt=Path(self.tmp.name) / "prompt.txt", delay=4.0,
                               segmenter="hybrid", snapshot="reply",
                               recall="note")
        self.live = server.Live(args, "ws://unused", decider=None, tools=None, out=Path(self.tmp.name),
                                secret=SECRET)
        self.patch = mock.patch.object(server, "run_clip", fake_run_clip)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        session_patch = mock.patch.object(server.Session, "open", FakeVoice.open)
        session_patch.start()
        self.addCleanup(session_patch.stop)
        self.heard("And one more thing.")
        self.ws_server = await serve(self.live.handler, "127.0.0.1", 0, process_request=server.authorize(SECRET))
        self.url = f"ws://127.0.0.1:{self.ws_server.sockets[0].getsockname()[1]}"

    async def asyncTearDown(self):
        self.ws_server.close()
        await self.ws_server.wait_closed()
        self.tmp.cleanup()

    async def connect(self, headers=None):
        ws = await websockets.connect(self.url, additional_headers=headers or {server.HEADER: SECRET})
        await ws.send(json.dumps({"type": "hello", "mode": "vad"}))
        self.assertEqual(json.loads(await ws.recv())["type"], "ready")
        return ws

    async def send(self, ws, pcm):
        for offset in range(0, len(pcm), 640):
            await ws.send(pcm[offset:offset + 640])

    async def until(self, ws, kind, log):
        while True:
            message = await asyncio.wait_for(ws.recv(), 10)
            log.append("audio" if isinstance(message, bytes) else json.loads(message)["type"])
            if isinstance(message, bytes):
                self.frames.append(len(message))
            if log[-1] == kind:
                return json.loads(message)

    async def test_t3_off_preserves_the_persona_for_each_snapshot_mode(self):
        for mode, expected in (("note", server.NOTE_PERSONA), ("reply", server.PERSONA + server.CALL_RULES)):
            with self.subTest(mode=mode):
                self.live.args.snapshot = mode
                self.live.args.t3 = "off"
                conversation = server.Conversation(self.live, None, SECRET)
                await conversation.open_voice()
                self.assertEqual(conversation.persona, expected)
                self.assertEqual(conversation.voice.sent, [])
                await conversation.voice.close()

    async def test_a_missing_or_wrong_secret_is_refused(self):
        for headers in ({}, {server.HEADER: "wrong"}, {server.HEADER: SECRET, "Origin": "http://example.com"}):
            with self.assertRaises(websockets.InvalidStatus):
                await websockets.connect(self.url, additional_headers=headers)

    async def test_a_turn_streams_its_reply_back(self):
        ws = await self.connect()
        log = []
        await self.send(ws, noise(500) + tone(600) + noise(900, seed=1))
        start = await self.until(ws, "reply_start", log)
        self.assertEqual(start["rate"], OUT_RATE)
        done = await self.until(ws, "turn_done", log)
        self.assertEqual(log[:2], ["turn_start", "turn_end"])
        self.assertEqual(sum(self.frames), OUT_RATE * 2 * 2, "all 2 s of the reply")
        self.assertLessEqual(max(self.frames), OUT_RATE * 2 // 25, "no frame longer than 40 ms")
        self.assertIn("reply_end", log)
        self.assertIsNone(done["metrics"]["cancelled"])
        self.assertEqual(CALLS[0][9], "gated", "the live turn keeps the default gated pipeline")
        self.assertEqual(CALLS[0][0], "c", "gated injects the snapshot as in c")
        self.assertEqual(CALLS[0][7], "hybrid", "gated needs the hybrid segmenter")
        self.assertTrue((Path(self.tmp.name) / "turn_001.json").is_file())
        await ws.close()

    async def test_the_prompt_file_sets_the_turn_persona(self):
        (Path(self.tmp.name) / "prompt.txt").write_text("Be brief.\n", encoding="utf-8")
        ws = await self.connect()
        await self.send(ws, noise(500) + tone(600) + noise(900, seed=1))
        await self.until(ws, "turn_done", [])
        self.assertEqual(len(PERSONAS), 1)
        self.assertTrue(PERSONAS[0].startswith("Be brief."))
        self.assertIn(server.CALL_RULES, PERSONAS[0], "the call's rules follow the prompt")
        record = json.loads((Path(self.tmp.name) / "turn_001.json").read_text())
        self.assertTrue(record["live"]["prompt"].startswith("prompt.txt sha256:"))
        await ws.close()

    async def test_a_call_keeps_one_voice_session(self):
        ws = await self.connect()
        await self.send(ws, noise(500) + tone(600) + noise(900, seed=1))
        await self.until(ws, "turn_done", [])
        await self.send(ws, noise(500) + tone(600) + noise(900, seed=2))
        await self.until(ws, "turn_done", [])
        self.assertEqual(len(VOICES), 2)
        self.assertIs(VOICES[0], VOICES[1], "turn 2 adds to turn 1's session, it does not open one")
        self.assertFalse(VOICES[0].closed)
        first = json.loads((Path(self.tmp.name) / "turn_001.json").read_text())
        second = json.loads((Path(self.tmp.name) / "turn_002.json").read_text())
        self.assertEqual(second["live"]["call"], {"turn": 2, "session_turn": 2})
        self.assertNotEqual(first["live"]["call"], second["live"]["call"])
        await ws.close()

    async def test_the_snapshot_mode_and_the_last_snapshot_reach_the_turn(self):
        self.live.args.snapshot = "before-audio"
        ws = await self.connect()
        await self.send(ws, noise(500) + tone(600) + noise(900, seed=1))
        await self.until(ws, "turn_done", [])
        await self.send(ws, noise(500) + tone(600) + noise(900, seed=2))
        await self.until(ws, "turn_done", [])
        self.assertEqual([call[-1]["snapshot_mode"] for call in CALLS], ["before-audio", "before-audio"])
        self.assertIsNone(CALLS[0][-1]["prior_snapshot"], "the call's first turn has no task state yet")
        self.assertEqual(CALLS[1][-1]["prior_snapshot"], {"final_transcript": "state after turn_001"})
        record = json.loads((Path(self.tmp.name) / "turn_002.json").read_text())
        self.assertEqual(record["live"]["snapshot"], "before-audio", "the record says where the item went")
        await ws.close()

    async def test_a_long_call_stays_on_its_one_session(self):
        ws = await self.connect()
        for seed in range(1, 11):
            await self.send(ws, noise(500) + tone(600) + noise(900, seed=seed))
            await self.until(ws, "turn_done", [])
        self.assertEqual(len({id(voice) for voice in VOICES}), 1, "ten turns, one voice session, never restarted")
        self.assertFalse(VOICES[0].closed)
        self.assertNotIn("seed", CALLS[-1][-1], "nothing rebuilds the session from the call's text")
        record = json.loads((Path(self.tmp.name) / "turn_010.json").read_text())
        self.assertEqual(record["live"]["call"], {"turn": 10, "session_turn": 10})
        await ws.close()

    async def test_a_new_prompt_mid_call_waits_for_the_next_call(self):
        prompt = Path(self.tmp.name) / "prompt.txt"
        prompt.write_text("Be brief.\n", encoding="utf-8")
        ws = await self.connect()
        await self.send(ws, noise(500) + tone(600) + noise(900, seed=1))
        await self.until(ws, "turn_done", [])
        prompt.write_text("Be warm.\n", encoding="utf-8")
        await self.send(ws, noise(500) + tone(600) + noise(900, seed=2))
        await self.until(ws, "turn_done", [])
        self.assertIs(VOICES[0], VOICES[1], "the call keeps its session")
        self.assertTrue(PERSONAS[1].startswith("Be brief."), "and the persona it started with")
        await ws.close()

    def heard(self, text):
        async def transcribe(pcm):
            self.transcribed.append(len(pcm))
            if isinstance(text, Exception):
                raise text
            return text
        self.transcribed = []
        self.live.transcribe = transcribe

    def overlaps(self):
        path = Path(self.tmp.name) / "overlaps.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.is_file() else []

    async def reply_playing(self, ws):
        log = []
        await self.send(ws, noise(500) + tone(600) + noise(900, seed=1))
        await self.until(ws, "reply_start", log)
        return log

    async def test_speech_during_the_reply_stops_playback_and_keeps_the_call(self):
        self.heard("unused")
        ws = await self.connect()
        log = await self.reply_playing(ws)
        await self.send(ws, tone(800))
        barge = await self.until(ws, "barge_in", log)
        self.assertEqual(barge["turn"], 1, "the reply the person talked over is the one flushed")
        self.assertIn("duck", log, "the reply is turned down as soon as the speech starts")
        self.assertIn("reply_end", log, "the reply ended before the person spoke")
        after = []
        started = await self.until(ws, "turn_start", after)
        self.assertEqual(started["turn"], 2)
        await self.send(ws, noise(900, seed=2))
        await self.until(ws, "turn_end", after)
        while (done := await self.until(ws, "turn_done", after))["turn"] != 2:
            pass
        self.assertEqual(self.transcribed, [], "enough voiced speech needs no transcript")
        self.assertEqual((done["metrics"]["overlap"]["verdict"], done["metrics"]["overlap"]["how"]),
                         ("barge_in", "voiced"))
        self.assertFalse(VOICES[0].closed, "a barge-in keeps the call's session, and its history, open")
        self.assertIs(VOICES[0], VOICES[1], "the next turn continues on the same session")
        await ws.close()

    async def test_a_barge_in_during_a_streamed_reply_stops_its_audio_and_ends_it_once(self):
        self.heard("unused")
        with mock.patch.object(server, "run_clip", streaming_run_clip):
            ws = await self.connect()
            log = await self.reply_playing(ws)
            await self.send(ws, tone(800))
            await self.until(ws, "barge_in", log)
            sent_before = sum(self.frames)
            after = []
            while (await self.until(ws, "turn_done", after))["turn"] != 1:
                pass
            self.assertNotIn("audio", after, "nothing of the reply goes out once the person talks over it")
            self.assertEqual(after.count("reply_end"), 1, "the client still hears the reply end, once")
            self.assertEqual(log.count("reply_start"), 1, "one reply, however many pieces it was released in")
            self.assertLess(sent_before, OUT_RATE * 2 * 2, "the reply was cut before its 2 s were sent")
            self.assertFalse(VOICES[0].closed, "the call's one session stays open")
            await ws.close()

    async def test_the_barge_in_turn_keeps_the_words_that_caused_it(self):
        self.heard("unused")
        ws = await self.connect()
        log = await self.reply_playing(ws)
        await self.send(ws, tone(800) + noise(900, seed=2))
        while (await self.until(ws, "turn_done", log))["turn"] != 2:
            pass
        size = (Path(self.tmp.name) / "turn_002_input.wav").stat().st_size
        self.assertGreater(size, (800 + 700) * 32, "all 800 ms of speech and the trailing silence")
        await ws.close()

    def heard_reply(self, text):
        async def hear(pcm, rate):
            self.heard_audio.append((len(pcm), rate))
            return text
        self.heard_audio = []
        self.live.hear_reply = hear

    def created(self, turn):
        """The session items added between the previous turn's start and this turn's."""
        return [event["item"] for event in SENT_BEFORE[turn][len(SENT_BEFORE[turn - 1]):]
                if event["type"] == "conversation.item.create"]

    async def barge_in(self, ws, log):
        await self.send(ws, tone(800) + noise(900, seed=2))
        while (await self.until(ws, "turn_done", log))["turn"] != 2:
            pass
        return json.loads((Path(self.tmp.name) / "turn_002.json").read_text())

    async def test_a_barge_in_records_what_was_heard_and_sends_no_note_by_default(self):
        self.heard_reply("Reply to turn")
        ws = await self.connect()
        record = await self.barge_in(ws, await self.reply_playing(ws))
        self.assertEqual(len(self.heard_audio), 1, "the interrupted reply is read back once")
        played, rate = self.heard_audio[0]
        self.assertEqual(rate, OUT_RATE)
        self.assertGreater(played, 0)
        self.assertLess(played, OUT_RATE * 2 * 2, "only the part of the reply the client had played")
        self.assertEqual(self.created(1), [], "the note ships off")
        barge = record["live"]["barge_in"]
        self.assertEqual((barge["heard"], barge["whole"], barge["note"]), ("reply to turn", False, None),
                         "the record keeps what the read-back matched, up to its last matched word")
        self.assertIn("estimate", barge["played_s_is"])

    async def test_with_the_note_on_a_barge_in_tells_the_session_what_was_heard(self):
        self.live.barge_in_note = True
        self.heard_reply("Reply to turn")
        ws = await self.connect()
        record = await self.barge_in(ws, await self.reply_playing(ws))
        note = self.created(1)[0]["content"][0]["text"]
        self.assertIn("interrupted your last reply", note)
        self.assertNotIn("reply to turn", note.lower(), "quoting the heard words primed the model to go on with them")
        barge = record["live"]["barge_in"]
        self.assertEqual(barge["note"], note, "the turn's record carries the note it sent")
        self.assertIs(barge["note_item"]["accepted"], True, "and whether the session took it")

    async def test_a_barge_in_note_comes_before_the_turns_audio(self):
        self.live.barge_in_note = True
        self.heard_reply("Reply to turn")
        with mock.patch.object(server, "run_clip", talking_run_clip):
            ws = await self.connect()
            await self.barge_in(ws, await self.reply_playing(ws))
        sent = VOICES[1].sent
        note_at = next(i for i, event in enumerate(sent) if event["type"] == "conversation.item.create")
        audio_at = next(i for i, event in enumerate(sent) if event["type"] == "input_audio_buffer.append")
        self.assertLess(note_at, audio_at, "the note is in the session before the barge-in turn's audio")

    async def test_no_note_when_nothing_of_the_reply_was_heard(self):
        self.live.barge_in_note = True
        self.heard_reply("")
        ws = await self.connect()
        record = await self.barge_in(ws, await self.reply_playing(ws))
        self.assertEqual(self.created(1), [])
        self.assertEqual((record["live"]["barge_in"]["heard"], record["live"]["barge_in"]["note"]), ("", None))

    async def test_a_continued_barge_in_turn_keeps_what_was_heard_and_its_one_note(self):
        self.live.barge_in_note = True
        self.heard_reply("Reply to turn")
        with mock.patch.object(server, "run_clip", held_run_clip):
            ws = await self.connect()
            log = await self.reply_playing(ws)
            await self.send(ws, tone(800) + noise(900, seed=2))
            self.assertEqual((await self.until(ws, "turn_end", log))["turn"], 2)
            await self.send(ws, tone(600) + noise(900, seed=3))
            while (await self.until(ws, "turn_done", log))["turn"] != 3:
                pass
        continued = json.loads((Path(self.tmp.name) / "turn_003.json").read_text())
        dropped = json.loads((Path(self.tmp.name) / "turn_002.json").read_text())
        self.assertEqual(dropped["cancelled"], "continued")
        self.assertIsNone(dropped["live"]["barge_in"], "the record moves with the audio")
        self.assertEqual(continued["live"]["barge_in"]["heard"], "reply to turn")
        self.assertIsNotNone(continued["live"]["barge_in"]["note"])
        notes = [event for event in VOICES[0].sent if event["type"] == "conversation.item.create"]
        self.assertEqual(len(notes), 1, "the note went in once, before the audio the continued turn carries")

    def held_read_back(self, text):
        release = asyncio.Event()

        async def hear(pcm, rate):
            await release.wait()
            return text

        self.live.hear_reply = hear
        return release

    async def test_a_slow_read_back_delays_neither_turn_done_nor_the_next_reply(self):
        release = self.held_read_back("Reply to turn")
        with mock.patch.object(server, "READBACK_TIMEOUT_S", 60, create=True):
            ws = await self.connect()
            log = await self.reply_playing(ws)
            await self.send(ws, tone(800) + noise(900, seed=2))
            while (await self.until(ws, "turn_done", log))["turn"] != 2:
                pass
            path = Path(self.tmp.name) / "turn_002.json"
            pending = json.loads(path.read_text())["live"]["barge_in"]
            self.assertEqual((pending["heard"], pending["pending"]), (None, True), "written before the read-back lands")
            await self.send(ws, noise(500) + tone(600) + noise(900, seed=1))
            while (await self.until(ws, "reply_start", log))["turn"] != 3:
                pass
            read_backs = list(self.live.active.read_backs)
            release.set()
            await asyncio.gather(*read_backs)
        landed = json.loads(path.read_text())["live"]["barge_in"]
        self.assertEqual(landed["heard"], "reply to turn", "the record is updated when the read-back lands")
        self.assertNotIn("pending", landed)
        await ws.close()

    async def test_a_turn_continued_while_the_read_back_is_pending_keeps_its_record_and_audio(self):
        release = self.held_read_back("Reply to turn")
        returned = asyncio.Event()

        async def quiet_second_turn(source, *args, **kwargs):
            record = await talking_run_clip(source, *args, **kwargs)
            if source.n == 2:
                returned.set()
            return record

        with mock.patch.object(server, "run_clip", quiet_second_turn), \
                mock.patch.object(server, "READBACK_TIMEOUT_S", 60, create=True):
            ws = await self.connect()
            log = await self.reply_playing(ws)
            await self.send(ws, tone(800) + noise(900, seed=2))
            await asyncio.wait_for(returned.wait(), 10)
            await self.send(ws, tone(600) + noise(900, seed=3))
            while (started := await self.until(ws, "turn_start", log))["turn"] != 3:
                pass
            self.assertEqual(started["carried_audio_s"], 0.0, "turn 2 already sent its audio to the session")
            release.set()
            while (await self.until(ws, "turn_done", log))["turn"] != 3:
                pass
        answered = json.loads((Path(self.tmp.name) / "turn_002.json").read_text())
        self.assertIsNone(answered["live"]["cancelled"], "turn 2 ended on its own and was not continued")
        self.assertIsNotNone(answered["live"]["barge_in"])
        await ws.close()

    async def test_a_reply_still_being_released_was_not_heard_whole(self):
        RELEASE_REST[:] = [asyncio.Event()]
        self.heard_reply("The forecast for")

        async def stop_after_the_audio_ran_out(pcm):
            await asyncio.sleep(max(0.0, self.live.active.playback.until - server.time.perf_counter()) + 0.05)
            return "Stop."

        self.live.transcribe = stop_after_the_audio_ran_out
        with mock.patch.object(server, "run_clip", unfinished_run_clip):
            ws = await self.connect()
            log = await self.reply_playing(ws)
            await self.send(ws, tone(300) + noise(900, seed=2))
            await self.until(ws, "barge_in", log)
            RELEASE_REST[0].set()
            while (await self.until(ws, "turn_done", log))["turn"] != 2:
                pass
        barge = json.loads((Path(self.tmp.name) / "turn_002.json").read_text())["live"]["barge_in"]
        self.assertEqual((barge["whole"], barge["heard"]), (False, "The forecast for"),
                         "all the audio sent had played, but the text it was released on runs past it")
        self.assertEqual(self.heard_audio, [(OUT_RATE * 2, OUT_RATE)], "the read-back covers all the audio sent")
        await ws.close()

    async def test_a_push_to_talk_barge_in_gets_the_same_record(self):
        self.heard_reply("Reply to turn")
        ws = await websockets.connect(self.url, additional_headers={server.HEADER: SECRET})
        await ws.send(json.dumps({"type": "hello", "mode": "ptt"}))
        self.assertEqual(json.loads(await ws.recv())["type"], "ready")
        log = []
        for turn in (1, 2):
            await ws.send(json.dumps({"type": "ptt", "down": True}))
            await self.send(ws, tone(400))
            await ws.send(json.dumps({"type": "ptt", "down": False}))
            await self.until(ws, "reply_start" if turn == 1 else "barge_in", log)
        while (await self.until(ws, "turn_done", log))["turn"] != 2:
            pass
        record = json.loads((Path(self.tmp.name) / "turn_002.json").read_text())
        self.assertEqual(record["live"]["barge_in"]["heard"], "reply to turn")

    async def test_a_backchannel_over_the_reply_leaves_it_playing(self):
        self.heard("Mm-hm.")
        ws = await self.connect()
        log = await self.reply_playing(ws)
        await self.send(ws, tone(300) + noise(900, seed=2))
        unduck = await self.until(ws, "unduck", log)
        self.assertEqual((unduck["verdict"], unduck["turn"]), ("backchannel", 1))
        self.assertNotIn("barge_in", log)
        self.assertEqual(log.count("turn_start"), 1, "a backchannel is not a turn")
        self.assertEqual(len(self.transcribed), 1)
        entry = self.overlaps()[0]
        self.assertEqual((entry["verdict"], entry["how"], entry["transcript"]), ("backchannel", "transcript", "Mm-hm."))
        self.assertTrue((Path(self.tmp.name) / entry["audio"]).is_file(), "a dismissed overlap keeps its audio")
        await ws.close()

    async def test_the_reply_coming_back_through_speakers_is_echo(self):
        self.heard("reply to turn")
        ws = await self.connect()
        log = await self.reply_playing(ws)
        await self.send(ws, tone(400) + noise(900, seed=2))
        unduck = await self.until(ws, "unduck", log)
        self.assertEqual(unduck["verdict"], "echo")
        self.assertNotIn("barge_in", log)
        await ws.close()

    async def test_a_short_command_over_the_reply_stops_it(self):
        self.heard("Stop.")
        ws = await self.connect()
        log = await self.reply_playing(ws)
        await self.send(ws, tone(300) + noise(900, seed=2))
        barge = await self.until(ws, "barge_in", log)
        self.assertEqual(barge["turn"], 1)
        started = await self.until(ws, "turn_start", log)
        self.assertEqual(started["turn"], 2)
        ended = await self.until(ws, "turn_end", log)
        self.assertEqual(ended["turn"], 2, "the endpointer already heard the command end")
        while (done := await self.until(ws, "turn_done", log))["turn"] != 2:
            pass
        self.assertEqual(done["metrics"]["overlap"]["transcript"], "Stop.")
        await ws.close()

    async def test_a_failed_transcript_keeps_the_words_as_a_turn(self):
        self.heard(ConnectionError("no slot"))
        ws = await self.connect()
        log = await self.reply_playing(ws)
        await self.send(ws, tone(300) + noise(900, seed=2))
        await self.until(ws, "barge_in", log)
        started = await self.until(ws, "turn_start", log)
        self.assertEqual(started["turn"], 2)
        await ws.close()

    async def test_a_blip_over_the_reply_is_noise_without_a_transcript(self):
        self.heard("unused")
        ws = await self.connect()
        log = await self.reply_playing(ws)
        await self.send(ws, tone(140) + noise(900, seed=2))
        unduck = await self.until(ws, "unduck", log)
        self.assertEqual(unduck["verdict"], "noise")
        self.assertEqual(self.transcribed, [])
        await ws.close()

    async def test_speech_the_reply_ends_under_becomes_a_plain_turn(self):
        self.heard("unused")
        ws = await self.connect()
        log = await self.reply_playing(ws)
        await self.send(ws, tone(200))
        await self.until(ws, "duck", log)
        await asyncio.sleep(2.3)
        await self.send(ws, tone(100))
        unduck = await self.until(ws, "unduck", log)
        self.assertEqual(unduck["verdict"], "turn")
        started = await self.until(ws, "turn_start", log)
        self.assertEqual(started["turn"], 2)
        self.assertNotIn("barge_in", log, "nothing was playing any more")
        await ws.close()

    async def test_words_before_the_reply_continue_the_turn(self):
        ws = await self.connect()
        log = []
        await self.send(ws, noise(500) + tone(600) + noise(720, seed=1))
        await self.until(ws, "turn_end", log)
        await self.send(ws, tone(300) + noise(900, seed=2))
        started = await self.until(ws, "turn_start", log)
        self.assertEqual(started["turn"], 2)
        self.assertGreater(started["carried_audio_s"], 1.0, "turn 2 starts with turn 1's audio")
        self.assertNotIn("reply_start", log, "turn 1 made no reply while the sound was undecided")
        self.assertNotIn("barge_in", log)
        self.assertEqual([o["verdict"] for o in self.overlaps()], ["turn"])
        await ws.close()

    async def test_words_join_the_turn_even_when_its_reply_was_already_on_its_way(self):
        async def in_flight(source, *args, persona, voice=None, speak=None, **kwargs):
            voice.on_event({"type": "session.created", "session": {"audio": {"output": {"format": {"rate": OUT_RATE}}}}})
            await source.stream(FakeSession(), FakeSession())
            await asyncio.sleep(0.3)
            speak(f"reply to {source.name}", bytes(OUT_RATE * 4), True)
            return {"clip": source.name, "final_transcript": "", "replies": [], "metrics": {}}

        with mock.patch.object(server, "run_clip", in_flight):
            ws = await self.connect()
            log = []
            await self.send(ws, noise(500) + tone(600) + noise(720, seed=1))
            await self.until(ws, "turn_end", log)
            await self.send(ws, tone(300))
            await asyncio.sleep(0.6)
            await self.send(ws, noise(900, seed=2))
            started = await self.until(ws, "turn_start", log)
            self.assertEqual(started["turn"], 2)
            self.assertGreater(started["carried_audio_s"], 1.0, "turn 2 starts with turn 1's audio")
            self.assertNotIn("reply_start", log, "turn 1's reply was parked, and dropped with turn 1")
            await ws.close()

    async def test_a_short_word_before_the_reply_is_transcribed_and_continues_the_turn(self):
        self.heard("And the second one.")
        ws = await self.connect()
        log = []
        await self.send(ws, noise(500) + tone(600) + noise(720, seed=1))
        await self.until(ws, "turn_end", log)
        await self.send(ws, tone(140) + noise(900, seed=2))
        started = await self.until(ws, "turn_start", log)
        self.assertEqual(started["turn"], 2, "the word continued the turn")
        self.assertTrue(self.transcribed, "and was transcribed, though most of it came with the start")
        await ws.close()

    async def test_talking_on_while_a_held_turn_speaks_again_stops_that_reply_before_they_finish(self):
        """The turn had spoken and was quiet between two replies when the person started; its second reply then
        played over them."""
        async def two_replies(source, *args, persona, voice=None, speak=None, **kwargs):
            voice.on_event({"type": "session.created", "session": {"audio": {"output": {"format": {"rate": OUT_RATE}}}}})
            await source.stream(FakeSession(), FakeSession())
            speak("first reply", bytes(OUT_RATE // 5 * 2), True)
            await asyncio.sleep(1.0)
            speak("second reply", bytes(OUT_RATE * 4 * 2), True)
            await asyncio.sleep(4)
            return {"clip": source.name, "final_transcript": "", "replies": [], "metrics": {}}

        self.heard("Wait, stop, that is not what I asked about.")
        with mock.patch.object(server, "run_clip", two_replies):
            ws = await self.connect()
            log = []
            await self.send(ws, noise(500) + tone(600) + noise(720, seed=1))
            await self.until(ws, "reply_end", log)
            await asyncio.sleep(0.6)
            await self.send(ws, tone(2500))
            barge = await self.until(ws, "barge_in", log)
            self.assertEqual(barge["turn"], 1, "the second reply stopped while they were still talking")
            self.assertIn("duck", log, "and was ducked while the sound was judged")
            await ws.close()

    async def test_the_reply_goes_while_wordless_sound_is_still_going(self):
        self.heard("")
        ws = await self.connect()
        log = []
        await self.send(ws, noise(500) + tone(600) + noise(720, seed=1))
        await self.until(ws, "turn_end", log)
        await self.send(ws, tone(4000))
        start = await self.until(ws, "reply_start", log)
        self.assertEqual(start["turn"], 1, "the reply came while the sound had not ended")
        self.assertEqual(log.count("turn_start"), 1)
        duck = await self.until(ws, "duck", log)
        self.assertEqual(duck["turn"], 1, "and plays ducked under the sound")
        await ws.close()

    async def test_sound_with_no_words_before_the_reply_cancels_nothing_and_the_reply_comes(self):
        self.heard("")
        ws = await self.connect()
        log = []
        await self.send(ws, noise(500) + tone(600) + noise(720, seed=1))
        await self.until(ws, "turn_end", log)
        await self.send(ws, tone(300) + noise(900, seed=2))
        done = await self.until(ws, "turn_done", log)
        self.assertEqual((done["turn"], done["metrics"]["cancelled"]), (1, None))
        self.assertIn("reply_start", log)
        self.assertEqual(log.count("turn_start"), 1, "the sound started no turn")
        self.assertNotIn("duck", log, "nothing was playing to duck")
        self.assertEqual([o["verdict"] for o in self.overlaps()], ["noise"])
        await ws.close()

    async def test_speech_after_the_reply_has_played_is_a_plain_turn(self):
        """Call 1 of 2026-11-16 15:26 logged 16 barge_in events; 15 came 1.5 to 17 s after the reply had played."""
        ws = await self.connect()
        await self.send(ws, noise(500) + tone(600) + noise(900, seed=1))
        await self.until(ws, "turn_done", [])
        await asyncio.sleep(2.3)
        log = []
        await self.send(ws, tone(140))
        started = await self.until(ws, "turn_start", log)
        self.assertEqual(started["turn"], 2)
        self.assertEqual(log, ["turn_start"], "nothing is playing, so there is nothing to duck or stop")
        await ws.close()

    async def test_a_second_client_is_turned_away(self):
        first = await self.connect()
        second = await websockets.connect(self.url, additional_headers={server.HEADER: SECRET})
        with self.assertRaises(websockets.ConnectionClosed):
            await second.recv()
        self.assertEqual(second.close_code, 1013)
        await first.close()


class News(T3Tasks):
    """A T3 whose `updates` arrive once the call has begun: news from before a call is not read when it opens."""

    def __init__(self, path, *updates, seam=None):
        super().__init__(Follow(T3Journal(path), F.Connector(seam) if seam is not None else None))
        worded(self, FakeText())
        self.given, self.arriving = [], [replace(update) for update in updates]

    def begin_call(self):
        super().begin_call()
        for update in self.arriving:
            self.follow.journal.pending_lines.append(update)
            self.follow.outbox.append(update)
        self.follow.journal.save()
        self.arriving = []

    @property
    def outbox(self):
        return self.follow.outbox

    def retry_news(self, updates):
        self.given.extend(update.acts for update in updates)
        super().retry_news(updates)


DONE = Update("conv_turn_001:task_1", (act(S.DONE, thread="Add a hello function", status="completed"),),
              "completed", "done-event", "thread-1", (750, 751))
DONE_LINE = "About Add a hello function, noted."
WAITS = Update("conv_turn_001:task_2", (act(S.NEEDS_YOU, thread="Add a goodbye function", need="question"),),
               "live", "needs_you", "thread-2", (760,))
WAITS_LINE = "About Add a goodbye function, noted."


class IdleNewsTest(unittest.IsolatedAsyncioTestCase):
    """T3 news while the person is silent goes out as its own turn, on the reply's own path."""

    asyncSetUp, asyncTearDown = LiveServerTest.asyncSetUp, LiveServerTest.asyncTearDown
    connect, send, until, heard = LiveServerTest.connect, LiveServerTest.send, LiveServerTest.until, LiveServerTest.heard

    def news(self, *updates, responses=(), audio=None, gate=None):
        self.live.t3 = News(Path(self.tmp.name) / "journal.json", *updates)

        async def open_voice(url, name, clock, on_event=None):
            voice = await FakeVoice.open(url, name, clock, on_event)
            voice.responses = list(responses)
            voice.reply_gate = gate
            if audio is not None:
                voice.default_audio = audio
            return voice

        for owner, name, value in ((server, "IDLE_GRACE_S", 0.05), (server, "COALESCE_S", 0.05),
                                   (server.Session, "open", open_voice)):
            patch = mock.patch.object(owner, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        return self.live.t3

    def record(self, n):
        return json.loads((Path(self.tmp.name) / f"turn_{n:03d}.json").read_text())

    async def test_news_while_the_call_is_silent_is_spoken_as_its_own_turn(self):
        t3 = self.news(DONE, audio=bytes(OUT_RATE * 2 * 2))
        ws = await self.connect()
        log = []
        self.live.news.set()
        start = await self.until(ws, "reply_start", log)
        done = await self.until(ws, "turn_done", log)
        self.assertNotIn("turn_start", log, "the person did not start this turn")
        self.assertEqual((start["turn"], done["turn"], done["idle"], done["rows"]), (1, 1, True, ["done-event"]))
        self.assertIn("reply_end", log)
        self.assertEqual(sum(self.frames), OUT_RATE * 2 * 2, "the whole line went out")
        record = self.record(1)
        self.assertTrue(record["idle"])
        spoken, rate, _ = read_pcm16(Path(self.tmp.name) / "turn_001_t3_update.wav")
        self.assertEqual((len(spoken), rate), (OUT_RATE * 2 * 2, OUT_RATE), "the line is kept as audio, like any spoken line")
        self.assertEqual(record["t3_updates"][0]["sequences"], [750, 751], "the record keeps the line's evidence")
        self.assertEqual(record["task_updates"][0]["evidence"], {"thread_id": "thread-1", "sequences": [750, 751]})
        self.assertEqual(record["replies"][0]["text"], DONE_LINE)
        settings, draft, response = self.live.active.voice.sent
        self.assertNotIn("note", settings["session"]["instructions"].lower(),
                         "the call's standing instructions say nothing about private notes")
        self.assertIn("say exactly these words", draft["item"]["content"][0]["text"].lower())
        self.assertEqual(draft["item"]["role"], "user")
        self.assertEqual(response["type"], "response.create")
        self.assertEqual(record["task_updates"][0]["acts"], [a.to_json() for a in DONE.acts])
        self.assertEqual((t3.outbox, t3.given), ([], []))
        await ws.close()

    async def test_silent_call_reply_is_retried_without_sending_audio(self):
        t3 = self.news(DONE, responses=[(DONE_LINE, b""), (DONE_LINE, bytes(960))])
        ws = await self.connect()
        log = []
        done = await self.until(ws, "turn_done", log)
        self.assertEqual((done["given_back"], t3.outbox, t3.given), (False, [], []), "the second try played, so nothing is handed back")
        self.assertEqual(self.frames, [960])
        notes = [event["item"]["content"][0]["text"] for event in self.live.active.voice.sent
                 if event["type"] == "conversation.item.create"]
        self.assertEqual(notes, [prototype_run.relay_note(DONE_LINE), prototype_run.relay_note(DONE_LINE, 1)],
                         "the retry sends the draft again, with no note about the reply that did not play")
        self.assertFalse(t3.has_news())
        await ws.close()

    async def test_two_idle_failures_hold_news_for_the_next_person_turn(self):
        t3 = self.news(DONE, responses=[("Something else finished.", bytes(960))] * 4)
        ws = await self.connect()
        for _ in range(2):
            done = await self.until(ws, "turn_done", [])
            self.assertTrue(done["given_back"])
        self.assertEqual(self.frames, [], "audio with rejected text must never reach the client")
        self.assertFalse(t3.has_news(idle=True))
        self.assertEqual(t3.reserve_news(idle=True), [])
        reopened = T3Journal(t3.follow.journal.path)
        self.assertEqual([(line.acts, line.delivery_failures) for line in reopened.pending_lines], [(DONE.acts, 2)])

        async def user_turn(source, *args, voice, send_checked, **kwargs):
            await source.stream(FakeSession(), FakeSession())
            service = server.T3TaskService(SimpleNamespace(records=lambda: []), source.name, t3=t3)
            entries = service.take_updates()
            replies = []
            for entry in entries:
                reply, _ = await server.say_acts(
                    voice, OUT_RATE, 0, entry, service, [], lambda *args, **kwargs: None,
                    send_audio=send_checked,
                    record_delivery=lambda state: service.record_delivery(entry, state))
                replies.append({"row": entry["row"], **reply})
            service.close()
            return {"clip": source.name, "replies": replies, "task_updates": entries}

        with mock.patch.object(server, "run_clip", user_turn):
            await self.send(ws, noise(300) + tone(600) + noise(900, seed=1))
            done = await self.until(ws, "turn_done", [])
        self.assertIsNone(done["error"])
        self.assertEqual((done["turn"], done["rows"], done["replies"]), (3, [DONE.row], [DONE_LINE]))
        self.assertEqual(self.frames, [960])
        self.assertFalse(T3Journal(t3.follow.journal.path).pending_lines)
        await ws.close()

    async def test_the_person_finishes_a_turn_while_an_idle_draft_is_pending(self):
        t3 = self.news(DONE)
        began, release = asyncio.Event(), threading.Event()
        loop = asyncio.get_running_loop()

        def draft(update, entry, turns):
            loop.call_soon_threadsafe(began.set)
            release.wait()
            return t3.say(update, turns)

        async with await self.connect() as ws:
            with mock.patch.object(t3, "say_entry", draft):
                try:
                    await asyncio.wait_for(began.wait(), 5)
                    await self.send(ws, noise(300) + tone(600) + noise(900, seed=1))
                    idle = await self.until(ws, "turn_done", [])
                    self.assertEqual((idle["idle"], idle["given_back"], idle["replies"]), (True, True, []))
                    spoken = await self.until(ws, "turn_done", [])
                    self.assertEqual((spoken["turn"], spoken["replies"]), (2, ["reply to turn_002"]))
                    self.assertFalse(release.is_set())
                    self.assertFalse(any(e["type"] == "conversation.item.create" for e in VOICES[-1].sent))
                    self.assertEqual([u.acts for u in T3Journal(t3.follow.journal.path).pending_lines], [DONE.acts])
                finally:
                    release.set()

    async def test_after_a_restart_a_thread_that_still_waits_is_raised_and_the_rest_is_held(self):
        first = self.news(DONE, WAITS)
        first.begin_call()
        self.assertEqual([u.acts for u in first.reserve_news(idle=True)], [DONE.acts, WAITS.acts])
        waiting = F.state(thread="thread-2", title="Add a goodbye function", as_of=770,
                          activity=T.ThreadActivity.WAITING_ON_YOU, pending=F.need("q1", "Run the tests?"))
        ended = F.state(thread="thread-1", title="Add a hello function", as_of=770, activity=T.ThreadActivity.FINISHED)

        class Reads(F.FakeSeam):
            def read_thread(self, thread_id):
                return waiting if thread_id == "thread-2" else ended

        self.live.t3 = restarted = News(first.follow.journal.path, seam=Reads())
        async with await self.connect() as ws:
            done = await self.until(ws, "turn_done", [])
            self.assertEqual(done["replies"], [WAITS_LINE], "the wait, read from T3 now; the done is held, not said")
            self.assertEqual(self.frames, [960])
        journal = T3Journal(restarted.follow.journal.path)
        self.assertEqual((journal.pending_lines, [u.acts for u in journal.earlier_lines if u.thread_id == "thread-1"]),
                         ([], [DONE.acts]))

    async def test_news_waits_while_the_person_talks_and_after_the_reply(self):
        self.news(DONE, audio=bytes(OUT_RATE * 2 * 2))
        ws = await self.connect()
        log = []
        await self.send(ws, noise(500) + tone(600))
        await self.until(ws, "turn_start", log)
        self.live.news.set()
        await self.send(ws, noise(900, seed=1))
        first = await self.until(ws, "turn_done", log)
        self.assertEqual((first["turn"], first.get("idle")), (1, None), "the person's turn goes first")
        second = await self.until(ws, "turn_done", log)
        self.assertEqual((second["turn"], second["idle"]), (2, True))
        self.assertEqual(log.count("reply_start"), 2, "the person's reply, then the news")
        await ws.close()

    async def test_speech_before_the_line_is_heard_gives_it_back_for_later(self):
        gate = (asyncio.Event(), asyncio.Event())
        t3 = self.news(DONE, gate=gate)
        ws = await self.connect()
        self.live.news.set()
        await asyncio.wait_for(gate[0].wait(), 5)
        log = []
        await self.send(ws, noise(300) + tone(600))
        await self.until(ws, "turn_start", log)
        gate[1].set()
        idle = await self.until(ws, "turn_done", log)
        self.assertEqual((idle["turn"], idle["idle"], idle["given_back"]), (1, True, True))
        self.assertEqual(idle["cancelled"], "superseded", "the shared response is drained before the next turn")
        self.assertNotIn("audio", log, "a line the person talked before is never played over them")
        self.assertNotIn("did not play", str(self.live.active.voice.sent))
        self.assertEqual(t3.given, [DONE.acts])
        await self.send(ws, noise(900, seed=1))
        while not (done := await self.until(ws, "turn_done", log)).get("idle"):
            pass
        self.assertEqual((done["turn"], done["given_back"], done["replies"]), (3, False, [DONE_LINE]),
                         "the line waits for the next quiet moment and is said then")
        self.assertTrue(self.record(1)["given_back"])
        await ws.close()

    async def test_talking_over_an_idle_line_barges_in(self):
        self.heard("unused")
        self.news(DONE, audio=bytes(OUT_RATE * 2 * 2))
        ws = await self.connect()
        log = []
        await self.send(ws, noise(500))
        self.live.news.set()
        await self.until(ws, "reply_start", log)
        await self.send(ws, tone(800))
        barge = await self.until(ws, "barge_in", log)
        self.assertEqual(barge["turn"], 1, "the idle line is the reply that stops")
        self.assertIn("duck", log)
        started = await self.until(ws, "turn_start", log)
        self.assertEqual(started["turn"], 2)
        await self.send(ws, noise(900, seed=2))
        while (done := await self.until(ws, "turn_done", log))["turn"] != 2:
            pass
        record = self.record(2)
        self.assertEqual(record["live"]["overlap"]["verdict"], "barge_in")
        self.assertIsNotNone(record["live"]["barge_in"], "the barge-in records what was heard of the idle line")
        self.assertFalse(self.record(1).get("given_back"), "its whole audio was sent before the barge-in")
        await ws.close()


class SpeakTest(unittest.IsolatedAsyncioTestCase):
    """The live transport half of the fact check: the server speaks only what the turn hands it after the check, and
    a reply the person talked over is not sent at all."""

    async def conversation(self):
        live = SimpleNamespace(settings=server.Settings(silence_ms=700, start_ms=100, preroll_ms=300,
                                                        floor_dbfs=-50.0, margin_db=10.0, max_turn_s=30.0),
                               turns=0)
        return server.Conversation(live, SimpleNamespace(send=None), secret=SECRET)

    async def test_a_checked_reply_is_sent_start_audio_end(self):
        conversation = await self.conversation()
        turn = server.MicTurn(1, b"")
        turn.out_rate = OUT_RATE
        conversation.speak(turn, "I don't have the day for that yet.", b"\x01\x00" * OUT_RATE, True)
        kinds, frames = self.drain(conversation)
        self.assertEqual(kinds[:2], ["reply_start", "say"], "the caption goes out just before the audio it says")
        self.assertEqual(kinds[-1], "reply_end")
        self.assertEqual(set(kinds[2:-1]), {"audio"})
        self.assertEqual(frames, OUT_RATE * 2, "one second of 16-bit audio")

    def drain(self, conversation):
        """What the client would get from the outbox: the send loop drops a muted turn's audio."""
        kinds, frames, self.captions = [], 0, []
        while not conversation.outbox.empty():
            turn, payload = conversation.outbox.get_nowait()
            if isinstance(payload, server.AudioFrame):
                caption, payload = payload.caption, payload.pcm
                if turn is not None and turn.muted:
                    continue
                if caption is not None:
                    self.captions.append(json.loads(caption))
                    kinds.append(self.captions[-1]["type"])
                kinds.append("audio")
                frames += len(payload)
                self.assertLessEqual(len(payload), OUT_RATE * 2 // 25, "no frame longer than 40 ms")
            else:
                kinds.append(json.loads(payload)["type"])
        return kinds, frames

    async def test_a_reply_released_in_pieces_is_one_reply_on_the_wire(self):
        conversation = await self.conversation()
        turn = server.MicTurn(1, b"")
        turn.out_rate = OUT_RATE
        conversation.speak(turn, "It's sunny", b"\x01\x00" * (OUT_RATE // 2), False)
        conversation.speak(turn, "It's sunny in Denver", b"\x01\x00" * (OUT_RATE // 2), False)
        conversation.speak(turn, "It's sunny in Denver tomorrow.", b"", True)
        kinds, frames = self.drain(conversation)
        self.assertEqual((kinds.count("reply_start"), kinds.count("reply_end")), (1, 1))
        self.assertEqual((kinds[0], kinds[-1]), ("reply_start", "reply_end"))
        self.assertEqual(frames, OUT_RATE * 2, "both pieces, one second in all")
        self.assertEqual(conversation.playback.text, "It's sunny in Denver", "the echo check hears each word once")
        self.assertEqual(self.captions, [{"type": "say", "turn": 1, "text": "It's sunny"},
                                         {"type": "say", "turn": 1, "text": " in Denver"}],
                         "each caption is the text its audio adds, and the end with no audio has none")

    async def test_a_turn_muted_mid_reply_sends_no_more_audio_and_still_ends_the_reply(self):
        conversation = await self.conversation()
        turn = server.MicTurn(1, b"")
        turn.out_rate = OUT_RATE
        conversation.speak(turn, "It's sunny", b"\x01\x00" * (OUT_RATE // 2), False)
        turn.mute("barge_in")
        conversation.speak(turn, "It's sunny in Denver", b"\x01\x00" * (OUT_RATE // 2), False)
        conversation.speak(turn, "It's sunny in Denver.", b"", True)
        kinds, _ = self.drain(conversation)
        self.assertEqual(kinds, ["reply_start", "reply_end"], "the muted turn's audio is dropped, its end is not")

    async def test_a_muted_turn_says_nothing(self):
        conversation = await self.conversation()
        turn = server.MicTurn(1, b"")
        turn.out_rate = OUT_RATE
        turn.muted = True
        conversation.speak(turn, "I'm running the task for Sunday.", b"\x01\x00" * OUT_RATE, True)
        self.assertTrue(conversation.outbox.empty(), "the person already talked over this reply")

    async def test_a_t3_line_muted_after_one_frame_records_only_the_sent_audio(self):
        conversation = await self.conversation()
        turn = server.MicTurn(1, b"")
        turn.out_rate = OUT_RATE
        frames, sent = [], []
        async def send(payload):
            sent.append("audio" if isinstance(payload, bytes) else json.loads(payload))
            if isinstance(payload, bytes):
                frames.append(payload)
                turn.mute("barge_in")

        conversation.ws.send = send
        sender = asyncio.create_task(conversation._send_loop())
        try:
            line = Speech(None, lambda thread_id: None, F.Agree()).accept(DONE.acts, DONE_LINE, "text_model").line
            pcm, first_sent = await conversation.speak_t3(turn, line, bytes(9600))
            self.assertEqual(frames, [pcm])
            captions = [m for m in sent if m != "audio" and m["type"] == "say"]
            self.assertEqual(captions, [{"type": "say", "turn": 1, "text": DONE_LINE, "relay": True}],
                             "a T3 line is captioned once, as a relay, and nothing after the barge-in")
            self.assertLess(sent.index(captions[0]), sent.index("audio"), "the caption goes out before its audio")
            self.assertEqual(len(pcm), 1920)
            self.assertIsNotNone(first_sent)
        finally:
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)

    async def test_a_plain_string_never_reaches_the_t3_reply_path(self):
        conversation = await self.conversation()
        turn = server.MicTurn(1, b"")
        turn.out_rate = OUT_RATE
        with self.assertRaises(TypeError):
            await conversation.speak_t3(turn, DONE_LINE, b"\x01\x00" * OUT_RATE)
        self.assertTrue(conversation.outbox.empty())
        self.assertFalse(turn.reply_started)

    async def test_a_delta_on_the_session_alone_sends_no_audio(self):
        """The voice session is not the transport any more: only a checked reply reaches the client."""
        conversation = await self.conversation()
        conversation.voice_turn = server.MicTurn(1, b"")
        conversation.on_voice_event({"type": "response.output_audio.delta", "delta": "AAAA"})
        conversation.on_voice_event({"type": "response.done"})
        self.assertTrue(conversation.outbox.empty())
        self.assertFalse(conversation.voice_turn.reply_started, "nothing was spoken")

    async def two_replies(self, said=()):
        """A turn that spoke an answer and then a task update, one second each, and a read-back that answers in turn
        with `said`."""
        conversation = await self.conversation()
        turn = server.MicTurn(1, b"")
        turn.out_rate = OUT_RATE
        conversation.speak(turn, "The forecast is sunny.", b"\x01\x00" * OUT_RATE, True)
        conversation.speak(turn, "The task found three results.", b"\x01\x00" * OUT_RATE, True)
        read_back, said = [], list(said)

        async def hear(pcm, rate):
            read_back.append(len(pcm) / 2 / rate)
            return said.pop(0)

        conversation.live.hear_reply = hear
        return conversation, turn, read_back

    async def test_the_read_back_is_matched_against_the_replies_that_played_in_order(self):
        said = ["The forecast", "The forecast is sunny. The task found"]
        conversation, turn, read_back = await self.two_replies(said)
        first = await conversation.heard_before(turn, 1.5, False)
        self.assertEqual(first["heard"], "The forecast", "the answer's audio is matched against the answer, not the "
                                                         "task update that followed it")
        second = await conversation.heard_before(turn, 0.5, False)
        self.assertEqual(second["heard"], "The forecast is sunny. The task found", "both replies, in order")
        self.assertEqual(read_back, [0.5, 1.5], "only the audio the client had played")
        self.assertEqual((first["whole"], first["played_s"]), (False, 0.5))

    async def test_a_reply_that_had_finished_playing_was_heard_whole(self):
        conversation, turn, read_back = await self.two_replies()
        heard = await conversation.heard_before(turn, 0.0, False)
        self.assertEqual((heard["heard"], heard["whole"], heard["played_s"]),
                         ("The forecast is sunny. The task found three results.", True, 2.0))
        self.assertEqual(read_back, [], "nothing to read back")
        self.assertIsNone(server.note_for(heard), "a note saying it heard only the start would be false")

    async def test_nothing_played_or_nothing_matched_is_no_note(self):
        conversation, turn, read_back = await self.two_replies(["Something else entirely"])
        silent = await conversation.heard_before(turn, 2.0, False)
        self.assertEqual((silent["heard"], silent["played_s"], read_back), ("", 0.0, []))
        unmatched = await conversation.heard_before(turn, 1.0, False)
        self.assertEqual(unmatched["heard"], "")
        self.assertIsNone(server.note_for(silent))
        self.assertIsNone(server.note_for(unmatched))
        never_spoke = server.MicTurn(2, b"")
        self.assertIsNone(await conversation.heard_before(never_spoke, 0.0, False), "a turn that never spoke")

    async def test_a_failed_read_back_is_recorded_as_unknown(self):
        conversation, turn, _ = await self.two_replies()

        async def hear(pcm, rate):
            raise ConnectionError("asr down")

        conversation.live.hear_reply = hear
        heard = await conversation.heard_before(turn, 1.0, False)
        self.assertIsNone(heard["heard"])
        self.assertIn("asr down", heard["error"])
        self.assertIsNone(server.note_for(heard))

    async def test_a_read_back_that_takes_too_long_is_recorded_as_unknown(self):
        conversation, turn, _ = await self.two_replies()

        async def hear(pcm, rate):
            await asyncio.Event().wait()

        conversation.live.hear_reply = hear
        with mock.patch.object(server, "READBACK_TIMEOUT_S", 0.05, create=True):
            heard = await asyncio.wait_for(conversation.heard_before(turn, 1.0, False), 2)
        self.assertIsNone(heard["heard"])
        self.assertIn("timed out", heard["error"])
        self.assertIsNone(server.note_for(heard))


class ChunkTest(unittest.IsolatedAsyncioTestCase):
    async def test_upstream_audio_goes_to_the_asr_in_40_ms_chunks_and_to_the_voice_once_at_the_end(self):
        class Recorder(FakeSession):
            def __init__(self):
                super().__init__()
                self.chunks = []

            async def send(self, **event):
                if "audio" in event:
                    self.chunks.append(len(base64.b64decode(event["audio"])))

        turn = server.MicTurn(1, tone(200))
        turn.end(700)
        voice, asr = Recorder(), Recorder()
        await turn.stream(voice, asr)
        self.assertEqual(server.UP_CHUNK_BYTES, 1280, "40 ms of 16 kHz PCM16")
        self.assertEqual(max(asr.chunks), server.UP_CHUNK_BYTES)
        self.assertEqual(sum(asr.chunks), len(turn.pcm))
        self.assertEqual(voice.chunks, [], "a turn that is cancelled while streaming leaves the voice session empty")
        await turn.send_voice(voice)
        self.assertEqual(sum(voice.chunks), len(turn.pcm), "the voice session gets the whole turn when it ends")


class DefaultsTest(unittest.TestCase):
    def test_the_local_session_words_t3_lines_whichever_model_writes_the_summaries(self):
        from voice.live import t3_tasks
        from voice.t3.realtime_writer import RealtimeWriter

        with mock.patch.dict("os.environ", {"VOICE_RENDERER": "", "VOICE_PAID_TEXT_MODEL": ""}):
            self.assertIsInstance(server.live_writer("ws://unused"), RealtimeWriter)
            with mock.patch.dict("os.environ", {"VOICE_PAID_TEXT_MODEL": "paid/model", "VOICE_ENV_FILE": "unused"}), \
                    mock.patch.object(t3_tasks, "LiveClient") as cloud:
                self.assertIsInstance(server.live_writer("ws://unused"), RealtimeWriter)
                self.assertIs(server.default_model(), cloud.return_value)
            with mock.patch.dict("os.environ", {"VOICE_RENDERER": "particle-1"}):
                self.assertIsNone(server.live_writer("ws://unused"))

    def test_live_speech_has_no_tts_mode_dependency(self):
        from voice.t3 import realtime_writer

        for module in (server, prototype_run, realtime_writer):
            with self.subTest(module=module.__name__):
                source = inspect.getsource(module)
                for tts in ("/v1/audio/speech", "/tts", "from voice.live import script", "voice.live.fallback", "tts_line"):
                    self.assertNotIn(tts, source)

    def test_t3_can_start_with_any_snapshot_mode(self):
        for mode in ("note", "reply-no-utterance", "reply", "before-audio", "off"):
            with self.subTest(mode=mode), mock.patch("sys.argv", ["server", "--t3", "on", "--snapshot", mode]), \
                    mock.patch.object(server, "main_async", new=mock.Mock()) as start, \
                    mock.patch.object(server.asyncio, "run"):
                server.main()
                self.assertEqual(start.call_args.args[0].snapshot, mode)

    def test_live_mode_sends_only_a_lookup_note_unless_told_otherwise(self):
        argv = mock.patch("sys.argv", ["server"])
        with argv, mock.patch.object(server, "main_async", new=mock.Mock()) as main_async, mock.patch.object(server.asyncio, "run"):
            server.main()
        self.assertEqual(main_async.call_args.args[0].snapshot, "note")

    def test_the_note_persona_is_the_default_without_a_prompt_file(self):
        self.assertEqual(server.load_persona(None, server.NOTE_PERSONA), (server.NOTE_PERSONA, "built-in default"))

    def test_live_mode_names_the_turn_a_question_asks_about_unless_told_otherwise(self):
        argv = mock.patch("sys.argv", ["server"])
        with argv, mock.patch.object(server, "main_async", new=mock.Mock()) as main_async, mock.patch.object(server.asyncio, "run"):
            server.main()
        self.assertEqual(main_async.call_args.args[0].recall, "note")


class BindTest(unittest.TestCase):
    def test_only_tailnet_or_loopback(self):
        for host in ("100.64.0.1", "127.0.0.1"):
            self.assertEqual(server.check_bind(host), host)
        for host in ("0.0.0.0", "192.168.0.10", "10.0.0.5"):
            with self.assertRaises(SystemExit):
                server.check_bind(host)

    def test_the_secret_is_made_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "live" / "secret"
            first = server.shared_secret(path)
            self.assertEqual(len(first), 64)
            self.assertEqual(server.shared_secret(path), first)

    def test_the_prompt_file_replaces_the_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "prompt.txt"
            self.assertEqual(server.load_persona(path), (server.PERSONA, "built-in default"))
            path.write_text("Be brief.\n", encoding="utf-8")
            text, label = server.load_persona(path)
            self.assertEqual(text, "Be brief.")
            self.assertTrue(label.startswith("prompt.txt sha256:"))


class LlamaServerSlotsTest(unittest.IsolatedAsyncioTestCase):
    """One llama-server slot holds the call's voice session for the whole call, so a call needs two more: the turn's
    ASR and the reply it generates. With one free slot (two in total) the ASR waited behind the previous turn's reply
    and its final transcript arrived tens of seconds late (measured: 2026-11-16 17:38 replay)."""

    def test_each_extra_asr_lane_gets_its_own_slot(self):
        self.assertEqual([server.slots(n) for n in (1, 2, 4)], [3, 4, 6],
                         "the voice session, one slot per ASR lane, and one for the reply")

    async def test_the_call_gets_a_slot_for_its_session_its_asr_and_its_reply(self):
        recorded = {}

        class FakeServer:
            startup_s = 0.0
            proc = SimpleNamespace(pid=1)

            def __init__(self, port, log_path, extra_args, slots):
                recorded["slots"], recorded["args"] = slots, tuple(extra_args)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        class FakeServe:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        @contextlib.contextmanager
        def fake_decider(name, out):
            yield SimpleNamespace(live_calls=lambda: 0)

        async def stop_now(*args, **kwargs):
            return None

        with tempfile.TemporaryDirectory() as tmp:
            args = SimpleNamespace(host="127.0.0.1", port=8799, lfm_port=8119, lfm_ctx=32768, lfm_args=None,
                                   stop_file=Path(tmp) / "STOP", decider_cache=Path(tmp) / "cache",
                                   decider_budget=200, out=Path(tmp) / "out", max_minutes=1.0, mock_tools=True,
                                   decider="liquid", snapshot="note", prompt=None, silence_ms=700, start_ms=100,
                                   preroll_ms=300, floor_dbfs=-50.0, margin_db=10.0, max_turn_s=30.0)
            with mock.patch.object(server, "Server", FakeServer), \
                    mock.patch.object(server, "serve", FakeServe), \
                    mock.patch.object(server, "make_decider", fake_decider), \
                    mock.patch.object(server, "wait_for_stop", stop_now), \
                    mock.patch.object(server, "shared_secret", lambda: "s" * 64), \
                    mock.patch.object(server, "use_decider_cache", lambda *a, **k: None):
                await server.main_async(args)
        self.assertGreaterEqual(recorded["slots"], 3)
        self.assertIn("-kvu", recorded["args"])


if __name__ == "__main__":
    unittest.main()
