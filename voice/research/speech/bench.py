"""Latency benchmark. Every input clip streams in as 100 ms mic chunks paced at real time, and every time is measured
from the start of the input audio unless the name says otherwise. One warm-up pass runs before the timed runs.

Usage: python bench.py cuda|cpu [runs]
       python bench.py cuda|cpu load   (load time only, one per process)
"""

import statistics
import sys
import time

import torch

import lfm

device = sys.argv[1]
mode = sys.argv[2] if len(sys.argv) > 2 else "5"
dtype = torch.bfloat16 if device == "cuda" else torch.float32
model, processor, codec, load_s = lfm.load(device, dtype)
if mode == "load":
    vram = torch.cuda.memory_allocated() / 2**30 if device == "cuda" else None
    lfm.write_json(f"load_{device}_{int(time.time())}.json", {"load_s": load_s, "vram_after_load_gb": vram})
    sys.exit(0)
runs = int(mode)
tok = processor.tokenizer
SYSTEM_CHAT = "<|startoftext|><|im_start|>system\nRespond with interleaved text and audio.<|im_end|>\n"
REPLY = "<|im_end|>\n<|im_start|>assistant\n<|mixed_start|>"


def clock_from_now():
    t0 = time.perf_counter()
    return lambda: time.perf_counter() - t0


def live_asr(clip):
    waveform = lfm.load_wav(clip)
    audio_s = len(waveform) / lfm.SR_IN
    asr = lfm.LiveASR(model, processor, device)
    clock = clock_from_now()
    updates, compute = [], 0.0
    for _, piece, arrival in lfm.mic_chunks(waveform, True, clock):
        started = clock()
        pieces = asr.feed(piece)
        lfm.sync(device)
        done = clock()
        compute += done - started
        for text in pieces:
            updates.append({"audio_s": arrival, "done_s": done, "text": text})
    started = clock()
    asr.feed([], is_final=True)
    lfm.sync(device)
    done = clock()
    compute += done - started
    nonempty = [u for u in updates if u["text"].strip()]
    gaps = [b["done_s"] - a["done_s"] for a, b in zip(nonempty, nonempty[1:])]
    lags = [u["done_s"] - u["audio_s"] for u in updates]
    return {
        "audio_s": audio_s,
        "first_partial_s": nonempty[0]["done_s"] if nonempty else None,
        "update_interval_median_s": statistics.median(gaps) if gaps else None,
        "block_lag_median_s": statistics.median(lags) if lags else None,
        "block_lag_max_s": max(lags) if lags else None,
        "final_after_audio_end_s": done - audio_s,
        "asr_compute_rtf": compute / audio_s,
        "text": asr.text,
    }


def whole_clip_asr(clip):
    conversation = [{"role": "system", "content": "Perform ASR in English."}, {"role": "user", "content": [{"type": "audio", "audio": str(clip)}]}]
    inputs = processor.apply_chat_template(conversation, add_generation_prompt=True, tokenize=True, return_dict=True, return_tensors="pt").to(device)
    audio_s = len(lfm.load_wav(clip)) / lfm.SR_IN
    lfm.sync(device)
    t = time.perf_counter()
    with torch.no_grad():
        out = model.generate(**inputs, max_new_tokens=256, suppress_mixed=True, suppress_audio_out=True, return_dict_in_generate=True)
    lfm.sync(device)
    elapsed = time.perf_counter() - t
    text = processor.decode(out.sequences[0, inputs["input_ids"].shape[1]:], skip_special_tokens=True)
    return {"audio_s": audio_s, "wall_s": elapsed, "asr_rtf": elapsed / audio_s, "text": text}


