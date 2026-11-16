"""Kind from the first W seconds of speech: d1.2 on the audio against d1.2 on the live partial transcript at that moment.

Tests candidate win (d): deciding before the transcript has caught up. Only turns whose speech runs past onset + W + 0.3 s
(so the cut is mid-speech) and that carry a hand label.
"""
import json
import sys

from voice.audio_probe import labels, liquid as L, questions as Q, stats, vad
from voice.audio_probe.report_kind import fold

CLIPS = L.OUT / "clips"


def main(windows=(1.0, 2.0, 3.0)):
    timing = {t["file"]: t for t in json.loads((L.OUT / "asr_timing.json").read_text())}
    c = L.Client()
    rows = []
    for fname, t in timing.items():
        src = fname.removeprefix("kind_").removesuffix(".wav")
        gold = labels.KIND.get(src)
        if gold is None or src in ("a008", "a009"):
            continue
        x, rate = L.read_wav(CLIPS / fname)
        for w in windows:
            cut = t["onset_s"] + w
            if t["speech_end_s"] < cut + 0.3:
                continue
            text = "".join(d for at, d in t["deltas"] if at <= cut).strip()
            audio = c.call({"audio": [L.wav_b64(x[: int(cut * rate)], rate)], "state": None, "questions": Q.kind()}, "early-audio")
            txt = c.call({"state": {"transcript": text, "transcript_is_final": False}, "questions": Q.kind(text)}, "early-text")
            for mode, rec in (("audio", audio), ("text", txt)):
                a = rec["response"]["answers"]["s1_kind"]
                rows.append({"src": src, "window": w, "mode": mode, "gold": gold, "pred": fold(a["choice"]),
                             "p_request": a["probabilities"]["request"], "text": text if mode == "text" else None})
    (L.OUT / "results" / "kind_early.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    for w in windows:
        for mode in ("audio", "text"):
            sub = [r for r in rows if r["window"] == w and r["mode"] == mode]
            if sub:
                acc = sum(r["gold"] == r["pred"] for r in sub) / len(sub)
                auc = stats.auc([r["p_request"] for r in sub], [int(r["gold"] == "request") for r in sub])
                print(f"W={w}s {mode}: n={len(sub)} acc={acc:.3f} request AUC={auc:.3f} requests={sum(r['gold'] == 'request' for r in sub)}")


if __name__ == "__main__":
    main()
