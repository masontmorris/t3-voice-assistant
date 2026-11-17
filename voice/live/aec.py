"""Cancels speaker echo from the live client's microphone before the frames reach the server.

Why. The client plays reply audio through speakers unless headphones are plugged in. The server decides turns from
the microphone, so with speakers it hears the assistant's own voice, treats it as speech, and sends `barge_in` for
a reply the person never interrupted. This module removes that echo and leaves the person's own speech alone, so
the reply plays and a real interruption still starts the next turn.

How. The reference is the PCM the client's output callback handed to the Windows audio device, stamped with that
callback's `perf_counter` time. The canceller keeps the last few seconds of it on the same clock as the microphone
frames, so each microphone frame can read the reference window that could have produced its echo. A
block-normalized least-mean-squares filter learns the room's echo path from that window, and what is sent upstream
is the microphone minus the filter's estimate. The filter length covers the output buffer, the acoustic path and
the room tail, so no separate delay measurement is needed.

Near-end speech. A linear filter cannot subtract the person's voice, and a residual that still clears the server's
endpointer would keep interrupting replies. The canceller therefore decides per frame whether the microphone is
mostly echo, and attenuates the frames that are. The decision uses the echo return loss, the microphone level
minus the reference level. That ratio is a property of the room and the speaker volume, so it stays steady while
the reply's own loudness moves, and a person talking raises it. The canceller learns it from the first reply's
opening frames, then flags a frame as the person's speech when the ratio rises more than `erl_margin_db` above
the learned value. A flagged frame is sent with only the learned echo subtracted, so an interruption keeps its
level. An unflagged frame is attenuated toward `suppression_target_dbfs`, which keeps the endpointer quiet.

The opening of the first reply has no learned ratio yet, so it is suppressed for at least `warmup_ms` while the
ratio settles, and a frame there is passed only when it is more than `warmup_boost_db` louder than the playback
level, which means it is too loud to be echo. Later replies start with the ratio already known, so they are
classified from their first frame. When playback stops, the learned gain is held for `hangover_ms` to cover the
room's echo tail.

Data shape. `playback(pcm, rate, t)` takes reply PCM16 mono and the output callback's timestamp. `process(pcm, t)`
takes one microphone frame PCM16 mono and its capture timestamp, and returns the same number of samples cleaned.
`stats()` reports frame counts, the measured echo return loss enhancement, the learned echo return loss and the
last gain. `reset()` returns the object to its initial state. State is per object and never global. One lock keeps
`playback` safe on the audio callback while `process` runs on the event loop; the lock covers buffer copies and
the filter update only, not the convolution.

Limits. A linear canceller cannot remove speaker distortion at high volume, and it cannot cancel an echo path
longer than `tail_ms`. A person quieter than the echo plus `erl_margin_db` at the microphone can be taken for
echo and attenuated. A person who starts talking in the first few hundred milliseconds of a reply is suppressed
until the ratio settles. All three are measured against synthetic scenes in `tests/test_aec.py` and
`echo_bench.py`. This module adds no dependency; it uses the numpy the client already pins. No microphone or
reply audio is logged by this module.
"""

import math
import threading

import numpy as np

RATE = 16_000
FRAME_MS = 20
FRAME_BYTES = RATE * FRAME_MS // 1000 * 2
SILENT_DBFS = -100.0


def frame_dbfs(samples):
    """The RMS level of float samples in [-1, 1], in dBFS. Silence returns `SILENT_DBFS`."""
    if len(samples) == 0:
        return SILENT_DBFS
    rms = math.sqrt(float(np.mean(np.square(samples, dtype=np.float64))))
    return 20 * math.log10(rms) if rms > 0 else SILENT_DBFS


def decode(pcm):
    """PCM16 little-endian mono to float32 in [-1, 1], scaled by 32768 as the audio stack does."""
    return np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0


def encode(samples):
    """Float samples back to PCM16 little-endian mono, rounding rather than truncating."""
    return np.clip(np.rint(samples * 32768.0), -32768, 32767).astype("<i2").tobytes()


def resample(samples, rate_in, rate_out):
    """Linear resampling. The client plays at LFM3's output rate and the canceller works at 16 kHz."""
    if rate_in == rate_out or len(samples) == 0:
        return samples.astype(np.float32, copy=False)
    count = int(round(len(samples) * rate_out / rate_in))
    positions = np.arange(count) * rate_in / rate_out
    return np.interp(positions, np.arange(len(samples)), samples).astype(np.float32)


