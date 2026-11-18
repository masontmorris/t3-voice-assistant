"""Drive T3Seam against a fake seam server.

The frames below are built from voice/t3/seam.schema.json, in the shapes
PR #50's route test records, because recording from that TS test would need
node_modules and a dev server. The fake also closes with 1013, 1008 and an
aborted socket, answers upgrades with 401 and 404, and refuses connections,
and the client reports each one distinctly.
"""

import asyncio
import concurrent.futures
import contextlib
import json
import logging
import os
import socket
import tempfile
import threading
import unittest

from websockets.asyncio.server import serve

from voice.t3 import seam as S
from voice.t3 import types as T

TOKEN = "seam-test-token-abcdef"

HELLO = {
    "type": "hello",
    "ceiling": {
        "maxRuntimeMode": "auto-accept-edits",
        "interactionMode": "default",
        "operations": ["list", "read", "watch", "launch", "send", "interrupt"],
        "maxWatches": 16,
    },
    "projects": [
        {
            "projectId": "proj_sandbox",
            "name": "Voice sandbox",
            "spokenNames": ["sandbox", "voice sandbox"],
        }
    ],
    "latestSequence": 78,
}

THREAD = "mcp:5dc69bcb-ab72-4e05-8952-dc1047ab1e17"

LIST_VALUE = {
    "threads": [
        {
            "threadId": THREAD,
            "projectId": "proj_sandbox",
            "title": "voice fork-env proof",
            "activity": "finished",
            "lastActivityAt": "2026-11-17T20:25:10.738Z",
            "pinned": False,
        }
    ],
    "total": 1,
    "truncated": False,
    "asOfSequence": 78,
}

READ_VALUE = {
    "threadId": THREAD,
    "projectId": "proj_sandbox",
    "title": "voice fork-env proof",
    "activity": "finished",
    "run": {
        "runId": "run_1",
        "status": "completed",
        "startedAt": "2026-11-17T20:24:00.000Z",
    },
    "pending": None,
    "lastReply": {
        "text": "",
        "messageId": "msg_1",
        "turnItemId": None,
        "runId": "run_1",
        "at": "2026-11-17T20:25:10.738Z",
    },
    "asOfSequence": 78,
}

DONE_EVENT = {
    "type": "done",
    "threadId": THREAD,
    "runId": "run_1",
    "status": "completed",
    "result": {"text": "", "messageId": "msg_1", "turnItemId": None},
    "sequence": 78,
    "asOfSequence": 78,
}


def result_ok(call_id, value):
    return {"type": "result", "id": call_id, "ok": True, "value": value}


def result_err(call_id, code, message):
    return {"type": "result", "id": call_id, "ok": False, "error": {"code": code, "message": message}}


class ServerThread(threading.Thread):
    """A fake seam listener. Daemon, so it dies with the test process."""

    def __init__(self, handler):
        super().__init__(daemon=True)
        self.handler = handler
        self.port = None
        self.ready = threading.Event()
        self.seen_auth = []
        self.seen_close = []

    def run(self):
        async def main():
            async with serve(self._wrap, "127.0.0.1", 0) as server:
                self.port = server.sockets[0].getsockname()[1]
                self.ready.set()
                await asyncio.sleep(3600)

        asyncio.run(main())

    async def _wrap(self, ws):
        self.seen_auth.append(ws.request.headers.get("Authorization"))
        try:
            await self.handler(ws)
        finally:
            with contextlib.suppress(Exception):
                await ws.wait_closed()
            self.seen_close.append(ws.close_code)

    def start_and_wait(self):
        self.start()
        self.assert_ready()
        return self.port

    def assert_ready(self):
        if not self.ready.wait(timeout=10):
            raise AssertionError("fake seam server did not start")


def free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


