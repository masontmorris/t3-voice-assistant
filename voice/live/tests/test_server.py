import asyncio
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

SECRET = "s" * 64
OUT_RATE = 24_000


class FakeSession:
    def __init__(self):
        self.audio_bytes = 0

    def clock(self):
        return 0.0

    async def send(self, **event):
        self.audio_bytes += len(event.get("audio", ""))


async def fake_run_clip(source, *args, persona, on_voice_event, **kwargs):
    """Streams the turn's input, then sends 2 s of reply audio in 20 ms deltas after a 0.3 s think."""
    on_voice_event({"type": "session.created", "session": {"audio": {"output": {"format": {"rate": OUT_RATE}}}}})
    await source.stream(FakeSession(), FakeSession())
    await asyncio.sleep(0.3)
    delta = __import__("base64").b64encode(b"\x01\x00" * (OUT_RATE // 50)).decode()
    for _ in range(100):
        on_voice_event({"type": "response.output_audio.delta", "delta": delta})
        await asyncio.sleep(0.02)
    on_voice_event({"type": "response.done"})
    return {"clip": source.name, "final_transcript": "hello", "replies": [{"text": "hi"}], "metrics": {}}


class LiveServerTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.frames = []
        self.tmp = tempfile.TemporaryDirectory()
        args = SimpleNamespace(silence_ms=700, start_ms=100, preroll_ms=300, floor_dbfs=-50.0, margin_db=10.0,
                               max_turn_s=30.0, prompt=Path(self.tmp.name) / "prompt.txt", delay=4.0,
                               segmenter="hybrid")
        self.live = server.Live(args, "ws://unused", decider=None, tools=None, out=Path(self.tmp.name))
        patch = mock.patch.object(server, "run_clip", fake_run_clip)
        patch.start()
        self.addCleanup(patch.stop)
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
        self.assertTrue((Path(self.tmp.name) / "turn_001.json").is_file())
        await ws.close()

    async def test_speech_during_the_reply_interrupts_it(self):
        ws = await self.connect()
        log = []
        await self.send(ws, noise(500) + tone(600) + noise(900, seed=1))
        await self.until(ws, "reply_start", log)
        await self.send(ws, tone(300))
        barge = await self.until(ws, "barge_in", log)
        self.assertEqual(barge["turn"], 1)
        after = []
        started = await self.until(ws, "turn_start", after)
        self.assertEqual(started["turn"], 2)
        await self.send(ws, noise(900, seed=2))
        await self.until(ws, "turn_end", after)
        self.assertNotIn("audio", after[:after.index("turn_start")], "no audio from turn 1 after barge_in")
        record = json.loads((Path(self.tmp.name) / "turn_001.json").read_text())
        self.assertEqual(record["live"]["cancelled"], "barge_in")
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

    async def test_a_second_client_is_turned_away(self):
        first = await self.connect()
        second = await websockets.connect(self.url, additional_headers={server.HEADER: SECRET})
        with self.assertRaises(websockets.ConnectionClosed):
            await second.recv()
        self.assertEqual(second.close_code, 1013)
        await first.close()


class BindTest(unittest.TestCase):
    def test_only_tailnet_or_loopback(self):
        for host in ("100.86.25.114", "127.0.0.1"):
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
