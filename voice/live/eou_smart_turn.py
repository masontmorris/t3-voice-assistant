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
        from transformers import WhisperFeatureExtractor
        from transformers.audio_utils import window_function

        options = onnxruntime.SessionOptions()
        options.inter_op_num_threads, options.intra_op_num_threads = 1, threads
        options.execution_mode = onnxruntime.ExecutionMode.ORT_SEQUENTIAL
        session = onnxruntime.InferenceSession(str(model), sess_options=options, providers=["CPUExecutionProvider"])
        extractor = WhisperFeatureExtractor(chunk_length=WINDOW_S)
        self.session, self.policy = session, policy or Policy(window_s=WINDOW_S)
        self._window = window_function(extractor.n_fft, "hann")
        self._n_fft, self._hop, self._mel = extractor.n_fft, extractor.hop_length, extractor.mel_filters

    def features(self, samples):
        """Whisper's log-mel features of 8 s of audio, in numpy: the extractor's own path runs on torch and so on
        torch's process-wide thread pool, which this server must not change. Whole-array operations, so the work is
        in numpy's native code and does not fight the server's other threads for the interpreter."""
        samples = (samples - samples.mean()) / np.sqrt(samples.var() + 1e-7)
        padded = np.pad(samples, (self._n_fft // 2, self._n_fft // 2), mode="reflect")
        frames = np.lib.stride_tricks.sliding_window_view(padded, self._n_fft)[:: self._hop]
        power = np.abs(np.fft.rfft(frames * self._window, axis=1)) ** 2
        log_mel = np.log10(np.maximum(power @ self._mel, 1e-10)).T[:, :-1]
        return ((np.maximum(log_mel, log_mel.max() - 8.0) + 4.0) / 4.0).astype(np.float32)[None]

    def p_done(self, pcm, timeout_s=None):
        """P(the speaker has finished) for the last 8 s of `pcm`, PCM16 mono at 16 kHz. `timeout_s` is not enforced:
        the caller bounds the wait."""
        started = time.perf_counter()
        samples = np.frombuffer(pcm[-WINDOW_S * RATE * 2:], dtype="<i2").astype(np.float32) / 32768
        samples = np.pad(samples, (WINDOW_S * RATE - len(samples), 0))
        out = self.session.run(None, {"input_features": self.features(samples)})
        return Answer(float(out[0].reshape(-1)[0]), round((time.perf_counter() - started) * 1000, 1))


def load(policy=None, model=MODEL, threads=2):
    """The model, or None with the reason, when it cannot be loaded."""
    if not Path(model).is_file():
        return None, f"no model at {model}"
    try:
        return SmartTurn(model, policy, threads), None
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"
