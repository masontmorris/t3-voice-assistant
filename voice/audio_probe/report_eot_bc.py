"""Print the end-of-turn and backchannel numbers for one results mode (audio, text, both)."""
import json
import sys

import numpy as np

from voice.audio_probe import liquid as L, stats

R = L.OUT / "results"


def load(name):
    p = R / f"{name}.jsonl"
    return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


def eot(mode):
    rows = load(f"eot_{mode}")
    for T in (150, 300, 500, 700):
        sub = [r for r in rows if r["pause_ms"] == T]
        if not sub:
            continue
        s, y = [r["probs"]["done"] for r in sub], [r["label"] for r in sub]
        print(f"eot {mode} T={T}ms n={len(y)} pos={sum(y)} auc={stats.auc(s, y):.3f}", stats.at(s, y, 0.5))
    s, y = [r["probs"]["done"] for r in rows], [r["label"] for r in rows]
    print(f"eot {mode} all n={len(y)} auc={stats.auc(s, y):.3f} median ms={np.median([r['ms'] for r in rows]):.0f}")


def bc(mode):
    rows = load(f"bc_{mode}")
    lab = lambda r: 1 if r["label"] == "backchannel" else 0
    s, y = [r["probs"]["backchannel"] for r in rows], [lab(r) for r in rows]
    print(f"bc {mode} all n={len(y)} bc={sum(y)} auc={stats.auc(s, y):.3f}", stats.at(s, y, 0.5))
    for w in (0.6, 1.0, 1.5):
        sub = [r for r in rows if r.get("window_s") in (w, None)]
        s, y = [r["probs"]["backchannel"] for r in sub], [lab(r) for r in sub]
        print(f"  window {w} n={len(y)} bc={sum(y)} auc={stats.auc(s, y):.3f}", stats.at(s, y, 0.5))


if __name__ == "__main__":
    for mode in sys.argv[1:] or ["audio"]:
        eot(mode)
        bc(mode)
