"""LFM3's text-only implementation of the T3 text model's complete(messages) interface."""

import asyncio
import time


class RealtimeWriter:
    model = "lfm3-realtime"

    def __init__(self, url: str):
        self.url = url

    def complete(self, messages) -> dict:
        return asyncio.run(self._complete(messages))

    async def _complete(self, messages) -> dict:
        from voice.prototype.realtime import MODEL_RATE, Session
        from voice.prototype.run import create_item, plain_text, respond, update_session
        from voice.t3.speech import RENDER_TIMEOUT_S

        started = time.perf_counter()
        system, user = messages
        async with asyncio.timeout(RENDER_TIMEOUT_S):
            session = await Session.open(self.url, "t3-writer", time.perf_counter)
            try:
                await session.wait_for("session.created")
                await update_session(session, {"type": "realtime", "instructions": system["content"],
                                               "output_modalities": ["text"]})
                item = await create_item(session, plain_text(user["content"]))
                if item["accepted"] is not True:
                    raise RuntimeError(f"T3 writer item refused: {item['answer']}")
                result, _ = await respond(session, MODEL_RATE, started)
                if result["status"] != "completed":
                    raise RuntimeError(f"T3 writer response status: {result['status']}")
                return {"text": result["text"], "model": self.model, "ms": (time.perf_counter() - started) * 1000}
            finally:
                await session.close()
