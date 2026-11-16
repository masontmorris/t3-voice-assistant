"""Prints the findings doc's latency table from the benchmark JSON under $VOICE_HOME/evidence/stage1."""

import glob
import json
import statistics

from lfm import EVIDENCE_DIR

METRICS = [
    ("Load time, process start excluded (s)", "load", None),
    ("Live ASR: first partial transcript, from audio start (s)", "live_asr_long", "first_partial_s"),
    ("Live ASR: interval between transcript updates (s)", "live_asr_long", "update_interval_median_s"),
    ("Live ASR: lag of each update behind its audio, median per run (s)", "live_asr_long", "block_lag_median_s"),
    ("Live ASR: final transcript after audio end (s)", "live_asr_long", "final_after_audio_end_s"),
    ("Live ASR: compute real-time factor", "live_asr_long", "asr_compute_rtf"),
    ("Whole-clip ASR real-time factor, 19.1 s clip", "whole_clip_asr_long", "asr_rtf"),
    ("Voice turn: streaming prefill lag behind the mic, median per run (s)", "voice_turn_two_request", "prefill_lag_median_s"),
    ("Voice turn: first reply text token after audio end (s)", "voice_turn_two_request", "first_text_after_audio_end_s"),
    ("Voice turn: first reply audio (decoded PCM) after audio end (s)", "voice_turn_two_request", "first_audio_after_audio_end_s"),
    ("Voice turn: first reply audio from audio start, 4.4 s clip (s)", "voice_turn_two_request", "first_audio_from_audio_start_s"),
    ("Voice turn: reply generation real-time factor (interleaved text and speech)", "voice_turn_two_request", "reply_gen_rtf"),
    ("TTS: first audio (s)", "tts", "first_audio_s"),
    ("TTS real-time factor (generation and streaming decode)", "tts", "tts_rtf"),
]


def cell(values):
    if not values:
        return "not run"
    return f"{statistics.median(values):.2f} ({min(values):.2f} to {max(values):.2f}), n={len(values)}"


def values(device, case, key):
    if case == "load":
        return [json.load(open(p))["load_s"] for p in sorted(glob.glob(str(EVIDENCE_DIR / f"load_{device}_*.json")))]
    try:
        bench = json.load(open(EVIDENCE_DIR / f"bench_{device}.json"))
    except FileNotFoundError:
        return []
    return [row[key] for row in bench["cases"][case]["rows"] if row.get(key) is not None]


print("| Metric | GPU, RTX 3060, bf16 | CPU, Ryzen 5 5600X, fp32 |")
print("| --- | --- | --- |")
for label, case, key in METRICS:
    print(f"| {label} | {cell(values('cuda', case, key))} | {cell(values('cpu', case, key))} |")
for device in ("cuda", "cpu"):
    try:
        bench = json.load(open(EVIDENCE_DIR / f"bench_{device}.json"))
    except FileNotFoundError:
        continue
    vram = bench.get("vram_peak_allocated_gb")
    print(f"\n{device}: env {bench['env']}" + (f", peak VRAM allocated {vram:.2f} GB, reserved {bench['vram_peak_reserved_gb']:.2f} GB" if vram else ""))

LLAMA = [
    ("Voice session: first reply text after audio end (s)", "voice_turn", "first_text_after_audio_end_s"),
    ("Voice session: first reply audio after audio end (s)", "voice_turn", "first_audio_after_audio_end_s"),
    ("Voice session: reply generation real-time factor", "voice_turn", "reply_gen_rtf"),
    ("Transcription session: first partial, from audio start (s)", "live_transcription", "first_partial_s"),
    ("Transcription session: final transcript after audio end (s)", "live_transcription", "final_after_audio_end_s"),
]
print("\n| Metric | GPU, sessions in turn | GPU, both sessions at once | CPU only, both sessions at once |")
print("| --- | --- | --- | --- |")
runs = {}
for tag in ("gpu", "gpu_concurrent", "cpu_concurrent"):
    try:
        runs[tag] = json.load(open(EVIDENCE_DIR / f"llamacpp_rt_{tag}.json"))
    except FileNotFoundError:
        runs[tag] = None
for label, case, key in LLAMA:
    cells = [cell([r[key] for r in run[case]["rows"] if r.get(key) is not None]) if run else "not run" for run in runs.values()]
    print(f"| {label} | " + " | ".join(cells) + " |")