class _Timeline:
    """A fixed-length ring of mono samples indexed by absolute sample number on one clock.

    A read returns zeros for samples that were never written (before the stream started or ahead of the writer)
    and for samples the ring has already overwritten, so a far-future or far-past read is silence rather than
    stale audio from another moment.
    """

    def __init__(self, seconds, rate=RATE):
        self.rate = rate
        self.capacity = max(1, int(round(seconds * rate)))
        self.data = np.zeros(self.capacity, dtype=np.float32)
        self.newest = None

    def write(self, samples, start):
        if len(samples) == 0:
            return
        positions = start + np.arange(len(samples))
        self.data[positions % self.capacity] = samples
        self.newest = start + len(samples) if self.newest is None else max(self.newest, start + len(samples))

    def read(self, start, count):
        positions = start + np.arange(count)
        if self.newest is None:
            return np.zeros(count, dtype=np.float32)
        out = self.data[positions % self.capacity].copy()
        out[positions >= self.newest] = 0.0
        out[positions <= self.newest - self.capacity] = 0.0
        return out

    def clear(self):
        self.data[:] = 0.0
        self.newest = None


class EchoCanceller:
    """Removes the client's own playback from the microphone stream, frame by frame.

    `tail_ms` is the echo path the filter can model, from the output callback's timestamp to the microphone's.
    It must cover the output buffer, the speaker-to-microphone path and the room tail. The default 256 ms covers a
    120 ms delay plus a 100 ms tail in the tests.

    `warmup_ms`, `erl_margin_db` and `suppression_target_dbfs` are the tuning a different room might need. Raise
    `warmup_ms` for a long echo delay (a Bluetooth speaker), raise `erl_margin_db` if echo-only frames are taken
    for speech, and lower it if quiet interruptions are missed.
    """

    def __init__(self, rate=RATE, tail_ms=256, adapt=0.5, warmup_ms=250, warmup_settle_ms=160,
                 warmup_max_ms=None, hangover_ms=150, warmup_boost_db=6.0, erl_margin_db=3.0,
                 erl_rise_db_s=6.0, rel_smoothing=0.4, suppression_target_dbfs=-60.0,
                 max_suppression_db=50.0, active_dbfs=-55.0, reference_history_s=4.0,
                 microphone_history_s=2.0):
        if tail_ms <= 0:
            raise ValueError("tail_ms must be positive")
        if rate <= 0:
            raise ValueError("rate must be positive")
        if warmup_max_ms is None:
            warmup_max_ms = max(600, tail_ms + 400)
        self.rate = rate
        self.tail_ms = tail_ms
        self.taps = max(1, int(round(tail_ms * rate / 1000)))
        self.adapt = adapt
        self.warmup_frames = max(1, int(round(warmup_ms / FRAME_MS)))
        self.warmup_max_frames = max(self.warmup_frames, int(round(warmup_max_ms / FRAME_MS)))
        self.settle_frames = max(1, int(round(warmup_settle_ms / FRAME_MS)))
        self.hangover_frames = max(0, int(round(hangover_ms / FRAME_MS)))
        self.warmup_boost_db = warmup_boost_db
        self.erl_margin_db = erl_margin_db
        self.erl_rise_db_s = erl_rise_db_s
        self.rel_smoothing = rel_smoothing
        self.suppression_target_dbfs = suppression_target_dbfs
        self.max_suppression_db = max_suppression_db
        self.active_dbfs = active_dbfs
        self._reference = _Timeline(reference_history_s, rate)
        self._microphone = _Timeline(microphone_history_s, rate)
        self._weights = np.zeros(self.taps, dtype=np.float32)
        self._lock = threading.Lock()
        self.reset()

    def reset(self):
        """Drop the learned echo path and every frame count. The result is the object as first constructed."""
        with self._lock:
            self._weights = np.zeros(self.taps, dtype=np.float32)
            self._reference.clear()
            self._microphone.clear()
            self._reset_stats()

    def _reset_stats(self):
        self.frames = 0
        self.playing_frames = 0
        self.tail_frames = 0
        self.double_talk_frames = 0
        self.suppressed_frames = 0
        self.erle_db = None
        self.echo_loss_db = None
        self.last_relative_db = None
        self.last_gain_db = None
        self._erl_db = None
        self._relative_db = None
        self._warmup_frames_seen = 0
        self._warmup_max = None
        self._warmup_hold = 0
        self._hangover = 0
        self._was_active = False
        self._last_residual_db = None
        self._last_playing_gain_db = 0.0

    def playback(self, pcm, rate, t):
        """Add reply audio to the reference timeline. Safe on the output callback thread, and cheap.

        `t` is when the callback handed this chunk to the device, so the filter learns the output buffer, the
        acoustic delay and the input buffer as one impulse response. A pause in playback is a gap with no writes.
        """
        samples = decode(pcm)
        if samples.size == 0:
            return
        with self._lock:
            self._reference.write(resample(samples, rate, self.rate), int(round(t * self.rate)))

    def _classify(self, mic_db, reference_db, residual_db):
        """Return whether the person is talking over the reply, and the gain the frame should get."""
        relative_db = mic_db - reference_db
        self._relative_db = relative_db if self._relative_db is None else \
            (1 - self.rel_smoothing) * self._relative_db + self.rel_smoothing * relative_db
        if self._erl_db is None:
            return self._classify_warmup(mic_db, reference_db, residual_db)
        step = self.erl_rise_db_s * FRAME_MS / 1000
        double_talk = self._relative_db > self._erl_db + self.erl_margin_db
        if not double_talk:
            self._erl_db = min(max(self._relative_db, self._erl_db - step), self._erl_db + step)
        gain_db = 0.0 if double_talk else \
            min(0.0, max(-self.max_suppression_db, self.suppression_target_dbfs - residual_db))
        return double_talk, gain_db

    def _classify_warmup(self, mic_db, reference_db, residual_db):
        """The first reply has no learned echo loss yet: hold suppression while the ratio settles.

        The ratio is the frame's microphone level minus the reference level. Echo is a fixed attenuation of the
        reference, so the ratio climbs to its steady value as the room's echo arrives and then stops moving. A
        frame louder than the playback level by `warmup_boost_db` cannot be echo and is passed as speech.
        """
        self._warmup_frames_seen += 1
        if self._warmup_max is not None and self._relative_db > self._warmup_max + 0.5:
            self._warmup_max, self._warmup_hold = self._relative_db, 0
        elif self._warmup_max is None:
            self._warmup_max, self._warmup_hold = self._relative_db, 0
        else:
            self._warmup_hold += 1
        settled = self._warmup_hold >= self.settle_frames and self._warmup_frames_seen >= self.warmup_frames
        if settled or self._warmup_frames_seen >= self.warmup_max_frames:
            self._erl_db = self._warmup_max
        double_talk = mic_db > reference_db + self.warmup_boost_db
        if double_talk:
            return True, 0.0
        return False, min(0.0, max(-self.max_suppression_db, self.suppression_target_dbfs - residual_db))

    def process(self, pcm, t):
        """Return one microphone frame with the learned echo subtracted and residual echo suppressed.

        `t` is the frame's capture time, the last sample's time under the input stream's latency. The returned
        bytes have the same sample count as `pcm`. Frames with no reference audio are returned bit for bit.
        """
        samples = decode(pcm)
        count = samples.size
        if count == 0:
            return b""
        end = int(round(t * self.rate))
        with self._lock:
            self._microphone.write(samples, end - count)
            reference = self._reference.read(end - count - self.taps + 1, self.taps + count - 1)
            weights = self._weights.copy()
            if frame_dbfs(reference[-count:]) > self.active_dbfs:
                self._was_active = True
                self._hangover = self.hangover_frames
            else:
                self._was_active = False
                self._hangover = max(0, self._hangover - 1)
            active, hangover = self._was_active, not self._was_active and self._hangover > 0
            last_residual = self._last_residual_db
            last_gain = self._last_playing_gain_db

        echo = np.correlate(reference, weights, mode="valid")
        residual = samples - echo
        reference_db, residual_db, mic_db = frame_dbfs(reference[-count:]), frame_dbfs(residual), frame_dbfs(samples)
        double_talk, gain_db, erle = False, 0.0, None
        if active:
            erle = mic_db - residual_db
            double_talk, gain_db = self._classify(mic_db, reference_db, residual_db)
            gradient = np.correlate(reference, residual, mode="valid")
            energy = float(np.dot(reference, reference))
            if energy > 1e-9:
                step = self.adapt * (0.1 if double_talk else 1.0)
                weights = np.clip(weights + step * gradient / (energy + 1e-9), -8.0, 8.0)
        elif hangover:
            double_talk = last_residual is not None and residual_db > last_residual + 6.0
            gain_db = 0.0 if double_talk else last_gain

        with self._lock:
            self._weights = weights
            self.frames += 1
            self.last_relative_db = None if reference_db <= self.active_dbfs else round(
                self._relative_db if self._relative_db is not None else mic_db - reference_db, 1)
            self.last_gain_db = round(gain_db, 1)
            if active:
                self.playing_frames += 1
                self._last_residual_db, self._last_playing_gain_db = residual_db, gain_db
                if double_talk:
                    self.double_talk_frames += 1
                else:
                    self.erle_db = erle if self.erle_db is None else 0.9 * self.erle_db + 0.1 * erle
            elif hangover:
                self.tail_frames += 1
            if gain_db < 0:
                self.suppressed_frames += 1
            if self._erl_db is not None:
                self.echo_loss_db = round(self._erl_db, 1)

        if gain_db == 0.0:
            return encode(residual)
        return encode(residual * 10 ** (gain_db / 20))

    def stats(self):
        """A JSON-safe summary for the client log. Levels and counts only, never audio."""
        with self._lock:
            return {
                "tail_ms": self.tail_ms,
                "frames": self.frames,
                "playing_frames": self.playing_frames,
                "tail_frames": self.tail_frames,
                "double_talk_frames": self.double_talk_frames,
                "suppressed_frames": self.suppressed_frames,
                "erle_db": None if self.erle_db is None else round(self.erle_db, 1),
                "echo_loss_db": self.echo_loss_db,
                "last_relative_db": self.last_relative_db,
                "last_gain_db": self.last_gain_db,
            }
