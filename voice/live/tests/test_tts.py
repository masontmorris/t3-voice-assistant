"""ServerTts against a local stand-in for llama-server's streamed `/v1/audio/speech`: chunked PCM16, with the rate in
the Content-Type, split wherever the socket splits it."""

import asyncio
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

from voice.live.tts import ServerTts

PCM = np.arange(1, 2401, dtype="<i2").tobytes()
# Odd sizes, so a sample is split across chunks.
PIECES = [PCM[:1001], PCM[1001:1002], PCM[1002:3333], PCM[3333:]]


def serve(rate, status=200):
    asked = []

    class Speech(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            asked.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(status)
            self.send_header("Content-Type", f"audio/pcm;rate={rate};encoding=signed-int;bits=16")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for piece in PIECES if status == 200 else ():
                self.wfile.write(f"{len(piece):x}\r\n".encode() + piece + b"\r\n")
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Speech)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, asked


class ServerTtsTest(unittest.IsolatedAsyncioTestCase):
    async def speak(self, rate, out_rate, status=200):
        server, asked = serve(rate, status)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        tts = ServerTts(f"http://127.0.0.1:{server.server_address[1]}")
        chunks = [chunk async for chunk in tts.stream("It finished.", out_rate)]
        return chunks, asked

    async def test_the_stream_is_the_lines_samples_whole_and_in_order(self):
        chunks, asked = await self.speak(24_000, 24_000)
        self.assertEqual(b"".join(chunks), PCM)
        self.assertTrue(all(len(chunk) % 2 == 0 for chunk in chunks), "no chunk ends inside a sample")
        self.assertEqual(asked, [{"input": "It finished.", "voice": "female", "response_format": "pcm",
                                  "stream_format": "audio"}])

    async def test_another_server_rate_is_resampled_to_the_turns_rate(self):
        chunks, _ = await self.speak(16_000, 24_000)
        self.assertAlmostEqual(len(b"".join(chunks)) / 2 / 24_000, len(PCM) / 2 / 16_000, delta=0.01)

    async def test_a_refused_request_raises(self):
        with self.assertRaises(OSError):
            await self.speak(24_000, 24_000, status=500)


if __name__ == "__main__":
    unittest.main()