@contextlib.contextmanager
def status_server(status):
    """A raw TCP server that answers the upgrade with one HTTP status."""
    holder = {}
    ready = threading.Event()

    async def handle(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(f"HTTP/1.1 {status}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode())
        await writer.drain()
        writer.close()

    async def main():
        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        holder["port"] = server.sockets[0].getsockname()[1]
        ready.set()
        await asyncio.sleep(3600)

    thread = threading.Thread(target=lambda: asyncio.run(main()), daemon=True)
    thread.start()
    if not ready.wait(timeout=10):
        raise AssertionError("status server did not start")
    yield holder["port"]


class SeamClientTest(unittest.TestCase):
    def write_token(self):
        fd, path = tempfile.mkstemp(prefix="seam-test-token")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(TOKEN + "\n")
        self.addCleanup(os.remove, path)
        return path

    def connect(self, port, token_file=None):
        seam = S.T3Seam.connect(
            f"ws://127.0.0.1:{port}", token_file or self.write_token()
        )
        self.addCleanup(seam.close)
        return seam

    def test_hello_list_read_watch_done(self):
        async def handler(ws):
            await ws.send(json.dumps(HELLO))
            async for message in ws:
                req = json.loads(message)
                if req["op"] == "list_threads":
                    await ws.send(json.dumps(result_ok(req["id"], LIST_VALUE)))
                elif req["op"] == "read_thread":
                    await ws.send(json.dumps(result_ok(req["id"], READ_VALUE)))
                elif req["op"] == "watch":
                    await ws.send(
                        json.dumps(result_ok(req["id"], {"threadId": THREAD}))
                    )
                    await ws.send(
                        json.dumps(
                            {
                                "type": "event",
                                "event": {
                                    "type": "run",
                                    "threadId": THREAD,
                                    "runId": "run_1",
                                    "status": "completed",
                                    "sequence": 77,
                                },
                            }
                        )
                    )
                    await ws.send(json.dumps({"type": "event", "event": DONE_EVENT}))
                elif req["op"] == "unwatch":
                    await ws.send(
                        json.dumps(result_ok(req["id"], {"threadId": THREAD}))
                    )

        server = ServerThread(handler)
        seam = self.connect(server.start_and_wait())

        hello = seam.hello
        self.assertEqual(hello.latest_sequence, 78)
        self.assertEqual(hello.ceiling.max_runtime_mode, T.RuntimeMode.AUTO_ACCEPT_EDITS)
        self.assertEqual(
            hello.projects[0].spoken_names, ("sandbox", "voice sandbox")
        )

        listing = seam.list_threads()
        self.assertEqual(listing.total, 1)
        self.assertFalse(listing.truncated)
        self.assertEqual(listing.threads[0].activity, T.ThreadActivity.FINISHED)

        state = seam.read_thread(THREAD)
        self.assertEqual(state.as_of_sequence, 78)
        self.assertIsNone(state.pending)
        self.assertEqual(state.last_reply.text, "")

        watched = seam.watch(THREAD, after_sequence=state.as_of_sequence)
        self.assertEqual(watched, T.WatchStarted(thread_id=THREAD))
        events = list(seam.drain(timeout=5))
        self.assertEqual(len(events), 2)
        self.assertIsInstance(events[0], T.RunEvent)
        done = events[1]
        self.assertIsInstance(done, T.DoneEvent)
        self.assertEqual(done.result.text, "")
        self.assertEqual(done.as_of_sequence, 78)

        self.assertEqual(seam.unwatch(THREAD), T.WatchStarted(thread_id=THREAD))
        seam.close()
        tail = list(seam.drain(timeout=5))
        self.assertEqual(tail, [T.CloseOther(code=1000)])
        self.assertEqual(server.seen_auth, [f"Bearer {TOKEN}"])

    def test_concurrent_calls_match_by_id(self):
        async def handler(ws):
            await ws.send(json.dumps(HELLO))
            first = json.loads(await ws.recv())
            second = json.loads(await ws.recv())
            answers = {
                "list_threads": LIST_VALUE,
                "read_thread": READ_VALUE,
            }
            for req in (second, first):
                await ws.send(json.dumps(result_ok(req["id"], answers[req["op"]])))

        server = ServerThread(handler)
        seam = self.connect(server.start_and_wait())
        results = {}
        threads = [
            threading.Thread(
                target=lambda: results.update({"list": seam.list_threads()})
            ),
            threading.Thread(
                target=lambda: results.update({"read": seam.read_thread(THREAD)})
            ),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertIsInstance(results["list"], T.ThreadList)
        self.assertIsInstance(results["read"], T.ThreadState)

    def test_launch_send_interrupt_surface_unavailable(self):
        async def handler(ws):
            await ws.send(json.dumps(HELLO))
            async for message in ws:
                req = json.loads(message)
                await ws.send(
                    json.dumps(
                        result_err(req["id"], "unavailable", "I can't do that yet.")
                    )
                )

        server = ServerThread(handler)
        seam = self.connect(server.start_and_wait())
        with self.assertRaises(S.SeamUnavailable):
            seam.launch("01JD3Z8Q4W5X6Y7Z8A9B0C1D2E", "proj_sandbox", "Fix it")
        with self.assertRaises(S.SeamUnavailable):
            seam.send("01JD3Z8Q4W5X6Y7Z8A9B0C1D2E", THREAD, "go on")
        with self.assertRaises(S.SeamUnavailable):
            seam.interrupt("01JD3Z8Q4W5X6Y7Z8A9B0C1D2E", THREAD)

    def test_watch_refused_not_found(self):
        async def handler(ws):
            await ws.send(json.dumps(HELLO))
            async for message in ws:
                req = json.loads(message)
                await ws.send(
                    json.dumps(
                        result_err(req["id"], "not_found", "I can't see that thread.")
                    )
                )

        server = ServerThread(handler)
        seam = self.connect(server.start_and_wait())
        with self.assertRaises(S.SeamRefused) as caught:
            seam.watch("thread_missing", after_sequence=0)
        self.assertEqual(caught.exception.code, T.RefusalCode.NOT_FOUND)

    def test_malformed_result_fails_its_caller_and_closes_1007(self):
        for reply in (
            result_ok(1, {"threads": [], "asOfSequence": 78, "truncated": False}),
            result_err(1, "new_unknown_code", "Unexpected refusal."),
        ):
            with self.subTest(reply=reply):
                async def handler(ws):
                    await ws.send(json.dumps(HELLO))
                    request = json.loads(await ws.recv())
                    await ws.send(json.dumps({**reply, "id": request["id"]}))

                server = ServerThread(handler)
                seam = self.connect(server.start_and_wait())
                answer = concurrent.futures.Future()

                def list_threads():
                    try:
                        answer.set_result(seam.list_threads())
                    except Exception as exc:
                        answer.set_exception(exc)

                threading.Thread(target=list_threads, daemon=True).start()
                with self.assertRaises(S.SeamUnavailable) as caught:
                    answer.result(timeout=1)
                self.assertIsInstance(caught.exception, S.SeamProtocolError)
                self.assertEqual(list(seam.drain(timeout=5)), [T.CLOSE_1007])

    def test_close_1013_mid_watch(self):
        async def handler(ws):
            await ws.send(json.dumps(HELLO))
            async for message in ws:
                req = json.loads(message)
                await ws.send(json.dumps(result_ok(req["id"], {"threadId": THREAD})))
                await ws.send(json.dumps({"type": "event", "event": DONE_EVENT}))
                await ws.close(1013, "The voice seam client stopped reading.")

        server = ServerThread(handler)
        seam = self.connect(server.start_and_wait())
        seam.watch(THREAD, after_sequence=70)
        events = list(seam.drain(timeout=5))
        self.assertEqual(events[-1], T.CLOSE_1013)
        self.assertIsInstance(events[0], T.DoneEvent)
        self.assertEqual(list(seam.drain(timeout=1)), [T.CLOSE_1013])

    def test_close_1008_after_hello(self):
        async def handler(ws):
            await ws.send(json.dumps(HELLO))
            await ws.close(1008, "The voice session was revoked.")

        server = ServerThread(handler)
        seam = self.connect(server.start_and_wait())
        self.assertEqual(list(seam.drain(timeout=5)), [T.CLOSE_1008])

    def test_close_1011_before_and_after_hello(self):
        for sends_hello in (False, True):
            with self.subTest(sends_hello=sends_hello):
                async def handler(ws):
                    if sends_hello:
                        await ws.send(json.dumps(HELLO))
                    await ws.close(1011, "The voice seam lost its session check.")

                server = ServerThread(handler)
                port = server.start_and_wait()
                if sends_hello:
                    end, = self.connect(port).drain(timeout=5)
                else:
                    with self.assertRaises(S.SeamConnectionEnded) as caught:
                        self.connect(port)
                    end = caught.exception.end
                self.assertEqual(end.label, "close-1011")
                self.assertIsInstance(end, T.Close1011)

    def test_aborted_socket_is_close_other_1006(self):
        async def handler(ws):
            await ws.send(json.dumps(HELLO))
            async for message in ws:
                req = json.loads(message)
                await ws.send(json.dumps(result_ok(req["id"], LIST_VALUE)))
                ws.transport.close()

        server = ServerThread(handler)
        seam = self.connect(server.start_and_wait())
        self.assertEqual(seam.list_threads().total, 1)
        self.assertEqual(list(seam.drain(timeout=5)), [T.CloseOther(code=1006)])

    def test_upgrade_401_and_404(self):
        for status, end in (
            ("401 Unauthorized", T.UPGRADE_401),
            ("404 Not Found", T.UPGRADE_404),
        ):
            with self.subTest(status=status):
                with status_server(status) as port:
                    with self.assertRaises(S.SeamConnectionEnded) as caught:
                        S.T3Seam.connect(
                            f"ws://127.0.0.1:{port}", self.write_token()
                        )
                self.assertEqual(caught.exception.end, end)

    def test_refused_connection_is_unreachable(self):
        with self.assertRaises(S.SeamConnectionEnded) as caught:
            S.T3Seam.connect(f"ws://127.0.0.1:{free_port()}", self.write_token())
        self.assertEqual(caught.exception.end, T.UNREACHABLE)

    def test_upgrade_403(self):
        with status_server("403 Forbidden") as port:
            with self.assertRaises(S.SeamConnectionEnded) as caught:
                self.connect(port)
        self.assertEqual(caught.exception.end.label, "upgrade-403")
        self.assertIsInstance(caught.exception.end, T.Upgrade403)

    def test_token_never_reaches_logs(self):
        async def handler(ws):
            await ws.send(json.dumps(HELLO))
            async for message in ws:
                req = json.loads(message)
                await ws.send(json.dumps(result_ok(req["id"], LIST_VALUE)))

        records = []

        class Capture(logging.Handler):
            def emit(self, record):
                records.append(record)

        # The websockets library itself logs headers at DEBUG, so only the
        # client's own loggers count: the requirement is that seam.py never
        # logs the token it read from the file.
        voice_logger = logging.getLogger("voice")
        capture = Capture()
        voice_logger.addHandler(capture)
        old_level = voice_logger.level
        voice_logger.setLevel(logging.DEBUG)
        try:
            server = ServerThread(handler)
            seam = self.connect(server.start_and_wait())
            seam.list_threads(project_id="proj_sandbox", limit=10)
            list(seam.drain(timeout=1))
        finally:
            voice_logger.removeHandler(capture)
            voice_logger.setLevel(old_level)
        leaked = [r for r in records if TOKEN in r.getMessage()]
        self.assertFalse(leaked, "the seam token reached the client's logs")
        with open(S.__file__, encoding="utf-8") as handle:
            source = handle.read()
        self.assertNotIn("logging", source)

    def test_malformed_server_frame_closes_1007(self):
        async def handler(ws):
            await ws.send(json.dumps(HELLO))
            await ws.send("this is not json{")

        server = ServerThread(handler)
        seam = self.connect(server.start_and_wait())
        self.assertEqual(list(seam.drain(timeout=5)), [T.CLOSE_1007])
        deadline = 50
        while not server.seen_close and deadline:
            __import__("time").sleep(0.1)
            deadline -= 1
        self.assertIn(1007, server.seen_close)


if __name__ == "__main__":
    unittest.main()
