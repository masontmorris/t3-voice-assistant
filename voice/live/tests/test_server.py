import asyncio
import base64
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import websockets
from websockets.asyncio.server import serve

from voice.live import server
from voice.live.tests.test_endpoint import noise, tone
from voice.prototype.run import run_clip

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

    @classmethod
    async def open(cls, url, name, clock, on_event=None):
        return cls(name, on_event)

    async def send(self, **event):
        self.sent.append(event)

    async def wait_for(self, *types, after=0, timeout=120):
        if "session.created" in types:
            return {"type": "session.created",
                    "session": {"audio": {"output": {"format": {"rate": OUT_RATE}}}}}
        return {"type": "conversation.item.added", "item": {}}

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
    await asyncio.sleep(0.3)
    speak(f"reply to {source.name}", b"\x01\x00" * (OUT_RATE * 2))
    return {"clip": source.name, "final_transcript": f"words in {source.name}",
            "replies": [{"text": f"reply to {source.name}"}], "metrics": {},
            "snapshot": {"final_transcript": f"state after {source.name}"}}


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
                               segmenter="hybrid", call_max_turns=8, call_keep_turns=6, snapshot="reply")
        self.live = server.Live(args, "ws://unused", decider=None, tools=None, out=Path(self.tmp.name))
        self.patch = mock.patch.object(server, "run_clip", fake_run_clip)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        session_patch = mock.patch.object(server.Session, "open", FakeVoice.open)
        session_patch.start()
        self.addCleanup(session_patch.stop)
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
        self.assertEqual(second["live"]["call"], {"turn": 2, "session_turn": 2, "voice_session": "shared",
                                                  "reopened": None, "voice_sessions": 1})
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

    async def test_a_long_call_seeds_a_new_session_with_the_call_so_far(self):
        self.live.args.call_max_turns = 1
        ws = await self.connect()
        await self.send(ws, noise(500) + tone(600) + noise(900, seed=1))
        await self.until(ws, "turn_done", [])
        await self.send(ws, noise(500) + tone(600) + noise(900, seed=2))
        await self.until(ws, "turn_done", [])
        self.assertEqual(len(VOICES), 2)
        self.assertIsNot(VOICES[0], VOICES[1], "the second turn starts a new session")
        self.assertTrue(VOICES[0].closed, "the old session is closed")
        self.assertEqual(SENT_BEFORE[1], [], "nothing goes into the new session before run_clip sends its settings")
        self.assertIsNone(CALLS[0][-1]["seed"], "the call's first session has nothing to carry")
        seed = CALLS[1][-1]["seed"]["content"][0]["text"]
        self.assertTrue(seed.startswith(server.SEED_PREFIX))
        self.assertIn("words in turn_001", seed, "the call's own text carries over")
        record = json.loads((Path(self.tmp.name) / "turn_002.json").read_text())
        self.assertEqual(record["live"]["call"]["voice_session"], "started")
        self.assertEqual(record["live"]["call"]["reopened"], "turn limit")
        self.assertEqual(record["live"]["call"]["voice_sessions"], 2)
        await ws.close()

    async def test_a_new_prompt_mid_call_starts_a_new_session(self):
        prompt = Path(self.tmp.name) / "prompt.txt"
        prompt.write_text("Be brief.\n", encoding="utf-8")
        ws = await self.connect()
        await self.send(ws, noise(500) + tone(600) + noise(900, seed=1))
        await self.until(ws, "turn_done", [])
        prompt.write_text("Be warm.\n", encoding="utf-8")
        await self.send(ws, noise(500) + tone(600) + noise(900, seed=2))
        await self.until(ws, "turn_done", [])
        self.assertIsNot(VOICES[0], VOICES[1], "llama.cpp keeps the old instructions on a session that holds audio")
        self.assertTrue(VOICES[0].closed)
        self.assertTrue(PERSONAS[1].startswith("Be warm."))
        self.assertIn("words in turn_001", CALLS[1][-1]["seed"]["content"][0]["text"], "the call carries over")
        record = json.loads((Path(self.tmp.name) / "turn_002.json").read_text())
        self.assertEqual(record["live"]["call"]["reopened"], "persona changed")
        self.assertEqual(record["live"]["call"]["voice_sessions"], 2)
        await ws.close()

    async def test_speech_during_the_reply_stops_playback_and_keeps_the_call(self):
        ws = await self.connect()
        log = []
        await self.send(ws, noise(500) + tone(600) + noise(900, seed=1))
        await self.until(ws, "reply_start", log)
        await self.send(ws, tone(300))
        barge = await self.until(ws, "barge_in", log)
        self.assertEqual(barge["turn"], 1, "the reply the person talked over is the one flushed")
        self.assertIn("reply_end", log, "the whole reply was handed to the client before it was flushed")
        after = []
        started = await self.until(ws, "turn_start", after)
        self.assertEqual(started["turn"], 2)
        await self.send(ws, noise(900, seed=2))
        await self.until(ws, "turn_end", after)
        while (done := await self.until(ws, "turn_done", after))["turn"] != 2:
            pass
        self.assertFalse(VOICES[0].closed, "a barge-in keeps the call's session, and its history, open")
        self.assertIs(VOICES[0], VOICES[1], "the next turn continues on the same session")
        await ws.close()

    async def test_speech_before_the_reply_continues_the_turn(self):
        ws = await self.connect()
        log = []
        await self.send(ws, noise(500) + tone(600) + noise(720, seed=1))
        await self.until(ws, "turn_end", log)
        await self.send(ws, tone(300))
        started = await self.until(ws, "turn_start", log)
        self.assertEqual(started["turn"], 2)
        self.assertGreater(started["carried_audio_s"], 1.0, "turn 2 starts with turn 1's audio")
        self.assertNotIn("barge_in", log)
        await ws.close()

    async def test_speech_after_the_reply_stops_playback(self):
        ws = await self.connect()
        await self.send(ws, noise(500) + tone(600) + noise(900, seed=1))
        await self.until(ws, "turn_done", [])
        log = []
        await self.send(ws, tone(400))
        barge = await self.until(ws, "barge_in", log)
        self.assertEqual(barge["turn"], 1, "the finished turn's audio is dropped once speech starts")
        started = await self.until(ws, "turn_start", log)
        self.assertEqual(started["turn"], 2)
        self.assertEqual(log, ["barge_in", "turn_start"], "stop playback before the new turn starts")
        await ws.close()

    async def test_a_second_client_is_turned_away(self):
        first = await self.connect()
        second = await websockets.connect(self.url, additional_headers={server.HEADER: SECRET})
        with self.assertRaises(websockets.ConnectionClosed):
            await second.recv()
        self.assertEqual(second.close_code, 1013)
        await first.close()


