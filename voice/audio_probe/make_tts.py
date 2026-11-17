"""Synthesize the 70 live-gate utterances with the Windows SAPI voice, clean and with noise, for the kind test (c).

Synthetic speech from a different voice than Mason's. Used only to compare audio against ASR text under degraded
input, never as acceptance evidence. Noise is white at 5 dB SNR over the speech (an assumption, not a recording).
"""
import json
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np

from voice.audio_probe import liquid as L

EVAL = Path(__file__).resolve().parents[1] / "segmentation" / "eval" / "live_gate.jsonl"
OUT = L.OUT / "tts"


def sapi(text, path, voice):
    ps = ("Add-Type -AssemblyName System.Speech; $s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
          f"$s.SelectVoice('{voice}'); $s.SetOutputToWaveFile('{path}'); $s.Speak($env:TEXT); $s.Dispose()")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True, env={**__import__("os").environ, "TEXT": text})


def voices():
    ps = "Add-Type -AssemblyName System.Speech; (New-Object System.Speech.Synthesis.SpeechSynthesizer).GetInstalledVoices() | % { $_.VoiceInfo.Name }"
    return subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True).stdout.split()


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    names = subprocess.run(["powershell", "-NoProfile", "-Command",
                            "Add-Type -AssemblyName System.Speech; (New-Object System.Speech.Synthesis.SpeechSynthesizer).GetInstalledVoices() | % { $_.VoiceInfo.Name }"],
                           capture_output=True, text=True).stdout.strip().splitlines()
    print("voices", names)
    voice = next((n for n in names if "Zira" in n), names[0]).strip()
    rng = np.random.default_rng(7)
    manifest = []
    for line in EVAL.read_text().splitlines():
        item = json.loads(line)
        raw = OUT / f"{item['id']}_raw.wav"
        sapi(item["utterance"], str(raw), voice)
        x, r = L.read_wav(raw)
        x = L.resample16k(x, r).astype(np.float32)
        lead = np.zeros(int(0.3 * 16000), np.float32)
        x = np.concatenate([lead, x, np.zeros(int(0.5 * 16000), np.float32)])
        power = np.mean(x[x != 0] ** 2) if np.any(x != 0) else 1.0
        noise = rng.normal(0, np.sqrt(power / 10 ** 0.5), len(x)).astype(np.float32)  # 5 dB SNR
        for tag, y in (("clean", x), ("noisy", x + noise)):
            y = np.clip(y, -32768, 32767).astype(np.int16)
            name = f"tts_{item['id']}_{tag}.wav"
            with wave.open(str(OUT / name), "wb") as w:
                w.setnchannels(1), w.setsampwidth(2), w.setframerate(16000)
                w.writeframes(y.tobytes())
            manifest.append({"set": "gate", "file": name, "id": item["id"], "cond": tag, "expect": item["expect"],
                             "text": item["utterance"], "context": item["context"], "category": item["category"]})
        raw.unlink()
    (OUT / "manifest.jsonl").write_text("\n".join(json.dumps(m) for m in manifest) + "\n")
    print(len(manifest), "clips, voice", voice)


if __name__ == "__main__":
    main()
