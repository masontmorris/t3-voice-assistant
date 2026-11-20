"""The browser path: the page over GET /, and the auth frame in place of the header."""

import asyncio
import base64
import json
import tempfile
import unittest
import urllib.request
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np
import websockets
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

from voice.live import server
from voice.live.tests import test_server
from voice.live.tests.test_endpoint import noise, tone


def make_live(tmp):
    args = SimpleNamespace(silence_ms=700, start_ms=100, preroll_ms=300, floor_dbfs=-50.0, margin_db=10.0,
                           max_turn_s=30.0, prompt=Path(tmp) / "prompt.txt", delay=4.0,
                           segmenter="hybrid", snapshot="reply", recall="note")
    return server.Live(args, "ws://unused", decider=None, tools=None, out=Path(tmp),
                       secret=test_server.SECRET)


class BrowserPathTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.live = make_live(self.tmp.name)
        self.patch = mock.patch.object(server, "run_clip", test_server.fake_run_clip)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        session_patch = mock.patch.object(server.Session, "open", test_server.FakeVoice.open)
        session_patch.start()
        self.addCleanup(session_patch.stop)
        self.ws_server = await serve(self.live.handler, "127.0.0.1", 0,
                                     process_request=server.authorize(test_server.SECRET))
        port = self.ws_server.sockets[0].getsockname()[1]
        self.url = f"ws://127.0.0.1:{port}"
        self.http = f"http://127.0.0.1:{port}"

    async def asyncTearDown(self):
        self.ws_server.close()
        await self.ws_server.wait_closed()
        self.tmp.cleanup()

    def get(self, path):
        with urllib.request.urlopen(self.http + path, timeout=5) as response:
            return response.status, response.headers, response.read().decode("utf-8")

    async def browser(self):
        return await websockets.connect(self.url, additional_headers={"Origin": "http://example.com"})

    async def send(self, ws, pcm):
        for offset in range(0, len(pcm), 640):
            await ws.send(pcm[offset:offset + 640])

    async def until(self, ws, kind, log):
        while True:
            message = await asyncio.wait_for(ws.recv(), 10)
            log.append("audio" if isinstance(message, bytes) else json.loads(message)["type"])
            if log[-1] == kind:
                return json.loads(message)

    async def test_get_root_serves_the_page(self):
        for path in ("/", "/?debug", "/?anything"):
            with self.subTest(path=path):
                status, headers, body = await asyncio.to_thread(self.get, path)
                self.assertEqual((status, headers.get_content_type()), (200, "text/html"))
                self.assertIn("<title>Voice call</title>", body)
                self.assertIn('src="app.js"', body)
                self.assertNotIn(test_server.SECRET, body, "the page carries no secret")

    async def test_get_script_paths_serve_javascript(self):
        for path in ("/app.js", "/resample.js", "/capture-worklet.js", "/playback-worklet.js"):
            status, headers, body = await asyncio.to_thread(self.get, path)
            self.assertEqual((status, headers.get_content_type()), (200, "text/javascript"), path)
            self.assertTrue(body.strip(), path)

    async def test_page_and_scripts_restrict_content_to_the_live_origin(self):
        for path in ("/", "/app.js", "/resample.js", "/capture-worklet.js", "/playback-worklet.js"):
            with self.subTest(path=path):
                _, headers, _ = await asyncio.to_thread(self.get, path)
                self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
                directives = dict(part.split(maxsplit=1) for part in headers["Content-Security-Policy"].split("; "))
                self.assertEqual(directives["default-src"], "'none'")
                self.assertEqual(directives["script-src"], "'self'")
                self.assertEqual(directives["style-src"], "'self'")
                self.assertCountEqual(directives["connect-src"].split(),
                                      ["'self'", self.url, self.url.replace("ws:", "wss:")])
                self.assertEqual(directives["frame-ancestors"], "'none'")

    def test_live_and_conversation_require_an_explicit_secret(self):
        with self.assertRaisesRegex(TypeError, "secret"):
            server.Live(self.live.args, "ws://unused", None, None, Path(self.tmp.name))
        with self.assertRaisesRegex(TypeError, "secret"):
            server.Conversation(self.live, SimpleNamespace(send=None))

    async def test_get_unknown_path_is_not_found(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            await asyncio.to_thread(self.get, "/nope")
        self.assertEqual(caught.exception.code, 404)

    async def test_a_good_auth_frame_is_accepted(self):
        ws = await self.browser()
        await ws.send(f"auth {test_server.SECRET}")
        await ws.send(json.dumps({"type": "hello", "mode": "vad"}))
        self.assertEqual(json.loads(await ws.recv())["type"], "ready")
        log = []
        await self.send(ws, noise(500) + tone(600) + noise(900, seed=1))
        done = await self.until(ws, "turn_done", log)
        self.assertEqual(done["transcript"], "words in turn_001")
        await ws.close()

    async def test_a_bad_auth_frame_closes_the_socket(self):
        ws = await self.browser()
        await ws.send("auth wrong")
        with self.assertRaises(ConnectionClosed) as caught:
            await ws.recv()
        self.assertEqual(caught.exception.code, 1008)

    async def test_audio_before_auth_closes_the_socket(self):
        ws = await self.browser()
        await ws.send(noise(500))
        with self.assertRaises(ConnectionClosed) as caught:
            await ws.recv()
        self.assertEqual(caught.exception.code, 1008)

    async def test_a_late_auth_frame_closes_the_socket(self):
        with mock.patch.object(server, "AUTH_TIMEOUT_S", 0.2):
            ws = await self.browser()
            with self.assertRaises(ConnectionClosed) as caught:
                await ws.recv()
            self.assertEqual(caught.exception.code, 1008)

    async def test_the_header_path_needs_no_auth_frame(self):
        ws = await websockets.connect(self.url, additional_headers={server.HEADER: test_server.SECRET})
        await ws.send(json.dumps({"type": "hello", "mode": "vad"}))
        self.assertEqual(json.loads(await ws.recv())["type"], "ready")
        await ws.close()

    async def test_silent_browser_leaves_room_for_a_header_client(self):
        async with await self.browser():
            async with websockets.connect(self.url, additional_headers={server.HEADER: test_server.SECRET}) as ws:
                await ws.send(json.dumps({"type": "hello", "mode": "vad"}))
                self.assertEqual(json.loads(await asyncio.wait_for(ws.recv(), 2))["type"], "ready")

    async def test_silent_browser_leaves_room_for_an_authenticated_browser(self):
        async with await self.browser():
            async with await self.browser() as ws:
                await ws.send(f"auth {test_server.SECRET}")
                await ws.send(json.dumps({"type": "hello", "mode": "vad"}))
                self.assertEqual(json.loads(await asyncio.wait_for(ws.recv(), 2))["type"], "ready")

    async def test_busy_status_requires_valid_browser_auth(self):
        async with websockets.connect(self.url, additional_headers={server.HEADER: test_server.SECRET}) as active:
            await active.send(json.dumps({"type": "hello", "mode": "vad"}))
            self.assertEqual(json.loads(await asyncio.wait_for(active.recv(), 2))["type"], "ready")
            for secret, code, reason in (("wrong", 1008, "bad or missing auth"),
                                         (test_server.SECRET, 1013, "another client is connected")):
                async with await self.browser() as ws:
                    await ws.send(f"auth {secret}")
                    with self.assertRaises(ConnectionClosed) as caught:
                        await asyncio.wait_for(ws.recv(), 2)
                    self.assertEqual((caught.exception.rcvd.code, caught.exception.rcvd.reason), (code, reason))

    async def test_a_header_secret_with_an_origin_is_still_refused(self):
        with self.assertRaises(websockets.InvalidStatus):
            await websockets.connect(self.url, additional_headers={server.HEADER: test_server.SECRET,
                                                                    "Origin": "http://example.com"})

    async def call(self, *, record=True, header=False, hangup=True, reported=(), tone_ms=600):
        """One call over the socket: hello, speech, the page's `played` reports, then a hangup or a drop. Returns
        the microphone samples sent, the `ready` message and the path of the call's finalized manifest."""
        finished = asyncio.Event()
        original = server.Conversation.finish_recording

        async def finish_recording(conversation):
            await original(conversation)
            finished.set()

        mic = noise(500) + tone(tone_ms) + noise(900, seed=1)
        with mock.patch.object(server.Conversation, "finish_recording", finish_recording):
            if header:
                ws = await websockets.connect(self.url, additional_headers={server.HEADER: test_server.SECRET})
            else:
                ws = await self.browser()
                await ws.send(f"auth {test_server.SECRET}")
            await ws.send(json.dumps({"type": "hello", "mode": "vad", "record": record}))
            ready = json.loads(await ws.recv())
            await self.send(ws, mic)
            await self.until(ws, "turn_done", [])
            for message in reported:
                await ws.send(json.dumps({"type": "played", **message}))
            if hangup:
                await ws.send(json.dumps({"type": "bye"}))
            await ws.close()
            await asyncio.wait_for(finished.wait(), 10)
        return np.frombuffer(mic, dtype="<i2"), ready

    def manifest(self, n=1):
        path = Path(self.tmp.name) / f"call-{n:03d}" / "manifest.json"
        return json.loads(path.read_text()) if path.is_file() else None

    def conversation(self, n=1):
        with wave.open(str(Path(self.tmp.name) / f"call-{n:03d}" / "conversation.wav"), "rb") as wav:
            return wav.getframerate(), np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2")

    @staticmethod
    def played(t, value, frames):
        return {"t": t, "pcm": base64.b64encode(np.full(frames, value, dtype="<i2").tobytes()).decode()}

    async def test_a_browser_call_ends_with_the_microphone_and_what_the_page_played(self):
        mic, ready = await self.call(reported=[self.played(0.6, 3000, 8_000)])
        self.assertTrue(ready["record"])
        manifest = self.manifest()
        self.assertEqual((manifest["end"], manifest["partial"], manifest["input_source"]),
                         ("hangup", False, "browser"))
        self.assertEqual((manifest["turns"]["first"], manifest["turns"]["last"]), (1, 1))
        rate, conversation = self.conversation()
        self.assertEqual((rate, conversation.size), (16_000, mic.size))
        window = slice(9_600, 17_600)
        self.assertTrue(np.array_equal(conversation[window].astype(int) - mic[window], np.full(8_000, 3000)))
        outside = np.ones(mic.size, bool)
        outside[window] = False
        self.assertTrue(np.array_equal(conversation[outside], mic[outside]),
                        "the reply the server sent is not in the recording unless the page reported playing it")

    async def test_a_dropped_connection_still_leaves_a_partial_recording(self):
        await self.call(hangup=False, reported=[self.played(0.2, 500, 1_600)])
        manifest = self.manifest()
        self.assertEqual((manifest["end"], manifest["partial"], manifest["finalized"]),
                         ("connection_closed", True, True))
        self.assertGreater(self.conversation()[1].size, 0)

    async def test_recording_can_be_turned_off_and_the_page_is_told(self):
        self.live.record_calls = False
        _, ready = await self.call(reported=[self.played(0.2, 500, 1_600)])
        self.assertFalse(ready["record"])
        self.assertEqual(list(Path(self.tmp.name).glob("call-*")), [])

    async def test_a_native_client_that_does_not_ask_is_not_recorded_here(self):
        _, ready = await self.call(header=True, record=False)
        self.assertFalse(ready["record"])
        self.assertEqual(list(Path(self.tmp.name).glob("call-*")), [])

    async def test_each_call_in_a_server_run_gets_its_own_recording(self):
        await self.call()
        await self.call()
        self.assertEqual((self.manifest(1)["turns"]["first"], self.manifest(2)["turns"]["first"]), (1, 2))


if __name__ == "__main__":
    unittest.main()
