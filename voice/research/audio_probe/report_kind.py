"""Kind accuracy against the hand labels, for audio, text and both, hosted d1.2 or a local run."""
import json
import sys
from collections import Counter

from voice.research.audio_probe import labels, liquid as L, stats

R = L.OUT / "results"


def fold(c):
    return {"request": "request", "correction": "correction"}.get(c, "conversation")


def report(path, name):
    rows = [json.loads(l) for l in path.read_text().splitlines()]
    rows = [r for r in rows if labels.KIND.get(r["src"]) is not None and r["probs"]]
    gold = [labels.KIND[r["src"]] for r in rows]
    pred = [fold(r["choice"]) for r in rows]
    acc = sum(g == p for g, p in zip(gold, pred)) / len(rows)
    req = [1 if g == "request" else 0 for g in gold]
    auc = stats.auc([r["probs"]["request"] for r in rows], req)
    print(f"{name}: n={len(rows)} 3-way acc={acc:.3f} request AUC={auc:.3f} confusion={dict(Counter(zip(gold, pred)))}")
    return rows


if __name__ == "__main__":
    for f in sys.argv[1:]:
        report(R / f, f)
