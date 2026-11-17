"""Synthetic long backchannels and interruptions (Windows SAPI, two voices), cut at 0.6, 1.0 and 1.5 s after onset.

Mason's recordings hold no long "mm-hm" over a playing reply, which is the case the 600 ms voiced gate gets wrong, so this
is the only test of it. Synthetic and from a different voice; indicative only. Appends to the bc clip manifest.
"""
import json
import os
import subprocess
import wave

import numpy as np

from voice.research.audio_probe import liquid as L, vad

BACK = ["Mm-hmm, yeah, I see.", "Yeah, yeah, right, right.", "Okay, okay, got it, uh huh.", "Right, mm hmm, sure.",
        "Uh-huh, yes, I understand.", "Yeah, that makes sense.", "Okay, sounds good.", "Oh, I see, right.",
        "Sure, sure, go on.", "Mm-hmm, mm-hmm, yeah."]
INT = ["Wait, hold on a second.", "Actually, can I ask you something?", "No, that's not what I meant.", "Stop, stop.",
       "Sorry, what was that?", "Hang on, that's wrong.", "Hey, can you repeat that?", "Let me stop you there.",
       "I have a question.", "That's not right."]
CLIPS = L.OUT / "clips"


def sapi(text, path, voice, rate):
    ps = ("Add-Type -AssemblyName System.Speech; $s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
          f"$s.SelectVoice('{voice}'); $s.Rate = {rate}; $s.SetOutputToWaveFile('{path}'); $s.Speak($env:TEXT); $s.Dispose()")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True, env={**os.environ, "TEXT": text})


def main():
    rows = [json.loads(l) for l in (CLIPS / "manifest.jsonl").read_text().splitlines() if l]
    rows = [r for r in rows if r.get("kind") != "sapi"]
    for label, phrases in (("backchannel", BACK), ("interrupt", INT)):
        for i, text in enumerate(phrases):
            for voice in ("Microsoft Zira Desktop", "Microsoft David Desktop"):
                tmp = CLIPS / "tmp_sapi.wav"
                sapi(text, str(tmp), voice, -2)
                x, r = L.read_wav(tmp)
                x = L.resample16k(x, r)
                tmp.unlink()
                spans, _ = vad.speech_and_pauses(x)
                onset = spans[0][0]
                for w in (0.6, 1.0, 1.5):
                    name = f"bc_sapi_{label[:2]}{i}_{voice.split()[1][0]}_{int(w * 1000)}.wav"
                    seg = x[: int((onset + w) * 16000)]
                    with wave.open(str(CLIPS / name), "wb") as f:
                        f.setnchannels(1), f.setsampwidth(2), f.setframerate(16000)
                        f.writeframes(seg.astype(np.int16).tobytes())
                    rows.append({"set": "bc", "file": name, "src": f"sapi_{label[:2]}{i}_{voice.split()[1][0]}",
                                 "label": label, "window_s": w, "full_s": round(spans[-1][1] - onset, 2),
                                 "kind": "sapi", "text": text})
    (CLIPS / "manifest.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    print(sum(1 for r in rows if r.get("kind") == "sapi"), "sapi clips")


if __name__ == "__main__":
    main()
