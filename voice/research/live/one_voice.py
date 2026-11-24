"""Is the assistant still one voice after a barge-in trim? Speaker embeddings of the replies, compared.

    python -m voice.research.live.one_voice DIR/off DIR/on [--other DIR/on/clips-male]

For each call `trim_eval` scored, the stopped reply and the reply after the barge-in are embedded with
microsoft/wavlm-base-plus-sv (an x-vector head on WavLM, run on the CPU) and compared by cosine similarity. Two
references frame the number: every pair of stopped replies across calls of the same arm (one voice, other words), and
each stopped reply against `--other`, clips of the server's own TTS in its other voice (a second voice, which the call
must never have). The same voice throughout means the after-trim pairs sit with the first reference and well clear of
the second. The model is fetched once from Hugging Face into $HF_HOME; the audio never leaves the machine.
"""

import argparse
import itertools
import json
import statistics
from pathlib import Path

import numpy as np
import soundfile as sf
import torch
from transformers import AutoFeatureExtractor, WavLMForXVector

MODEL = "microsoft/wavlm-base-plus-sv"
RATE = 16_000
MAX_S = 12.0


def load(path):
    samples, rate = sf.read(str(path), dtype="float32", always_2d=True)
    mono = samples.mean(axis=1)
    if rate != RATE:
        positions = np.arange(0, len(mono), rate / RATE)
        mono = np.interp(positions, np.arange(len(mono)), mono).astype("float32")
    return mono[:int(MAX_S * RATE)]


class Embedder:
    def __init__(self):
        self.extract = AutoFeatureExtractor.from_pretrained(MODEL)
        self.model = WavLMForXVector.from_pretrained(MODEL).eval()
        self.cache = {}

    def __call__(self, path):
        if path not in self.cache:
            inputs = self.extract(load(path), sampling_rate=RATE, return_tensors="pt")
            with torch.no_grad():
                vector = self.model(**inputs).embeddings[0]
            self.cache[path] = torch.nn.functional.normalize(vector, dim=-1)
        return self.cache[path]


def cosine(embed, a, b):
    return round(float(embed(a) @ embed(b)), 4)


def spread(values):
    return {"n": len(values), "min": min(values), "median": statistics.median(values), "max": max(values)} if values \
        else {"n": 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("arms", type=Path, nargs="+")
    parser.add_argument("--other", type=Path, help="clips of a second voice, as a reference")
    args = parser.parse_args()
    embed, report = Embedder(), {}
    others = sorted(args.other.glob("turn_*.wav")) if args.other else []
    for arm in args.arms:
        rows = json.loads((arm / "score.json").read_text(encoding="utf-8"))
        pairs = [r["case"]["reply_wavs"] for r in rows if r.get("case") and all(Path(p).is_file()
                                                                                   for p in r["case"]["reply_wavs"])]
        stopped = [a for a, _ in pairs]
        report[arm.name] = {
            "stopped_vs_next": spread([cosine(embed, a, b) for a, b in pairs]),
            "stopped_vs_stopped": spread([cosine(embed, a, b) for a, b in itertools.combinations(stopped, 2)]),
            "stopped_vs_other_voice": spread([cosine(embed, a, o) for a in stopped for o in others]),
        }
    print(json.dumps(report, indent=1))
    (args.arms[-1].parent / "one_voice.json").write_text(json.dumps(report, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
