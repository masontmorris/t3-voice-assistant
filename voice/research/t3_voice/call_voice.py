"""The tried and rejected way to speak a T3 line in the call's own voice: ask a fresh chat-mode session to repeat it.

TTS mode speaks only the `female` and `male` TTS speakers, and the call's chat mode is a third speaker no TTS prompt
names. So `CallVoice` asks a fresh persona session (never the call's own) to read the line aloud, and lets its audio
out through the reply's `Release` gate: the text so far must start the checked line, and the whole text must be it.

The text gate held (19 of 20 lines exact on the GPU), but the audio did not say its own text: WER 23.1% and 13 of 20
lines with a wrong name, status or number, against 5.2% and 5 for the TTS voice (measured, docs/voice/t3-same-voice.md).
It is kept for `bench.py`, so the result can be rerun, and is not on the live path.
"""

import asyncio
import base64
import re
import time

from websockets.exceptions import WebSocketException

from voice.live.tts import STALL_S, resampled
from voice.prototype.realtime import Session
from voice.prototype.release import Release

# On the 20 synthetic lines (CPU, one run each) this wording repeated 20 exactly; "Say exactly this and nothing
# else: " 19, answering "No," to a question; the same request under the bare chat prompt 17. As the system prompt
# the request was answered, not repeated.
REQUEST = "Read this aloud word for word, and say nothing else: "
APOSTROPHES = str.maketrans({"’": "'", "‘": "'"})


class LineCut(Exception):
    """The voice stopped part way through a line. The audio already yielded is a true start of it."""


def words(text):
    return re.sub(r"\s+", " ", text.translate(APOSTROPHES).lower()).lstrip()


def starts(said, line, final):
    """Whether `said` is the start of `line`, or all of it when `final`."""
    said, line = words(said), words(line)
    return said.strip() == line.strip() if final else line.startswith(said)


class CallVoice:
    engine = "call"

    def __init__(self, url, fallback, instructions, request=REQUEST, session_cls=Session):
        self.url, self.fallback, self.session_cls = url, fallback, session_cls
        self.instructions, self.request = instructions, request

    async def stream(self, text, rate, log=None):
        started, chunks = time.perf_counter(), asyncio.Queue()
        gate = Release(lambda said, final: starts(said, text, final), lambda _said, pcm: chunks.put_nowait(pcm))
        state = {"strayed": False, "done": False, "source": 24_000}

        def on_event(event):
            kind = event.get("type")
            if kind == "session.created":
                state["source"] = event["session"]["audio"]["output"]["format"]["rate"]
            elif kind == "response.output_text.delta":
                gate.on_text(event["delta"])
                if not starts(gate.text.rstrip(" "), text, False) and not state["strayed"]:
                    state["strayed"] = True
                    chunks.put_nowait(None)
            elif kind == "response.output_audio.delta":
                gate.on_audio(base64.b64decode(event["delta"]), time.perf_counter() - started)
            elif kind == "response.done":
                state["done"] = True
                gate.finish(time.perf_counter() - started)
                chunks.put_nowait(None)
            elif kind == "error":
                state["error"] = event.get("error", {}).get("message")
                chunks.put_nowait(None)

        sent, first, session, error = 0, None, None, None
        try:
            session = await self.session_cls.open(self.url, "t3-voice", time.perf_counter, on_event=on_event)
            await session.send(type="session.update", session={"type": "realtime", "instructions": self.instructions,
                                                               "output_modalities": ["text", "audio"]})
            await session.send(type="conversation.item.create", item={
                "type": "message", "role": "user", "content": [{"type": "input_text", "text": self.request + text}]})
            await session.send(type="response.create")
            while (pcm := await asyncio.wait_for(chunks.get(), STALL_S)) is not None:
                first = first or time.perf_counter() - started
                sent += len(pcm)
                yield resampled(pcm, state["source"], rate)
        except (OSError, asyncio.TimeoutError, WebSocketException) as exc:
            error = repr(exc)
        finally:
            if session is not None:
                if not state["done"]:
                    await _quietly(session.send(type="response.cancel"))
                await _quietly(session.close())
        exact = gate.passed is True and not state["strayed"] and error is None and "error" not in state
        if log is not None:
            log("t3_voice", engine=self.engine, said=gate.text, exact=exact, strayed=state["strayed"],
                error=error or state.get("error"), first_chunk_s=None if first is None else round(first, 4),
                total_s=round(time.perf_counter() - started, 4), audio_bytes=sent)
        if exact:
            return
        if sent:
            raise LineCut(f"the call voice stopped after {sent} bytes: {gate.text!r}")
        async for pcm in self.fallback.stream(text, rate, log):
            yield pcm


async def _quietly(awaitable):
    try:
        await awaitable
    except (OSError, WebSocketException):
        pass
