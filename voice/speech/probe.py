"""Capability probe: exercises each real-time capability once and saves what the model did as evidence.

Usage: python probe.py cuda|cpu
"""

import json
import re
import sys
import time

import soundfile as sf
import torch

import lfm

device = sys.argv[1] if len(sys.argv) > 1 else "cuda"
dtype = torch.bfloat16 if device == "cuda" else torch.float32
model, processor, codec, load_s = lfm.load(device, dtype)
tok = processor.tokenizer
results = {"env": lfm.environment(device, dtype), "load_s": load_s}
out_dir = lfm.EVIDENCE_DIR / f"probe_{device}"
out_dir.mkdir(parents=True, exist_ok=True)

SYSTEM_CHAT = "<|startoftext|><|im_start|>system\nRespond with interleaved text and audio.<|im_end|>\n"
TOOLS = [
    {"type": "function", "function": {"name": "find_file", "description": "Search the user's files and return the best match.",
     "parameters": {"type": "object", "properties": {"query": {"type": "string", "description": "What to search for."}}, "required": ["query"]}}},
    {"type": "function", "function": {"name": "send_email", "description": "Send an email.",
     "parameters": {"type": "object", "properties": {"to": {"type": "string"}, "subject": {"type": "string"}, "body": {"type": "string"}}, "required": ["to", "body"]}}},
]
CALL_RE = re.compile(r"<tool_call>\s*<function=([^>\n]+)>(.*?)</function>\s*</tool_call>", re.DOTALL)
PARAM_RE = re.compile(r"<parameter=([^>\n]+)>\n?(.*?)\n?</parameter>", re.DOTALL)


def parse_tool_calls(text):
    calls = []
    for name, body in CALL_RE.findall(text):
        args = {}
        for key, value in PARAM_RE.findall(body):
            try:
                args[key] = json.loads(value)
            except json.JSONDecodeError:
                args[key] = value
        calls.append({"type": "function", "function": {"name": name, "arguments": args}})
    return calls


def raw_text(tokens):
    return tok.decode(tokens, skip_special_tokens=False)


def section(name):
    print(f"\n=== {name} ===", flush=True)


def stream_turn(session, clip, realtime=True, user_prefix="<|im_start|>user\n"):
    """Streams a clip into the session's user turn at real-time pace. Returns timing relative to audio start."""
    session.append_text(user_prefix)
    waveform = lfm.load_wav(clip)
    t0 = time.perf_counter()
    clock = lambda: time.perf_counter() - t0  # noqa: E731
    prefill = []
    for _, piece, arrival in lfm.mic_chunks(waveform, realtime, clock):
        added = session.stream_audio(piece)
        lfm.sync(device)
        prefill.append({"audio_s": arrival, "done_s": clock(), "tokens": added})
    added = session.stream_audio([], is_final=True)
    lfm.sync(device)
    return clock, {"audio_s": len(waveform) / lfm.SR_IN, "final_prefill_done_s": clock(), "chunks": prefill, "final_tokens": added}


def speak(session, clock, suffix, max_new_tokens=600, stop_after=None, **kw):
    text_rec = lfm.Recorder(clock, "text")
    audio_rec = lfm.Recorder(clock, "audio", session=session, stop_after=stop_after)
    start = clock()
    interrupted = None
    try:
        session.generate(suffix, streamer=text_rec, audio_streamer=audio_rec, max_new_tokens=max_new_tokens, **lfm.SPEECH_SAMPLING, **kw)
    except lfm.Interrupted as stop:
        interrupted = stop.at
    lfm.sync(device)
    end = clock()
    wave = audio_rec.waveform()
    return {
        "start_s": start,
        "end_s": end,
        "first_text_s": text_rec.events[0] if text_rec.events else None,
        "first_frame_s": audio_rec.events[0] if audio_rec.events else None,
        "first_pcm_s": audio_rec.first_pcm_s,
        "frames": len(audio_rec.frames),
        "speech_s": 0 if wave is None else len(wave) / lfm.SR_OUT,
        "text": lfm.text_of(tok, text_rec.tokens),
        "raw": raw_text(text_rec.tokens),
        "interrupted_at_s": interrupted,
        "returned_after_interrupt_ms": None if interrupted is None else (end - interrupted) * 1000,
    }, wave


