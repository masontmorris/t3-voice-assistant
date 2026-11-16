"""Benchmarks the recommended pattern: live ASR and voice-chat prefill fed from the same mic chunks, then the reply.
One warm-up turn, then 5 timed turns, each paced at real time.

Usage: python bench_combined.py cuda|cpu [clip] [runs]
"""

import statistics
import sys

import torch

import lfm
from demo import run_turn

device = sys.argv[1]
clip = lfm.AUDIO_DIR / (sys.argv[2] if len(sys.argv) > 2 else "two_request.wav")
runs = int(sys.argv[3]) if len(sys.argv) > 3 else 5
dtype = torch.bfloat16 if device == "cuda" else torch.float32
model, processor, codec, _ = lfm.load(device, dtype)
run_turn(model, processor, codec, device, clip, echo=False, max_new_tokens=300)
rows = []
for index in range(runs):
    row, _ = run_turn(model, processor, codec, device, clip, echo=False, max_new_tokens=300)
    rows.append(row)
    print(index, {k: round(v, 3) if isinstance(v, float) else v for k, v in row.items() if k != "reply_text"}, flush=True)
summary = {}
for key, value in rows[0].items():
    if isinstance(value, float):
        values = [r[key] for r in rows if r[key] is not None]
        summary[key] = {"median": statistics.median(values), "min": min(values), "max": max(values), "n": len(values)}
lfm.write_json(f"bench_combined_{device}.json", {"env": lfm.environment(device, dtype), "clip": clip.name, "rows": rows, "summary": summary})
