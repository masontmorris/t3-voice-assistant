"""Splits one recording of a read script into one 16 kHz clip per spoken line, at the pauses between lines.

    python -m voice.live.split_takes TAKE.wav OUT_DIR [--min-gap-s 1.2] [--expect N]

Each clip is written as turn_001.wav, turn_002.wav, ... and can be fed to `client.py --wav OUT_DIR/turn_*.wav`, which
replays them through the live server as one call. A pause shorter than `--min-gap-s` stays inside a line, so a
mid-sentence hesitation does not split it. `--expect` fails the run when the count differs from the script.
"""

import argparse
import sys
import wave
from pathlib import Path

import numpy as np

RATE = 16000
FRAME = RATE // 50


def read_mono_16k(path):
    with wave.open(str(path), "rb") as wav:
        rate, channels, width = wav.getframerate(), wav.getnchannels(), wav.getsampwidth()
        raw = wav.readframes(wav.getnframes())
    dtype = {2: "<i2", 4: "<i4"}[width]
    samples = np.frombuffer(raw, dtype=dtype).astype(np.float64).reshape(-1, channels).mean(axis=1)
    samples /= 32768.0 if width == 2 else 2147483648.0
    if rate != RATE:
        samples = np.interp(np.arange(0, len(samples), rate / RATE), np.arange(len(samples)), samples)
    return samples


def speech_spans(samples, min_gap_s=1.2, pad_s=0.25, min_speech_s=0.3):
    """(start, end) sample ranges of speech, separated by at least `min_gap_s` of quiet. A 20 ms frame is quiet below
    12 dB over the noise floor (the 10th percentile of frame levels) or 35 dB under the speech level (the 95th),
    whichever is higher, so both a gated near-silent recording and a hissy raw one split at their pauses."""
    count = len(samples) // FRAME
    levels = 20 * np.log10(np.sqrt(np.mean(samples[:count * FRAME].reshape(count, FRAME) ** 2, axis=1)) + 1e-9)
    loud = levels > max(np.percentile(levels, 10) + 12, np.percentile(levels, 95) - 35)
    spans, start, quiet = [], None, 0
    gap_frames = int(min_gap_s * 50)
    for index, is_loud in enumerate(loud):
        if is_loud:
            start = index if start is None else start
            quiet = 0
        elif start is not None:
            quiet += 1
            if quiet >= gap_frames:
                spans.append((start, index - quiet + 1))
                start, quiet = None, 0
    if start is not None:
        spans.append((start, count - quiet))
    pad = int(pad_s * 50)
    return [(max(0, a - pad) * FRAME, min(count, b + pad) * FRAME) for a, b in spans if (b - a) / 50 >= min_speech_s]


def write_clip(path, samples):
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(RATE)
        wav.writeframes(np.clip(np.rint(samples * 32767), -32768, 32767).astype("<i2").tobytes())


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("take", type=Path)
    parser.add_argument("out", type=Path)
    parser.add_argument("--min-gap-s", type=float, default=1.2)
    parser.add_argument("--expect", type=int)
    args = parser.parse_args()
    samples = read_mono_16k(args.take)
    spans = speech_spans(samples, args.min_gap_s)
    args.out.mkdir(parents=True, exist_ok=True)
    for number, (start, end) in enumerate(spans, 1):
        write_clip(args.out / f"turn_{number:03d}.wav", samples[start:end])
        print(f"turn_{number:03d}.wav  {start / RATE:7.2f} s  {(end - start) / RATE:5.2f} s")
    if args.expect is not None and len(spans) != args.expect:
        print(f"found {len(spans)} lines, the script has {args.expect}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