# 1. Live ASR: partial transcripts while the clip is still streaming in.
section("live ASR, 100 ms mic chunks at real-time pace")
live = {}
for name in ("two_request", "correction"):
    waveform = lfm.load_wav(lfm.AUDIO_DIR / f"{name}.wav")
    prompt = tok("<|startoftext|><|im_start|>system\nPerform live ASR.<|im_end|>\n<|im_start|>assistant\n", add_special_tokens=False, return_tensors="pt").input_ids.to(device)
    suppress = tok.convert_tokens_to_ids(["<|mixed_end|>", "<|endoftext|>"])
    stop = tok.convert_tokens_to_ids(["<|endoftext|>", "<|im_start|>"])
    chunk_frames = 8 * model.config.interleaved_n_audio
    state, fcache = None, None
    mel = torch.empty((1, processor.feature_extractor.feature_size, 0), device=device)
    partials, text = [], ""
    t0 = time.perf_counter()
    clock = lambda: time.perf_counter() - t0  # noqa: E731

    def run(frames, arrival, is_final=False):
        global state, text
        started = clock()
        o = model.transcribe_live(frames, state, input_ids=prompt if state is None else None, is_final=is_final,
                                  max_new_tokens=96 if is_final else 48, suppress_token_ids=suppress, stop_token_ids=stop)
        lfm.sync(device)
        state = o.state
        piece = tok.decode(o.sequences[0], skip_special_tokens=True)
        text += piece
        partials.append({"audio_s": arrival, "start_s": started, "done_s": clock(), "piece": piece, "text": text, "final": is_final})
        print(f"  [{clock():6.2f}s | audio {arrival:5.2f}s] {text!r}", flush=True)

    for _, piece, arrival in lfm.mic_chunks(waveform, True, clock):
        features, fcache = processor.feature_extractor.stream(piece, fcache)
        mel = torch.cat((mel, features.input_features.to(device)), dim=-1)
        while mel.shape[-1] >= chunk_frames:
            run(mel[..., :chunk_frames], arrival)
            mel = mel[..., chunk_frames:]
    features, fcache = processor.feature_extractor.stream([], fcache, is_final=True)
    mel = torch.cat((mel, features.input_features.to(device)), dim=-1)
    while mel.shape[-1] > chunk_frames:
        run(mel[..., :chunk_frames], len(waveform) / lfm.SR_IN)
        mel = mel[..., chunk_frames:]
    run(mel, len(waveform) / lfm.SR_IN, is_final=True)
    live[name] = {"reference": (lfm.AUDIO_DIR / f"{name}.txt").read_text(), "audio_s": len(waveform) / lfm.SR_IN, "partials": partials}
results["live_asr"] = live

# 2. Streaming input plus streaming output in one voice-chat turn.
section("voice chat: audio prefilled while streaming in, reply streamed out")
session = lfm.Session(model, processor, codec, device)
session.append_text(SYSTEM_CHAT)
clock, prefill = stream_turn(session, lfm.AUDIO_DIR / "single.wav")
reply, wave = speak(session, clock, "<|im_end|>\n<|im_start|>assistant\n<|mixed_start|>")
sf.write(out_dir / "chat_reply.wav", wave, lfm.SR_OUT)
results["voice_chat"] = {"prefill": {k: v for k, v in prefill.items() if k != "chunks"}, "prefill_chunks": prefill["chunks"], "reply": reply}
print(json.dumps({k: v for k, v in reply.items() if k != "raw"}, indent=1))

# 3. Tool calling from a spoken two-request utterance (whole-clip path, as on the card).
section("tool calling from speech")
conversation = [
    {"role": "system", "content": "Respond with function calls, and interleaved text and audio.\n\nList of tools:"},
    {"role": "user", "content": [{"type": "audio", "audio": str(lfm.AUDIO_DIR / "two_request.wav")}]},
]
inputs = processor.apply_chat_template(conversation, tools=TOOLS, add_generation_prompt=True, tokenize=True, return_dict=True, return_tensors="pt").to(device)
with torch.no_grad():
    out = model.generate(**inputs, max_new_tokens=400, **lfm.SPEECH_SAMPLING, return_dict_in_generate=True)
gen = out.sequences[0, inputs["input_ids"].shape[1]:].tolist()
tool_text = tok.decode(gen, skip_special_tokens=True)
calls = parse_tool_calls(tool_text)
results["tool_calls"] = {"raw": raw_text(gen), "text": tool_text, "parsed": calls}
print(tool_text)
print("parsed:", json.dumps(calls))

