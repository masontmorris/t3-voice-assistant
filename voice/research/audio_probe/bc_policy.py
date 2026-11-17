"""Barge-in policy replay. Rule baseline: stop the reply when the window holds 600 ms or more of voiced audio (the
shipped voiced threshold, measured here with the probe's energy VAD, so approximate). Audio policy: stop unless the decider
puts P(backchannel) at or above theta."""
import json

import numpy as np

from voice.research.audio_probe import liquid as L, vad

R = L.OUT / "results"
CLIPS = L.OUT / "clips"


def rows(name):
    return [json.loads(l) for l in (R / f"{name}.jsonl").read_text().splitlines()]


def voiced_ms(file):
    x, rate = L.read_wav(CLIPS / file)
    return 20 * int(vad.voiced(x, rate).sum())


def main():
    groups = {"real backchannel (Okay x2 and 3 first words, plus 9 dismissed overlaps)": lambda r: r["label"] == "backchannel" and r["kind"] in ("real_backchannel", "real_first_word", "dismissed_overlap"),
              "short synthetic backchannel (13 TTS)": lambda r: r["label"] == "backchannel" and r["kind"] == "synth_tts",
              "long synthetic backchannel (20 SAPI x3 windows)": lambda r: r["label"] == "backchannel" and r["kind"] == "sapi",
              "real turn openings 0.6 s": lambda r: r["kind"] == "real_turn_opening" and r["window_s"] == 0.6,
              "real turn openings 1.0 s": lambda r: r["kind"] == "real_turn_opening" and r["window_s"] == 1.0,
              "real turn openings 1.5 s": lambda r: r["kind"] == "real_turn_opening" and r["window_s"] == 1.5,
              "synthetic interruptions (5 TTS + 20 SAPI x3)": lambda r: r["label"] == "interrupt" and r["kind"] in ("synth_tts", "sapi")}
    for backend in ("audio", "audio_2b"):
        data = rows("bc_" + backend)
        for r in data:
            r["voiced_ms"] = voiced_ms(r["file"])
        print(f"== {backend}: share of each group DISMISSED (reply keeps playing). Backchannel groups should be high, interrupt groups low")
        print(f"{'group':62s} n   rule600  " + "  ".join(f"th{t}" for t in (0.5, 0.7, 0.8, 0.9)))
        for name, pick in groups.items():
            sub = [r for r in data if pick(r)]
            rule = np.mean([r["voiced_ms"] < 600 for r in sub])
            cols = [np.mean([r["probs"]["backchannel"] >= t for r in sub]) for t in (0.5, 0.7, 0.8, 0.9)]
            print(f"{name:62s} {len(sub):3d} {rule:7.2f}  " + "  ".join(f"{c:5.2f}" for c in cols))


if __name__ == "__main__":
    main()
