"""LFM3's text-only implementation of the T3 text model's complete(messages) interface."""

import asyncio
import logging
import time

MAX_SESSIONS = 2
CLOSE_TIMEOUT_S = 2.0
log = logging.getLogger(__name__)


class RealtimeWriter:
    model = "lfm3-realtime"

    def __init__(self, url: str):
        self.url = url
        self._loop = None
        self._sessions: set[asyncio.Task] = set()
        self._slots: asyncio.Semaphore | None = None

    async def __aenter__(self):
        self._loop = asyncio.get_running_loop()
        self._slots = asyncio.Semaphore(MAX_SESSIONS)
        return self

    async def __aexit__(self, *_):
        self._loop = None
        await asyncio.gather(*self._sessions)

    def complete(self, messages) -> dict:
        loop = self._loop
        if loop is None:
            raise RuntimeError("T3 writer is not running")
        return asyncio.run_coroutine_threadsafe(self._submit(messages), loop).result()

    async def _submit(self, messages) -> dict:
        if self._loop is None:
            raise RuntimeError("T3 writer is stopping")
        # A draft asked for while the last two sessions are still closing waits for one, as a request waits its turn on
        # the lane; failing it left the act unspoken until the next retry (6 of 10 rehearsal calls had one).
        await self._slots.acquire()
        draft = self._loop.create_future()
        task = asyncio.create_task(self._write(messages, draft))
        self._sessions.add(task)
        task.add_done_callback(self._release)
        return await draft

    def _release(self, task: asyncio.Task) -> None:
        self._sessions.discard(task)
        self._slots.release()

    async def _write(self, messages, draft: asyncio.Future) -> None:
        from voice.prototype.realtime import MODEL_RATE, Session
        from voice.prototype.run import create_item, plain_text, respond, update_session
        from voice.t3.speech import RENDER_TIMEOUT_S

        started = time.perf_counter()
        session = None
        try:
            system, user = messages
            async with asyncio.timeout(RENDER_TIMEOUT_S):
                session = await Session.open(self.url, "t3-writer", time.perf_counter)
                await session.wait_for("session.created")
                await update_session(session, {"type": "realtime", "instructions": system["content"],
                                               "output_modalities": ["text"]})
                item = await create_item(session, plain_text(user["content"]))
                if item["accepted"] is not True:
                    raise RuntimeError(f"T3 writer item refused: {item['answer']}")
                result, _ = await respond(session, MODEL_RATE, started)
                if result["status"] != "completed":
                    raise RuntimeError(f"T3 writer response status: {result['status']}")
            if not draft.done():
                draft.set_result({"text": result["text"], "model": self.model,
                                  "ms": (time.perf_counter() - started) * 1000})
        except asyncio.CancelledError:
            draft.cancel()
            raise
        except Exception as exc:
            if not draft.done():
                draft.set_exception(exc)
        finally:
            if session is not None:
                try:
                    async with asyncio.timeout(CLOSE_TIMEOUT_S):
                        await session.close()
                except Exception as exc:
                    log.warning("T3 writer session close failed: %r", exc)