def voice_turn(clip, max_new_tokens):
    session = lfm.Session(model, processor, codec, device)
    session.append_text(SYSTEM_CHAT + "<|im_start|>user\n")
    waveform = lfm.load_wav(clip)
    audio_s = len(waveform) / lfm.SR_IN
    clock = clock_from_now()
    prefill_lags = []
    for _, piece, arrival in lfm.mic_chunks(waveform, True, clock):
        session.stream_audio(piece)
        lfm.sync(device)
        prefill_lags.append(clock() - arrival)
    session.stream_audio([], is_final=True)
    lfm.sync(device)
    prefill_done = clock()
    text_rec = lfm.Recorder(clock, "text")
    audio_rec = lfm.Recorder(clock, "audio", session=session)
    session.generate(REPLY, streamer=text_rec, audio_streamer=audio_rec, max_new_tokens=max_new_tokens, **lfm.SPEECH_SAMPLING)
    lfm.sync(device)
    end = clock()
    speech_s = len(audio_rec.frames) * 0.08
    first_frame = audio_rec.events[0] if audio_rec.events else None
    return {
        "audio_s": audio_s,
        "prefill_lag_median_s": statistics.median(prefill_lags),
        "prefill_lag_max_s": max(prefill_lags),
        "prefill_done_after_audio_end_s": prefill_done - audio_s,
        "first_text_after_audio_end_s": text_rec.events[0] - audio_s if text_rec.events else None,
        "first_frame_after_audio_end_s": first_frame - audio_s if first_frame else None,
        "first_audio_after_audio_end_s": audio_rec.first_pcm_s - audio_s if audio_rec.first_pcm_s else None,
        "first_audio_from_audio_start_s": audio_rec.first_pcm_s,
        "speech_s": speech_s,
        "frames": len(audio_rec.frames),
        "text_tokens": sum(1 for t in text_rec.tokens),
        "reply_gen_rtf": (end - first_frame) / speech_s if first_frame and speech_s else None,
        "reply_wall_s": end - prefill_done,
        "text": lfm.text_of(tok, text_rec.tokens),
    }


def tts(text, max_new_tokens):
    conversation = [
        {"role": "system", "content": "Perform TTS in American English using a female voice."},
        {"role": "user", "content": text},
        {"role": "assistant", "content": "<|audio_start|>"},
    ]
    inputs = processor.apply_chat_template(conversation, continue_final_message=True, tokenize=True, return_dict=True, return_tensors="pt").to(device)
    session = lfm.Session(model, processor, codec, device)
    clock = clock_from_now()
    audio_rec = lfm.Recorder(clock, "audio", session=session)
    with torch.no_grad():
        model.generate(**inputs, max_new_tokens=max_new_tokens, audio_streamer=audio_rec, **lfm.SPEECH_SAMPLING, return_dict_in_generate=True)
    lfm.sync(device)
    wall = clock()
    speech_s = len(audio_rec.frames) * 0.08
    return {"speech_s": speech_s, "wall_s": wall, "tts_rtf": wall / speech_s, "first_audio_s": audio_rec.first_pcm_s, "frames": len(audio_rec.frames)}


def summarize(rows):
    keys = [k for k, v in rows[0].items() if isinstance(v, (int, float)) and not isinstance(v, bool)]
    out = {}
    for key in keys:
        values = [r[key] for r in rows if r.get(key) is not None]
        if values:
            out[key] = {"median": statistics.median(values), "min": min(values), "max": max(values), "n": len(values)}
    return out


TTS_TEXT = "I found last week's sales report. Revenue was up twelve percent, and I am sending the summary to Dana now."
cases = {
    "live_asr_long": lambda: live_asr(lfm.AUDIO_DIR / "long.wav"),
    "whole_clip_asr_long": lambda: whole_clip_asr(lfm.AUDIO_DIR / "long.wav"),
    "voice_turn_two_request": lambda: voice_turn(lfm.AUDIO_DIR / "two_request.wav", 300),
    "tts": lambda: tts(TTS_TEXT, 200),
}
results = {"env": lfm.environment(device, dtype), "load_s_this_process": load_s, "runs": runs, "cases": {}}
if device == "cuda":
    torch.cuda.reset_peak_memory_stats()
for name, case in cases.items():
    print(f"warm-up {name}", flush=True)
    case()
# cases alternate inside each round, so drift and warm caches favor no case
for round_index in range(runs):
    for name, case in cases.items():
        row = case()
        results["cases"].setdefault(name, {"rows": []})["rows"].append(row)
        printable = {k: (round(v, 3) if isinstance(v, float) else v) for k, v in row.items() if k != "text"}
        print(f"round {round_index} {name}: {printable}", flush=True)
for name, entry in results["cases"].items():
    entry["summary"] = summarize(entry["rows"])
if device == "cuda":
    results["vram_peak_allocated_gb"] = torch.cuda.max_memory_allocated() / 2**30
    results["vram_peak_reserved_gb"] = torch.cuda.max_memory_reserved() / 2**30
lfm.write_json(f"bench_{device}.json", results)
