"""The browser path: the page over GET /, and the auth frame in place of the header."""

import asyncio
import json
import tempfile
import unittest
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

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
        with urllib.request.urlopen(self.http + path) as response:
            return response.status, response.headers.get_content_type(), response.read().decode("utf-8")

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

    def test_get_root_serves_the_page(self):
        status, content_type, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertEqual(content_type, "text/html")
        self.assertIn("<title>Voice call</title>", body)
        self.assertIn('src="app.js"', body)
        self.assertNotIn(test_server.SECRET, body, "the page carries no secret")

    def test_get_script_paths_serve_javascript(self):
        for path in ("/app.js", "/resample.js", "/capture-worklet.js", "/playback-worklet.js"):
            status, content_type, body = self.get(path)
            self.assertEqual((status, content_type), (200, "text/javascript"), path)
            self.assertTrue(body.strip(), path)

    def test_get_unknown_path_is_not_found(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.get("/nope")
        self.assertEqual(caught.exception.code, 404)

    async def test_a_good_auth_frame_is_accepted(self):
        ws = await self.browser()
        await ws.send(f"auth {test_server.SECRET}")
        await ws.send(json.dumps({"type": "hello", "mode": "vad"}))
        self.assertEqual(json.loads(await ws.recv())["type"], "ready")
        log = []
        await self.send(ws, noise(500) + tone(600) + noise(900, seed=1))
        done = await self.until(ws, "turn_done", log)
        self.assertEqual(done["final_transcript"], "words in turn_001")
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

    async def test_a_header_secret_with_an_origin_is_still_refused(self):
        with self.assertRaises(websockets.InvalidStatus):
            await websockets.connect(self.url, additional_headers={server.HEADER: test_server.SECRET,
                                                                    "Origin": "http://example.com"})


if __name__ == "__main__":
    unittest.main()
