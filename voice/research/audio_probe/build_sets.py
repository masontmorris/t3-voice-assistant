"""Cut the probe's clip sets from the recorded live calls and the barge-in evidence.

End-of-turn (eot): each recorded turn cut at its speech end plus T seconds of silence (finished), and each internal
pause cut at its start plus T (continues), for T in 0.15, 0.3, 0.5, 0.7 that the real silence allows.
Backchannel (bc): short real and synthetic acknowledgements against real turn openings cut at 0.6, 1.0 and 1.5 s.
Kind: the full real turns, hand-labelled in labels.py.
Writes 16 kHz mono WAVs and manifest.jsonl under $VOICE_HOME/research/audio-decider/clips.
"""
import glob
import json
import re
import sys
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from voice.research.audio_probe import liquid as L, vad  # noqa: E402

EV = L.HOME / "evidence" / "live"
OUT = L.OUT / "clips"
TS = (0.15, 0.3, 0.5, 0.7)
WINDOWS = (0.6, 1.0, 1.5)
MERGED = {8, 9}  # turn 10 is the same utterance, continued; its pauses cover them


def write(name, x):
    OUT.mkdir(parents=True, exist_ok=True)
    with wave.open(str(OUT / name), "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(16000)
        w.writeframes(x.astype(np.int16).tobytes())


def load(path):
    x, r = L.read_wav(path)
    return L.resample16k(x, r)


def main():
    rows = []
    sources = [(f"a{n:03d}", EV / "20261116-152653" / f"turn_{n:03d}_input.wav", n) for n in range(1, 38)] + \
              [(f"b{n:03d}", EV / "20261116-080326" / f"turn_{n:03d}_input.wav", n) for n in (2, 4, 6)]
    for sid, path, n in sources:
        if sid.startswith("a") and n in MERGED:
            continue
        x = load(path)
        spans, pauses = vad.speech_and_pauses(x)
        end = spans[-1][1]
        for t in TS:
            stop = int((end + t) * 16000)
            if stop <= len(x):
                name = f"eot_{sid}_end_{int(t * 1000)}.wav"
                write(name, x[:stop])
                rows.append({"set": "eot", "file": name, "src": sid, "label": 1, "pause_ms": int(t * 1000),
                             "cut_s": round(stop / 16000, 2), "pause_kind": "turn_end"})
        for i, (ps, pe) in enumerate(pauses):
            for t in TS:
                if t <= pe - ps + 1e-6:
                    name = f"eot_{sid}_p{i}_{int(t * 1000)}.wav"
                    write(name, x[: int((ps + t) * 16000)])
                    rows.append({"set": "eot", "file": name, "src": sid, "label": 0, "pause_ms": int(t * 1000),
                                 "cut_s": round(ps + t, 2), "pause_kind": "mid", "pause_len_s": round(pe - ps, 2)})
        rows.append({"set": "kind", "file": f"kind_{sid}.wav", "src": sid})
        write(f"kind_{sid}.wav", x)
        # turn openings, for the barge-in set (turn must carry speech beyond 1.5 s or the window is the whole clip)
        onset = spans[0][0]
        for w in WINDOWS:
            stop = int((onset + w) * 16000)
            name = f"bc_{sid}_open_{int(w * 1000)}.wav"
            write(name, x[:stop])
            short = sid in ("a014", "a034")  # "Okay." and "Okay, thanks." are Mason's own real backchannels
            rows.append({"set": "bc", "file": name, "src": sid, "label": "backchannel" if short else "interrupt",
                         "window_s": w, "full_s": round(end - onset, 2),
                         "kind": "real_backchannel" if short else "real_turn_opening"})
    for f in sorted(glob.glob(str(EV / "bargein" / "bench" / "clips" / "*.wav"))):
        x = load(f)
        name = "bc_real_" + Path(f).stem.split("_")[-1] + ".wav"
        write(name, x)
        rows.append({"set": "bc", "file": name, "src": Path(f).stem, "label": "backchannel", "kind": "real_first_word"})
    for f in sorted(glob.glob(str(EV / "bargein" / "gate1" / "server" / "overlap_*.wav"))):
        name = f"bc_overlap_{Path(f).stem.split('_')[-1]}.wav"
        write(name, load(f))
        rows.append({"set": "bc", "file": name, "src": Path(f).stem, "label": "backchannel", "kind": "dismissed_overlap"})
    manifest = json.loads((EV / "bargein" / "synth" / "manifest.json").read_text())
    for t in manifest["turns"]:
        name = f"bc_synth_{t['turn']:03d}.wav"
        write(name, load(EV / "bargein" / "synth" / t["file"]))
        lab = "interrupt" if t["turn"] in (1, 10, 11, 12, 13) else "backchannel"
        rows.append({"set": "bc", "file": name, "src": f"synth{t['turn']}", "label": lab, "kind": "synth_tts",
                     "text": t["text"]})
    (OUT / "manifest.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    from collections import Counter
    print(Counter((r["set"], r.get("label"), r.get("kind", r.get("pause_kind"))) for r in rows))


if __name__ == "__main__":
    main()
