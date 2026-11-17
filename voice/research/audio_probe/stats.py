"""Small metrics for the probe: AUC and accuracy at a fixed threshold."""
import numpy as np


def auc(scores, labels):
    s, y = np.asarray(scores, float), np.asarray(labels, int)
    pos, neg = s[y == 1], s[y == 0]
    if len(pos) == 0 or len(neg) == 0:
        return float("nan")
    return float(((pos[:, None] > neg[None, :]).sum() + 0.5 * (pos[:, None] == neg[None, :]).sum()) / (len(pos) * len(neg)))


def at(scores, labels, thr=0.5):
    s, y = np.asarray(scores, float), np.asarray(labels, int)
    pred = s >= thr
    tp, fn = int(((pred) & (y == 1)).sum()), int(((~pred) & (y == 1)).sum())
    tn, fp = int(((~pred) & (y == 0)).sum()), int(((pred) & (y == 0)).sum())
    return {"tpr": tp / max(tp + fn, 1), "tnr": tn / max(tn + fp, 1), "tp": tp, "fn": fn, "tn": tn, "fp": fp}
