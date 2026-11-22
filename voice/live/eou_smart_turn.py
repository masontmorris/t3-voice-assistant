"""End of turn by ear on the CPU: Pipecat's Smart Turn v3.2, an 8 MB int8 ONNX over a Whisper-tiny encoder (BSD-2).

The model reads the last 8 s of the turn's audio, zero-padded at the front, as Whisper log-mel features and returns
P(the person finished). It needs no GPU, so it adds no VRAM beside LFM3 and d1. The weights live under `VOICE_HOME`;
`load` returns None when they or the two Python packages are missing, and the caller then keeps the silence rule.
Call audio never leaves this machine.
"""

import os
import time
from pathlib import Path

import numpy as np

from voice.live.eou_audio import Answer, Policy

HOME = Path(os.environ.get("VOICE_HOME", Path.home() / "voice"))
MODEL = HOME / "smart-turn" / "smart-turn-v3.2-cpu.onnx"
RATE = 16_000
WINDOW_S = 8


# Chosen on the offline replays in docs/voice/audio-decider.md. Ending a turn early cost more cut-offs than it saved time
# (a theta above 1 never ends one), so the model only holds: it is asked at 500 and 600 ms, so the answer is in before the
# silence rule ends the turn, and a turn at 0.05 or less waits for up to 2 s of silence.
POLICY = Policy(check_ms=(500, 600, 900, 1200, 1600), theta=1.01, hold_theta=0.05, hold_ms=2000, window_s=WINDOW_S,
                cap_s=0.5)


class SmartTurn:
    """One question to the local model. `p_done` blocks, so the live server calls it from a worker thread."""

    def __init__(self, model=MODEL, policy=None, threads=2):
        import onnxruntime
        from transformers import WhisperFeatureExtractor, is_torch_available

        if is_torch_available():
            import torch

            torch.set_num_threads(1)  # its STFT otherwise spins every core: 380 ms of CPU for a 60 ms check

        options = onnxruntime.SessionOptions()
        options.inter_op_num_threads, options.intra_op_num_threads = 1, threads
        options.execution_mode = onnxruntime.ExecutionMode.ORT_SEQUENTIAL
        self.session = onnxruntime.InferenceSession(str(model), sess_options=options, providers=["CPUExecutionProvider"])
        self.features = WhisperFeatureExtractor(chunk_length=WINDOW_S)
        self.policy = policy or Policy(window_s=WINDOW_S)

    def p_done(self, pcm, timeout_s=None):
        """P(the speaker has finished) for the last 8 s of `pcm`, PCM16 mono at 16 kHz. `timeout_s` is not enforced:
        the caller bounds the wait."""
        started = time.perf_counter()
        samples = np.frombuffer(pcm[-WINDOW_S * RATE * 2:], dtype="<i2").astype(np.float32) / 32768
        samples = np.pad(samples, (WINDOW_S * RATE - len(samples), 0))
        inputs = self.features(samples, sampling_rate=RATE, return_tensors="np", padding="max_length",
                               max_length=WINDOW_S * RATE, truncation=True, do_normalize=True)
        out = self.session.run(None, {"input_features": inputs.input_features.astype(np.float32)})
        return Answer(float(out[0].reshape(-1)[0]), round((time.perf_counter() - started) * 1000, 1))


def load(policy=None, model=MODEL, threads=2):
    """The model, or None with the reason, when it cannot be loaded."""
    if not Path(model).is_file():
        return None, f"no model at {model}"
    try:
        return SmartTurn(model, policy, threads), None
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"
