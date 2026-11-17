"""All the probe's numbers in one place, with bootstrap 95% intervals over items. python -m voice.research.audio_probe.summary"""
import json

import numpy as np

from voice.research.audio_probe import labels, liquid as L, stats
from voice.research.audio_probe.report_kind import fold

R = L.OUT / "results"
RNG = np.random.default_rng(0)


def ld(name):
    p = R / f"{name}.jsonl"
    return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


def boot(fn, rows, n=1000):
    vals = []
    for _ in range(n):
        idx = RNG.integers(0, len(rows), len(rows))
        v = fn([rows[i] for i in idx])
        if v == v:
            vals.append(v)
    return np.percentile(vals, [2.5, 97.5]) if vals else (float("nan"),) * 2


def auc_rows(score, label):
    return lambda rows: stats.auc([score(r) for r in rows], [label(r) for r in rows])


def line(name, rows, score, label):
    f = auc_rows(score, label)
    lo, hi = boot(f, rows)
    return f"{name}: n={len(rows)} AUC={f(rows):.3f} [{lo:.2f},{hi:.2f}]"


def eot():
    print("== (a) end of turn: P(done); label 1 = finished. text = d1 on the transcript the live ASR held at the cut")
    for backend, suffix in (("d1.2 hosted", ""), ("d1-omni-2B local", "_2b"), ("d1-omni-600M local", "_600m")):
        for mode, nm in (("audio", "audio"), ("text", "text(live)")):
            rows = ld(f"eot_{mode}{suffix}")
            if not rows:
                continue
            for T in (150, 300, 500):
                sub = [r for r in rows if r["pause_ms"] == T]
                print(f"{backend} {nm} T={T}: " + line("", sub, lambda r: r["probs"]["done"], lambda r: r["label"]))
            print(f"{backend} {nm} all T: " + line("", rows, lambda r: r["probs"]["done"], lambda r: r["label"]),
                  f"ms p50={np.median([r['ms'] for r in rows]):.0f}")
    for name in ("eot_both", "eot_text_final", "eot_both_final", "eot_audio_tail3"):
        rows = ld(name)
        print(name, line("", rows, lambda r: r["probs"]["done"], lambda r: r["label"]),
              f"ms p50={np.median([r['ms'] for r in rows]):.0f}")


def bc():
    print("== (b) backchannel against interruption: P(backchannel); label 1 = backchannel")
    lab = lambda r: 1 if r["label"] == "backchannel" else 0
    for backend, suffix in (("d1.2", ""), ("2B", "_2b"), ("600M", "_600m")):
        for mode in ("audio", "text"):
            rows = ld(f"bc_{mode}{suffix}")
            if not rows:
                continue
            for group, pick in (("real (Mason) only", lambda r: r["kind"] in ("real_turn_opening", "real_backchannel", "real_first_word", "dismissed_overlap")),
                                ("synthetic only", lambda r: r["kind"] in ("synth_tts", "sapi")), ("all", lambda r: True)):
                sub = [r for r in rows if pick(r)]
                print(f"{backend} {mode} {group}: " + line("", sub, lambda r: r["probs"]["backchannel"], lab),
                      "| @0.5", {k: round(v, 2) if isinstance(v, float) else v for k, v in stats.at([r["probs"]["backchannel"] for r in sub], [lab(r) for r in sub]).items()})
    rows = ld("bc_audio")
    for w in (0.6, 1.0, 1.5):
        ints = [r for r in rows if r["kind"] == "real_turn_opening" and r["window_s"] == w]
        print(f"d1.2 audio: real turn openings read as backchannel at {w}s: {sum(r['probs']['backchannel'] >= .5 for r in ints)}/{len(ints)}")
    text = ld("bc_text")
    for w in (0.6, 1.0, 1.5):
        ints = [r for r in text if r["kind"] == "real_turn_opening" and r["window_s"] == w]
        print(f"d1.2 text(live): real turn openings read as backchannel at {w}s: {sum(r['probs']['backchannel'] >= .5 for r in ints)}/{len(ints)}; empty transcript {sum(not r['text'] for r in ints)}/{len(ints)}")


def kind():
    print("== (c) kind on Mason's real turns (hand labels), then the 70 synthetic gate items")
    for backend, suffix in (("d1.2", ""), ("2B", "_2b"), ("600M", "_600m")):
        for mode in ("audio", "text", "both"):
            rows = [r for r in ld(f"kind_{mode}{suffix}") if labels.KIND.get(r["src"]) and r["probs"]]
            if not rows:
                continue
            acc = lambda rr: np.mean([fold(r["choice"]) == labels.KIND[r["src"]] for r in rr])
            lo, hi = boot(acc, rows)
            print(f"{backend} {mode}: n={len(rows)} acc={acc(rows):.3f} [{lo:.2f},{hi:.2f}]",
                  line("request", rows, lambda r: r["probs"]["request"], lambda r: int(labels.KIND[r["src"]] == "request")))
    for tag in ("hosted", "2b", "600m"):
        rows = ld(f"gate_{tag}")
        for cond, mode in (("clean", "oracle"), ("clean", "asr"), ("clean", "audio"), ("noisy", "asr"), ("noisy", "audio")):
            sub = [r for r in rows if r["cond"] == cond and r["mode"] == mode and r["choice"]]
            if sub:
                acc = lambda rr: np.mean([fold(r["choice"]) == r["gold"] for r in rr])
                lo, hi = boot(acc, sub)
                print(f"gate {tag} {cond}/{mode}: n={len(sub)} acc={acc(sub):.3f} [{lo:.2f},{hi:.2f}] median ms={np.median([r['ms'] for r in sub]):.0f}")


def latency():
    print("== latency (ms, one request in flight, this machine)")
    for name in ("eot_audio", "eot_text", "bc_audio", "kind_audio", "kind_text", "eot_audio_2b", "eot_text_2b", "bc_audio_2b",
                 "kind_audio_2b", "eot_audio_600m", "bc_audio_600m", "kind_audio_600m"):
        rows = ld(name)
        if rows:
            ms = [r["ms"] for r in rows]
            print(f"{name}: n={len(rows)} p50={np.median(ms):.0f} p90={np.percentile(ms, 90):.0f} max={max(ms):.0f}",
                  f"tokens p50={np.median([r['tokens'] for r in rows if r.get('tokens')] or [0]):.0f}")


if __name__ == "__main__":
    eot()
    bc()
    kind()
    latency()