class SpeakTest(unittest.IsolatedAsyncioTestCase):
    """The live transport half of the fact check: the server speaks only what the turn hands it after the check, and
    a reply the person talked over is not sent at all."""

    async def conversation(self):
        live = SimpleNamespace(settings=server.Settings(silence_ms=700, start_ms=100, preroll_ms=300,
                                                        floor_dbfs=-50.0, margin_db=10.0, max_turn_s=30.0),
                               turns=0)
        conversation = server.Conversation(live, SimpleNamespace(send=None))
        conversation.sender.cancel()
        self.addAsyncCleanup(asyncio.gather, conversation.sender, return_exceptions=True)
        return conversation

    async def test_a_checked_reply_is_sent_start_audio_end(self):
        conversation = await self.conversation()
        turn = server.MicTurn(1, b"")
        turn.out_rate = OUT_RATE
        conversation.speak(turn, "I don't have the day for that yet.", b"\x01\x00" * OUT_RATE)
        kinds, frames = [], 0
        while not conversation.outbox.empty():
            _, payload = conversation.outbox.get_nowait()
            if isinstance(payload, bytes):
                kinds.append("audio")
                frames += len(payload)
                self.assertLessEqual(len(payload), OUT_RATE * 2 // 25, "no frame longer than 40 ms")
            else:
                kinds.append(json.loads(payload)["type"])
        self.assertEqual(kinds[0], "reply_start")
        self.assertEqual(kinds[-1], "reply_end")
        self.assertEqual(set(kinds[1:-1]), {"audio"})
        self.assertEqual(frames, OUT_RATE * 2, "one second of 16-bit audio")

    async def test_a_muted_turn_says_nothing(self):
        conversation = await self.conversation()
        turn = server.MicTurn(1, b"")
        turn.out_rate = OUT_RATE
        turn.muted = True
        conversation.speak(turn, "I'm running the task for Sunday.", b"\x01\x00" * OUT_RATE)
        self.assertTrue(conversation.outbox.empty(), "the person already talked over this reply")

    async def test_a_delta_on_the_session_alone_sends_no_audio(self):
        """The voice session is not the transport any more: only a checked reply reaches the client."""
        conversation = await self.conversation()
        conversation.voice_turn = server.MicTurn(1, b"")
        conversation.on_voice_event({"type": "response.output_audio.delta", "delta": "AAAA"})
        conversation.on_voice_event({"type": "response.done"})
        self.assertTrue(conversation.outbox.empty())
        self.assertFalse(conversation.voice_turn.reply_started, "nothing was spoken")


class ChunkTest(unittest.IsolatedAsyncioTestCase):
    async def test_upstream_audio_goes_to_llama_in_40_ms_chunks(self):
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
        self.assertEqual(max(voice.chunks), server.UP_CHUNK_BYTES)
        self.assertEqual(sum(voice.chunks), len(turn.pcm))
        self.assertEqual(voice.chunks, asr.chunks, "the voice and ASR sessions get the same chunks")


class DefaultsTest(unittest.TestCase):
    def test_live_mode_drops_the_persons_words_unless_told_otherwise(self):
        argv = mock.patch("sys.argv", ["server"])
        with argv, mock.patch.object(server, "main_async") as main_async, mock.patch.object(server.asyncio, "run"):
            server.main()
        self.assertEqual(main_async.call_args.args[0].snapshot, "reply-no-utterance")


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


if __name__ == "__main__":
    unittest.main()
