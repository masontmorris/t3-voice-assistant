"""Finds what bounds speech generation speed: splits wall time per decode step into backbone, depth decoder and codec,
and compares CUDA kernel time with wall time. Profiling slows the run, so its numbers are for attribution only."""

import sys
import time
from collections import defaultdict

import torch
from torch.profiler import ProfilerActivity, profile

import lfm

device = sys.argv[1] if len(sys.argv) > 1 else "cuda"
dtype = torch.bfloat16 if device == "cuda" else torch.float32
model, processor, codec, _ = lfm.load(device, dtype)
spent, calls = defaultdict(float), defaultdict(int)


def timed(name, fn):
    def wrapper(*args, **kwargs):
        lfm.sync(device)
        t = time.perf_counter()
        result = fn(*args, **kwargs)
        lfm.sync(device)
        spent[name] += time.perf_counter() - t
        calls[name] += 1
        return result
    return wrapper


conversation = [
    {"role": "system", "content": "Perform TTS in American English using a female voice."},
    {"role": "user", "content": "I found last week's sales report. Revenue was up twelve percent, and I am sending the summary to Dana now."},
    {"role": "assistant", "content": "<|audio_start|>"},
]
inputs = processor.apply_chat_template(conversation, continue_final_message=True, tokenize=True, return_dict=True, return_tensors="pt").to(device)


def tts():
    with torch.no_grad():
        out = model.generate(**inputs, max_new_tokens=200, **lfm.SPEECH_SAMPLING, return_dict_in_generate=True)
        codec.decode(out.audio_codes[:1, :, :out.audio_lengths[0]])
    return int(out.audio_lengths[0])


tts()  # warm-up
model.model.language_model.forward = timed("backbone", model.model.language_model.forward)
model.sample_audio_frame = timed("depth_decoder", model.sample_audio_frame)
codec.decode = timed("codec_decode", codec.decode)
lfm.sync(device)
t = time.perf_counter()
frames = tts()
wall = time.perf_counter() - t
print(f"frames {frames} ({frames * 0.08:.2f} s of speech), wall {wall:.2f} s, RTF {wall / (frames * 0.08):.2f}")
for name in spent:
    print(f"  {name:14s} {spent[name]:6.2f} s over {calls[name]:4d} calls, {1000 * spent[name] / calls[name]:6.1f} ms per call")

activities = [ProfilerActivity.CPU] + ([ProfilerActivity.CUDA] if device == "cuda" else [])
with profile(activities=activities) as prof:
    t = time.perf_counter()
    frames = tts()
    wall = time.perf_counter() - t
events = prof.key_averages()
if device == "cuda":
    gpu_us = sum(e.self_device_time_total for e in events)
    launches = sum(e.count for e in events if e.key in ("cudaLaunchKernel", "cuLaunchKernel", "cudaLaunchKernelExC"))
    print(f"profiled wall {wall:.2f} s, GPU kernel time {gpu_us / 1e6:.2f} s ({100 * gpu_us / 1e6 / wall:.0f}% busy), kernel launches {launches} ({launches / frames:.0f} per frame)")
print(events.table(sort_by="self_cpu_time_total", row_limit=15))
