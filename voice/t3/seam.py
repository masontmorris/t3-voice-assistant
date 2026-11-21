"""Typed client for the voice seam.

One socket on a background thread, typed calls, drain() for events. The token
is read from the token file and never logged. A socket end is a SocketEnd
value the watch lifecycle can act on.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import itertools
import json
import queue
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from voice.t3 import types as T

SEAM_PATH = "/voice-seam"
CONNECT_TIMEOUT_S = 30.0


class SeamRefused(Exception):
    """The seam answered a request with a refusal code.

    For ``refused``, ``reason`` is T3's recorded rejection and ``command_id`` the command whose
    receipt said so. ``thread_id`` names a launched thread whose first message T3 refused.
    """

    def __init__(self, refusal: T.ResultErr) -> None:
        super().__init__(f"{refusal.code.value}: {refusal.message}")
        self.code = refusal.code
        self.message = refusal.message
        self.reason = refusal.reason
        self.command_id = refusal.command_id
        self.thread_id = refusal.thread_id


class SeamUnavailable(Exception):
    """The seam answered unavailable, or the socket closed before a result."""


class SeamProtocolError(SeamUnavailable):
    """The seam sent a result the client could not decode."""


class SeamConnectionEnded(Exception):
    """Connect failed, or a call raced a dead socket. The end is the value."""

    def __init__(self, end: T.SocketEnd) -> None:
        super().__init__(end.label)
        self.end = end


_UPGRADE_ENDS = {401: T.UPGRADE_401, 403: T.UPGRADE_403, 404: T.UPGRADE_404}


def _origin_to_url(origin: str) -> str:
    scheme, _, rest = origin.partition("://")
    if not rest:
        raise ValueError(f"seam origin needs a scheme: {origin!r}")
    ws_scheme = {"ws": "ws", "wss": "wss", "http": "ws", "https": "wss"}.get(scheme)
    if ws_scheme is None:
        raise ValueError(f"seam origin has an unknown scheme: {origin!r}")
    return f"{ws_scheme}://{rest.rstrip('/')}{SEAM_PATH}"


class T3Seam:
    """One seam socket. Calls block, drain() yields events and the final end."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ids = itertools.count(1)
        self._pending: dict[int, tuple[str, concurrent.futures.Future]] = {}
        self._events: queue.Queue[T.DrainItem] = queue.Queue()
        self._handoff: queue.Queue = queue.Queue(maxsize=1)
        self._hello: T.Hello | None = None
        self._end: T.SocketEnd | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ws: Any = None
        self._thread: threading.Thread | None = None

    @classmethod
    def connect(cls, origin: str, token_file: str | Path) -> T3Seam:
        """Open the seam socket and wait for hello. The token never leaves the header."""
        token = Path(token_file).read_text(encoding="utf-8").strip()
        if not token:
            raise ValueError(f"seam token file is empty: {token_file}")
        seam = cls()
        url = _origin_to_url(origin)
        thread = threading.Thread(
            target=asyncio.run,
            args=(seam._run(url, token),),
            name="t3-seam",
            daemon=True,
        )
        seam._thread = thread
        thread.start()
        try:
            kind, payload = seam._handoff.get(timeout=CONNECT_TIMEOUT_S)
        except queue.Empty:
            seam.close()
            raise SeamConnectionEnded(T.UNREACHABLE) from None
        if kind != "ready":
            seam.close()
            raise SeamConnectionEnded(payload)
        seam._hello = payload
        return seam

    @property
    def hello(self) -> T.Hello:
        """The ceiling and allowed projects, sent by the server first."""
        assert self._hello is not None
        return self._hello

    def list_threads(
        self, project_id: str | None = None, limit: int | None = None
    ) -> T.ThreadList:
        return self._call(
            "list_threads",
            {
                k: v
                for k, v in (("projectId", project_id), ("limit", limit))
                if v is not None
            },
        )

    def read_thread(self, thread_id: str) -> T.ThreadState:
        return self._call("read_thread", {"threadId": thread_id})

    def watch(
        self, thread_id: str, after_sequence: int, run_id: str | None = None
    ) -> T.WatchStarted:
        return self._call(
            "watch",
            {
                "threadId": thread_id,
                "afterSequence": after_sequence,
                **({"runId": run_id} if run_id is not None else {}),
            },
        )

    def unwatch(self, thread_id: str) -> T.WatchStarted:
        return self._call("unwatch", {"threadId": thread_id})

    def launch(self, request_id: str, project_id: str, instruction: str) -> T.LaunchResult:
        """Start a thread, or answer for the one this request id already started."""
        return self._call(
            "launch",
            {"requestId": request_id, "projectId": project_id, "instruction": instruction},
        )

    def send(self, request_id: str, thread_id: str, text: str) -> T.SendResult:
        """Queue a message behind the thread's active run, or answer for the one this request id sent."""
        return self._call(
            "send", {"requestId": request_id, "threadId": thread_id, "text": text}
        )

    def interrupt(self, request_id: str, thread_id: str) -> T.InterruptResult:
        """Interrupt the thread's active run. A retry with the same request id answers NothingRunning."""
        return self._call("interrupt", {"requestId": request_id, "threadId": thread_id})

    def respond(
        self, request_id: str, thread_id: str, pending_request_id: str, decision: str
    ) -> T.RespondResult:
        """Approve or deny one pending approval of a thread. A retry with the same request id answers the first answer."""
        return self._call(
            "respond",
            {
                "requestId": request_id,
                "threadId": thread_id,
                "pendingRequestId": pending_request_id,
                "decision": decision,
            },
        )

    def drain(self, timeout: float | None = None) -> Iterator[T.DrainItem]:
        """Yield events, then the socket's end, then stop.

        A timeout expiry also stops the drain with nothing yielded for it.
        A later drain re-yields the recorded end at once.
        """
        while True:
            try:
                item = self._events.get(timeout=timeout)
            except queue.Empty:
                return
            if isinstance(item, T.SocketEnd):
                self._events.put(item)
                yield item
                return
            yield item

    def close(self) -> None:
        """Close the socket and join the background thread. Idempotent."""
        loop, ws = self._loop, self._ws
        if loop is not None and ws is not None:
            def close_if_open() -> None:
                if self._end is None:
                    asyncio.create_task(ws.close())

            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(close_if_open)
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=10.0)

    def __enter__(self) -> T3Seam:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _call(self, op: str, request_input: dict[str, Any]) -> Any:
        with self._lock:
            if self._end is not None:
                raise SeamConnectionEnded(self._end)
            call_id = next(self._ids)
            future: concurrent.futures.Future = concurrent.futures.Future()
            self._pending[call_id] = (op, future)
        try:
            self._send(json.dumps({"id": call_id, "op": op, "input": request_input}))
        except RuntimeError:
            raise SeamConnectionEnded(self._end or T.UNREACHABLE) from None
        return future.result()

    def _send(self, frame: str) -> None:
        loop, ws = self._loop, self._ws
        if loop is None or ws is None or self._end is not None:
            raise SeamConnectionEnded(
                self._end if self._end is not None else T.UNREACHABLE
            )
        future = asyncio.run_coroutine_threadsafe(ws.send(frame), loop)
        future.result()

    async def _run(self, url: str, token: str) -> None:
        self._loop = asyncio.get_running_loop()
        try:
            async with ws_connect(
                url, additional_headers={"Authorization": f"Bearer {token}"}
            ) as ws:
                self._ws = ws
                try:
                    first = await ws.recv()
                except ConnectionClosed:
                    self._settle(T.decode_close(ws.close_code))
                    return
                try:
                    frame = json.loads(first)
                    if not isinstance(frame, dict) or frame.get("type") != "hello":
                        raise ValueError(f"seam opened with: {first!r:.120}")
                    hello = T.decode_hello(frame)
                except ValueError:
                    with contextlib.suppress(ConnectionClosed):
                        await ws.close(1007, "The seam client needs a hello frame first.")
                    self._settle(T.CLOSE_1007)
                    return
                self._settle_ready(hello)
                end = await self._receive(ws)
                if end is T.CLOSE_1007:
                    with contextlib.suppress(ConnectionClosed):
                        await ws.close(1007, "The seam client cannot read this frame.")
                self._settle(end)
        except InvalidStatus as exc:
            self._settle(_UPGRADE_ENDS.get(exc.response.status_code, T.UNREACHABLE))
        except (OSError, TimeoutError):
            self._settle(T.UNREACHABLE)
        except Exception:
            self._settle(T.UNREACHABLE)
            raise

    async def _receive(self, ws: Any) -> T.SocketEnd | None:
        """Forward frames until the socket ends. Returns our own end, if any."""
        try:
            async for message in ws:
                end = self._on_message(message)
                if end is not None:
                    return end
        except ConnectionClosed:
            pass
        return T.decode_close(ws.close_code)

    def _on_message(self, message: Any) -> T.SocketEnd | None:
        """Route one server frame. Binary frames are ignored; the route sends text."""
        if not isinstance(message, str):
            return None
        try:
            frame = json.loads(message)
            if not isinstance(frame, dict):
                raise ValueError(f"seam frame is not an object: {message!r:.120}")
            kind = frame.get("type")
            if kind == "result":
                self._on_result(frame)
            elif kind == "event":
                self._events.put(T.decode_event(frame.get("event")))
            elif kind == "watch_ended":
                self._events.put(T.decode_watch_ended(frame))
            else:
                raise ValueError(f"unknown seam frame type: {kind!r}")
        except (TypeError, ValueError):
            return T.CLOSE_1007
        return None

    def _on_result(self, frame: dict[str, Any]) -> None:
        call_id = frame.get("id")
        with self._lock:
            entry = self._pending.get(call_id) if type(call_id) is int else None
        if entry is None:
            raise ValueError(f"seam result names no open request: {frame!r:.160}")
        op, future = entry
        try:
            result = self._decode_result(op, frame)
        except (TypeError, ValueError):
            future.set_exception(SeamProtocolError("the seam sent an invalid result"))
            raise
        with self._lock:
            del self._pending[call_id]
        if isinstance(result, T.ResultErr):
            if result.code is T.RefusalCode.UNAVAILABLE:
                future.set_exception(SeamUnavailable(result.message))
            else:
                future.set_exception(SeamRefused(result))
        else:
            future.set_result(result)

    @staticmethod
    def _decode_result(op: str, frame: dict[str, Any]) -> T.ResultValue | T.ResultErr:
        ok = frame.get("ok")
        if ok is True:
            decoder = T.RESULT_DECODERS.get(op)
            if decoder is None:
                raise ValueError(f"seam answered ok for {op}, which has no decoder yet")
            return decoder(frame.get("value"))
        if ok is not False:
            raise ValueError("seam result requires a boolean ok field")
        error = frame.get("error")
        if not isinstance(error, dict) or not isinstance(error.get("message"), str):
            raise ValueError("seam refusal requires an error object with a message")
        return T.ResultErr(
            code=T.RefusalCode(error.get("code")),
            message=error["message"],
            reason=error.get("reason"),
            command_id=error.get("commandId"),
            thread_id=error.get("threadId"),
        )

    def _settle_ready(self, hello: T.Hello) -> None:
        self._handoff.put(("ready", hello))

    def _settle(self, end: T.SocketEnd | None) -> None:
        if end is None:
            return
        with self._lock:
            if self._end is not None:
                return
            self._end = end
            pending = list(self._pending.values())
            self._pending.clear()
        for _, future in pending:
            if not future.done():
                future.set_exception(
                    SeamUnavailable(f"the socket closed ({end.label}) before its result")
                )
        self._events.put(end)
        if self._handoff.empty():
            self._handoff.put(("end", end))
