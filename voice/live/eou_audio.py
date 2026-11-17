"""End of turn by ear: a local 2B model answers whether the person finished or is pausing mid-thought.

The live transcript lags the voice. Its last word lands a median 0.69 s after speech ends, which is why the endpointer
waits a fixed 0.7 s of silence. This asks the audio itself instead, at 150, 300 and 500 ms of silence, and ends the turn
at the first answer of `theta` or more. Below that the 0.7 s rule stands unchanged, so a slow, wrong or missing answer
ends the turn exactly as today. The study's numbers: AUC 0.932 on real turns for this model over audio, against 0.637
for text on the lagging transcript (docs/voice/audio-decider.md).

The model is d1-Omni-2B Q8_0 on its own local llama-server, started and stopped with the call's LFM3 by `--eou audio`.
Mason's audio never leaves this machine. One question, worded as the study worded it, and audio only: adding the stale
live transcript to the same question dropped the study's AUC from 0.955 to 0.690.
"""

import base64
import http.client
import io
import json
import os
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from voice.live.endpoint import RATE
from voice.local_decider.client import LlamaServer

HOME = Path(os.environ.get("VOICE_HOME", Path.home() / "voice"))
MODEL = HOME / "gguf" / "d1-omni" / "d1-Omni-2B-Q8_0.gguf"
MMPROJ = HOME / "gguf" / "d1-omni" / "mmproj-d1-Omni-2B-Q8_0.gguf"
PORT = 8098
# The study's flags for this model, unchanged: one request in flight, every layer on the GPU, one physical batch.
FLAGS = ("-ngl", "99", "-np", "1", "-b", "8192", "-ub", "2048", "-fa", "on", "-c", "8192")

QUESTION = {"finished": {
    "type": "choice",
    "instructions": "The recording stops at the moment the speaker went quiet. Has the speaker finished their turn, or "
                    "are they pausing in the middle of a thought and about to say more?",
    "criteria": {
        "done": "The speaker has finished and expects the assistant to respond now: a complete request, question or "
                "statement that ends the way a finished sentence ends.",
        "continuing": "The speaker is pausing mid-thought: the sentence is unfinished, trails off, hesitates, or they "
                      "are collecting their thoughts and will say more."}}}


@dataclass(frozen=True)
class Policy:
    """When to ask, what counts as done, and what a check may cost.

    `check_ms` are points of silence inside the existing `silence_ms` wait; `window_s` is how much of the turn's tail
    the model hears; `cap_s` is the longest a single answer may take. The study asked on the last 3 s with no loss of
    accuracy, and measured a 40 ms median answer, so `cap_s` is slack for a loaded GPU, not the normal case.
    """
    check_ms: tuple[int, ...] = (150, 300, 500)
    theta: float = 0.5
    window_s: float = 3.0
    cap_s: float = 0.5

    def deadline_s(self, silent_ms, fallback_ms):
        """How long a check may take and still answer before `fallback_ms` ends the turn anyway, capped by `cap_s`."""
        return max(0.05, min(self.cap_s, (fallback_ms - silent_ms) / 1000))


@dataclass(frozen=True)
class Answer:
    p_done: float
    ms: float


def wav_data_uri(pcm, rate=16_000):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(pcm)
    return "data:audio/wav;base64," + base64.b64encode(buffer.getvalue()).decode()


class Decider:
    """One question to the local model. `p_done` blocks, so the live server calls it from a worker thread."""

    def __init__(self, url=f"http://127.0.0.1:{PORT}", policy=Policy()):
        parts = urlsplit(url)
        self.host, self.port, self.policy = parts.hostname, parts.port, policy

    def p_done(self, pcm, timeout_s=None):
        """P(the speaker has finished) for the last `window_s` of `pcm`, PCM16 mono at 16 kHz."""
        timeout = self.policy.cap_s if timeout_s is None else timeout_s
        tail = pcm[-int(self.policy.window_s * RATE) * 2:]
        body = {"state": None, "questions": QUESTION, "files": [wav_data_uri(tail)]}
        started = time.perf_counter()
        connection = http.client.HTTPConnection(self.host, self.port, timeout=timeout)
        try:
            connection.request("POST", "/v1/systemone", json.dumps(body), {"Content-Type": "application/json"})
            response = connection.getresponse()
            raw = response.read()
        finally:
            connection.close()
        if response.status != 200:
            raise RuntimeError(f"the model answered {response.status}: {raw[:200]!r}")
        probabilities = json.loads(raw)["answers"]["finished"]["probabilities"]
        return Answer(float(probabilities["done"]), round((time.perf_counter() - started) * 1000, 1))


def server(port=PORT, log_path=None):
    """The model's llama-server, as the study measured it. The caller starts and stops it."""
    return LlamaServer(["-m", str(MODEL), "--mmproj", str(MMPROJ), *FLAGS], port, log_path)