# 4. Mid-session injection: the same turn streamed into a live session with tools, then structured messages appended
#    to the cache (no re-prefill), each followed by a spoken reply.
section("mid-session injection of task state")
tool_system = processor.apply_chat_template([{"role": "system", "content": "Respond with function calls, and interleaved text and audio.\n\nList of tools:"}], tools=TOOLS, tokenize=False)
session = lfm.Session(model, processor, codec, device)
session.append_text(tool_system)
clock, _ = stream_turn(session, lfm.AUDIO_DIR / "two_request.wav")
turn1, _ = speak(session, clock, "<|im_end|>\n<|im_start|>assistant\n", max_new_tokens=400)
print("turn 1:", turn1["raw"][:600])
snapshot = {"type": "utterance_snapshot", "final_transcript": "Find last week's sales report and then email a summary to Dana.",
            "tasks": [{"task_id": "task_1", "description": "Find last week's sales report", "status": "running"},
                      {"task_id": "task_2", "description": "Email a summary to Dana", "status": "pending", "depends_on": ["task_1"]}]}
update = {"type": "task_update", "task_id": "task_1", "status": "completed",
          "result_summary": "Found Northwind_Sales_Week45.xlsx. Revenue was 48,200 dollars, up 12 percent."}
injections = []
previous = turn1
for label, message in (
    ("tool_snapshot", f"<|im_start|>tool\n<tool_response>\n{json.dumps(snapshot)}\n</tool_response><|im_end|>\n<|im_start|>assistant\n<|mixed_start|>"),
    ("tool_update", f"<|im_start|>tool\n<tool_response>\n{json.dumps(update)}\n</tool_response><|im_end|>\n<|im_start|>assistant\n<|mixed_start|>"),
    ("system_update", "<|im_start|>system\nTask update: task_2 failed. The email server rejected the message for Dana.<|im_end|>\n<|im_start|>assistant\n<|mixed_start|>"),
):
    cache_before = session.cache.get_seq_length()
    t_inject = time.perf_counter()
    clock_i = lambda: time.perf_counter() - t_inject  # noqa: E731
    closer = "\n" if previous["raw"].rstrip().endswith("<|im_end|>") else "<|im_end|>\n"
    reply_i, wave_i = speak(session, clock_i, closer + message)
    previous = reply_i
    if wave_i is not None:
        sf.write(out_dir / f"inject_{label}.wav", wave_i, lfm.SR_OUT)
    injections.append({"label": label, "cache_tokens_before": cache_before, "cache_tokens_after": session.cache.get_seq_length(), "reply": reply_i})
    print(f"{label}: {reply_i['text']!r}  (first audio {reply_i['first_pcm_s']}, total {reply_i['end_s']:.2f}s)")
results["injection"] = {"turn1": turn1, "injections": injections}

# 5. Interruption: stop a reply mid-speech, then take new audio into the same cache with no reload.
section("interruption and resume")
session = lfm.Session(model, processor, codec, device)
session.append_text(SYSTEM_CHAT)
clock, _ = stream_turn(session, lfm.AUDIO_DIR / "long.wav", realtime=False)
cut, wave_cut = speak(session, clock, "<|im_end|>\n<|im_start|>assistant\n<|mixed_start|>", max_new_tokens=900, stop_after=25)
if wave_cut is not None:
    sf.write(out_dir / "interrupted_reply.wav", wave_cut, lfm.SR_OUT)
print(f"interrupted after {cut['frames']} frames, generate returned {cut['returned_after_interrupt_ms']:.1f} ms after the stop; text so far {cut['text']!r}")
cache_at_cut = session.cache.get_seq_length()
clock2, _ = stream_turn(session, lfm.AUDIO_DIR / "single.wav", realtime=False, user_prefix="<|im_end|>\n<|im_start|>user\n")
resumed, wave_res = speak(session, clock2, "<|im_end|>\n<|im_start|>assistant\n<|mixed_start|>")
if wave_res is not None:
    sf.write(out_dir / "resumed_reply.wav", wave_res, lfm.SR_OUT)
print(f"resumed reply: {resumed['text']!r}")
results["interruption"] = {"cut": cut, "cache_tokens_at_cut": cache_at_cut, "resumed": resumed}

if device == "cuda":
    results["vram"] = {"max_allocated_gb": torch.cuda.max_memory_allocated() / 2**30, "max_reserved_gb": torch.cuda.max_memory_reserved() / 2**30}
lfm.write_json(f"probe_{device}.json", results)
