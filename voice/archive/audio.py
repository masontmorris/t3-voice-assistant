"""WAV reading, clip location by cross-correlation, and waveform peaks, shared by the exporter and the validator."""

import hashlib
import wave
from pathlib import Path

import numpy as np

PEAK_FPS = 100


def read_wav(path):
    """Mono PCM16 as float32 in [-1, 1], and its rate."""
    with wave.open(str(path), "rb") as wav:
        if wav.getnchannels() != 1 or wav.getsampwidth() != 2:
            raise ValueError(f"{path}: expected mono PCM16")
        data = np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2")
        return data.astype(np.float32) / 32768.0, wav.getframerate()


def write_wav(path, samples, rate):
    pcm = np.clip(np.round(np.asarray(samples) * 32767.0), -32768, 32767).astype("<i2")
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(pcm.tobytes())


def duration_s(path):
    with wave.open(str(path), "rb") as wav:
        return wav.getnframes() / wav.getframerate()


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def resample(samples, rate, target):
    if rate == target or samples.size == 0:
        return samples
    count = int(round(samples.size * target / rate))
    return np.interp(np.arange(count) * rate / target, np.arange(samples.size), samples).astype(np.float32)


def locate(needle, haystack, rate, lo_s, hi_s):
    """Where `needle` starts inside haystack[lo_s, hi_s + len(needle)), in seconds, and the normalized peak.

    Both are float arrays at `rate`. The score is the correlation at the peak divided by the two energies, so 1.0 is
    an exact copy and anything under about 0.5 is not a confident match."""
    lo = max(0, int(lo_s * rate))
    hi = min(haystack.size, int(hi_s * rate) + needle.size)
    window = haystack[lo:hi]
    if needle.size == 0 or window.size < needle.size or not np.any(needle):
        return None, 0.0
    size = 1 << int(np.ceil(np.log2(window.size + needle.size)))
    corr = np.fft.irfft(np.fft.rfft(window, size) * np.conj(np.fft.rfft(needle, size)), size)[:window.size - needle.size + 1]
    energy = np.concatenate(([0.0], np.cumsum(window.astype(np.float64) ** 2)))
    local = energy[needle.size:] - energy[:-needle.size]
    score = corr / (np.sqrt(np.maximum(local, 1e-12)) * np.linalg.norm(needle))
    best = int(np.argmax(score))
    return (lo + best) / rate, float(score[best])


def peaks(samples, rate, fps=PEAK_FPS, frames=None):
    """RMS per 1/fps bucket, rounded for a small JSON file."""
    step = rate / fps
    count = frames if frames is not None else int(np.ceil(samples.size / step))
    out = np.zeros(count, dtype=np.float64)
    for i in range(count):
        chunk = samples[int(i * step):int((i + 1) * step)]
        if chunk.size:
            out[i] = np.sqrt(np.mean(chunk.astype(np.float64) ** 2))
    return [round(float(v), 4) for v in out]


def place(tracks, rate, total_s):
    """Mix (samples, track_rate, offset_s) tracks onto one timeline at `rate`."""
    mix = np.zeros(int(np.ceil(total_s * rate)), dtype=np.float32)
    for samples, track_rate, offset_s in tracks:
        data = resample(samples, track_rate, rate)
        start = int(round(offset_s * rate))
        end = min(mix.size, start + data.size)
        if end > start:
            mix[start:end] += data[:end - start]
    return mix


def files_under(root):
    return sorted(p for p in Path(root).rglob("*") if p.is_file())
