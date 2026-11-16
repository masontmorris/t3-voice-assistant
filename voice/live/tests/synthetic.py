"""Generated audio for the echo canceller tests and `echo_bench.py`. Nothing here is recorded from a person.

A scene is the reply the client plays, the echo path from that playback to the microphone, an optional person
talking over it, and the room noise. Every part is made from seeded noise and sine envelopes, so a test failure is
reproducible on any machine and no real audio is needed.
"""

from dataclasses import dataclass

import numpy as np

from voice.live.aec import FRAME_BYTES, RATE, resample
from voice.live.endpoint import Endpointer, Start


def speechlike(seconds, seed, rate=RATE, level_dbfs=-12.0):
    """A speech-shaped signal: noise through a low pass, pre-emphasized, with syllable-rate amplitude bursts."""
    rng = np.random.default_rng(seed)
    count = int(round(seconds * rate))
    white = rng.standard_normal(count)
    shaped = np.empty(count, dtype=np.float32)
    state = 0.0
    for index in range(count):
        state = 0.5 * state + white[index]
        shaped[index] = state
    shaped = np.diff(shaped, prepend=np.float32(0.0)) * 0.4
    time = np.arange(count) / rate
    envelope = 0.55 + 0.45 * np.sin(2 * np.pi * 3.1 * time) ** 2
    shaped = shaped / (np.sqrt(np.mean(shaped ** 2)) + 1e-12) * envelope
    return (shaped * 10 ** (level_dbfs / 20)).astype(np.float32)


def echo_path(seed, delay_ms=30.0, tail_ms=40.0, rate=RATE):
    """A delayed, decaying room impulse response, normalized to unit energy."""
    rng = np.random.default_rng(seed)
    delay, tail = int(delay_ms * rate / 1000), int(tail_ms * rate / 1000)
    response = np.concatenate([np.zeros(delay, dtype=np.float32),
                               (rng.standard_normal(tail) * np.exp(-np.arange(tail) / (0.008 * rate))).astype(np.float32)])
    return response / np.sqrt(np.sum(response ** 2))


@dataclass(frozen=True)
class Scene:
    """One synthetic call moment. Arrays are mono float32 at 16 kHz."""

    reference: np.ndarray
    microphone: np.ndarray
    echo: np.ndarray
    speech: np.ndarray
    speech_start_s: float | None = None
    speech_end_s: float | None = None

    @property
    def seconds(self):
        return len(self.microphone) / RATE


@dataclass(frozen=True)
class Result:
    """What the client would send, and the turn starts the server's endpointer would see."""

    cleaned: np.ndarray
    starts: tuple
    stats: dict


def make_scene(seconds=8.0, echo_dbfs=-26.0, user=None, delay_ms=30.0, tail_ms=40.0, noise_dbfs=-65.0,
               reference_dbfs=-12.0, seed=1, speech_seed=9, rate=RATE, reply_gaps=()):
    """A reply, its echo, room noise, and optionally a person talking over the reply.

    `echo_dbfs` is the echo level at the microphone. `user` is `(start_s, end_s, level_dbfs)`, a near-end
    utterance on the same timeline. `echo_dbfs=None` models headphones, where the playback never reaches the
    microphone. `reply_gaps` are `(start_s, end_s)` spans with no playback, as when the output stream pauses.
    """
    reference = speechlike(seconds, seed, rate=rate, level_dbfs=reference_dbfs)
    for start_s, end_s in reply_gaps:
        reference[int(start_s * rate):int(end_s * rate)] = 0.0
    response = echo_path(seed + 1, delay_ms=delay_ms, tail_ms=tail_ms, rate=rate)
    echo = np.convolve(reference, response)[: len(reference)]
    if echo_dbfs is None:
        echo = np.zeros_like(reference)
    else:
        echo = echo * (10 ** (echo_dbfs / 20) / (np.sqrt(np.mean(echo ** 2)) + 1e-12))
    microphone = echo + np.random.default_rng(seed + 2).standard_normal(len(reference)).astype(np.float32) \
        * 10 ** (noise_dbfs / 20)
    speech = np.zeros_like(reference)
    start_s = end_s = None
    if user is not None:
        start_s, end_s, level_dbfs = user
        first, last = int(start_s * rate), int(end_s * rate)
        utterance = speechlike(end_s - start_s, speech_seed, rate=rate, level_dbfs=level_dbfs)
        speech[first:last] = utterance[: last - first]
        microphone = microphone + speech
    return Scene(reference, microphone, echo, speech, start_s, end_s)


def run_scene(scene, canceller, playback_rate=RATE, rate=RATE):
    """Push a scene through the canceller frame by frame, as the client does, and collect the endpointer's starts.

    Playback chunks are the same frames resampled to `playback_rate`, because the client plays at LFM3's output
    rate while the microphone, and this canceller, run at 16 kHz.
    """
    endpointer = Endpointer()
    cleaned, starts = [], []
    for offset in range(0, len(scene.microphone) - FRAME_BYTES // 2 + 1, FRAME_BYTES // 2):
        frame = scene.microphone[offset:offset + FRAME_BYTES // 2]
        time = (offset + len(frame)) / rate
        if scene.reference is not None and offset + FRAME_BYTES // 2 <= len(scene.reference):
            played = scene.reference[offset:offset + FRAME_BYTES // 2]
            if playback_rate != rate:
                played = resample(played, rate, playback_rate)
            canceller.playback((np.clip(played, -1, 1) * 32768).astype("<i2").tobytes(), playback_rate,
                               offset / rate)
        sent = canceller.process((np.clip(frame, -1, 1) * 32768).astype("<i2").tobytes(), time)
        cleaned.append(np.frombuffer(sent, dtype="<i2").astype(np.float32) / 32768.0)
        for event in endpointer.push(sent):
            if isinstance(event, Start):
                starts.append(round(time, 3))
    return Result(np.concatenate(cleaned), tuple(starts), canceller.stats())
