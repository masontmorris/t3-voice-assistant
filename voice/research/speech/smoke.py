"""CPU smoke test: load the pinned model offline and transcribe one synthetic clip with the whole-clip ASR path."""

import time

import torch

import lfm

device, dtype = "cpu", torch.float32
model, processor, codec, load_s = lfm.load(device, dtype)
print(f"loaded in {load_s:.1f} s, params {sum(p.numel() for p in model.parameters()) / 1e9:.3f} B")

clip = lfm.AUDIO_DIR / "single.wav"
conversation = [
    {"role": "system", "content": "Perform ASR in English."},
    {"role": "user", "content": [{"type": "audio", "audio": str(clip)}]},
]
inputs = processor.apply_chat_template(
    conversation, add_generation_prompt=True, tokenize=True, return_dict=True, return_tensors="pt"
).to(device)
t0 = time.perf_counter()
with torch.no_grad():
    out = model.generate(**inputs, max_new_tokens=128, suppress_mixed=True, suppress_audio_out=True, return_dict_in_generate=True)
elapsed = time.perf_counter() - t0
text = processor.decode(out.sequences[0, inputs["input_ids"].shape[1]:], skip_special_tokens=True)
print(f"reference: {(lfm.AUDIO_DIR / 'single.txt').read_text()}")
print(f"transcript: {text!r}")
print(f"asr {elapsed:.2f} s, one run, not a timing claim")
lfm.write_json("smoke_cpu.json", {"env": lfm.environment(device, dtype), "load_s": load_s, "asr_s": elapsed, "text": text})
