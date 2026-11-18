"""Whether two sets of clips are the same speaker: cosine similarity of speaker embeddings, per pair of clip groups.

    python -m voice.research.t3_voice.speaker DIR --weights ve.safetensors

The embedding is Chatterbox's voice encoder (`ve.safetensors` from `ResembleAI/chatterbox`, MIT): a three-layer LSTM
over 40 mel bands at 16 kHz, its last state projected to 256 values, averaged over 1.6 s windows that step 0.8 s.
Clips are grouped by name up to the last underscore (`tts_03.wav` is in `tts`), and every pair of clips from two
groups is scored. The metric separates speakers on LFM3's own voices: TTS `female` against `female` 0.89, `male`
against `male` 0.89, `female` against `male` 0.66 (measured, docs/voice/t3-same-voice.md).
"""

import argparse
import json
from pathlib import Path

import librosa
import numpy as np
import soundfile as sf
import torch
from safetensors.torch import load_file

RATE, WINDOW, STEP = 16_000, 160, 80


class VoiceEncoder(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.lstm = torch.nn.LSTM(40, 256, num_layers=3, batch_first=True)
        self.proj = torch.nn.Linear(256, 256)

    def forward(self, mels):
        _, (hidden, _) = self.lstm(mels)
        embeds = torch.relu(self.proj(hidden[-1]))
        return embeds / embeds.norm(dim=1, keepdim=True)


def mels(samples):
    power = np.abs(librosa.stft(samples, n_fft=400, hop_length=160, win_length=400)) ** 2
    return (librosa.filters.mel(sr=RATE, n_fft=400, n_mels=40, fmin=0, fmax=8000) @ power).T.astype(np.float32)


def embed(model, path):
    samples, rate = sf.read(str(path), dtype="float32")
    frames = mels(librosa.resample(samples, orig_sr=rate, target_sr=RATE))
    windows = [frames[start:start + WINDOW] for start in range(0, max(1, len(frames) - WINDOW + 1), STEP)]
    windows = np.stack([np.pad(w, ((0, WINDOW - len(w)), (0, 0))) for w in windows])
    with torch.no_grad():
        mean = model(torch.from_numpy(windows)).mean(0)
    return (mean / mean.norm()).numpy()


def similarities(folder, weights):
    model = VoiceEncoder()
    model.load_state_dict({k: v for k, v in load_file(weights).items() if not k.startswith("similarity")})
    model.eval()
    groups = {}
    for path in sorted(Path(folder).glob("*.wav")):
        if path.stat().st_size > 2000:
            groups.setdefault(path.stem.rsplit("_", 1)[0], {})[path.stem] = embed(model, path)
    table = {}
    for a, left in groups.items():
        for b, right in groups.items():
            scores = [float(x @ y) for i, x in left.items() for j, y in right.items() if i != j]
            if scores:
                table.setdefault(a, {})[b] = {"median": round(float(np.median(scores)), 3),
                                              "min": round(min(scores), 3), "max": round(max(scores), 3),
                                              "pairs": len(scores)}
    return table


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("folder", type=Path)
    parser.add_argument("--weights", type=Path, required=True, help="Chatterbox's ve.safetensors")
    args = parser.parse_args()
    table = similarities(args.folder, args.weights)
    for a, row in table.items():
        print(f"{a:12s}", "  ".join(f"{b} {cell['median']}" for b, cell in row.items()))
    (args.folder / "speaker.json").write_text(json.dumps(table, indent=1) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
