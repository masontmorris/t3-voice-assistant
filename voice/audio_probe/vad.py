"""Energy VAD for the probe: 20 ms frames, threshold from the clip's own noise floor and speech level."""
import numpy as np


def frames(x, rate=16000, ms=20):
    n = rate * ms // 1000
    k = len(x) // n
    f = x[: k * n].astype(np.float32).reshape(k, n)
    return np.sqrt((f ** 2).mean(axis=1)) + 1e-6


def voiced(x, rate=16000, ms=20):
    e = frames(x, rate, ms)
    db = 20 * np.log10(e)
    floor, top = np.percentile(db, 10), np.percentile(db, 95)
    thr = floor + 0.35 * (top - floor)
    return db > thr


def runs(mask, ms=20):
    """(start_s, end_s, value) runs of a boolean frame mask."""
    out, start = [], 0
    for i in range(1, len(mask) + 1):
        if i == len(mask) or mask[i] != mask[start]:
            out.append((start * ms / 1000, i * ms / 1000, bool(mask[start])))
            start = i
    return out


def speech_and_pauses(x, rate=16000, min_pause=0.25, join=0.12):
    """Speech spans with gaps shorter than `join` bridged, and the internal pauses at least `min_pause` long."""
    m = voiced(x, rate)
    r = runs(m)
    spans = []
    for s, e, v in r:
        if v:
            if spans and s - spans[-1][1] < join:
                spans[-1][1] = e
            else:
                spans.append([s, e])
    spans = [(s, e) for s, e in spans if e - s >= 0.08]
    pauses = [(spans[i][1], spans[i + 1][0]) for i in range(len(spans) - 1) if spans[i + 1][0] - spans[i][1] >= min_pause]
    return spans, pauses
