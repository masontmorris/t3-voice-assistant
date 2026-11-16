# Stage 1 findings: the LFM3 Audio speech path on this Desktop

The plan uses LFM3-Audio-2B as the local voice layer of the assistant. Stage 1 asks what the model and its runtime actually do in real time, and how fast it runs on this Desktop. Each claim below carries a label. "Measured" means a run on this machine produced it, "inferred" means I reasoned it from code or docs, and "guess" means neither.

## Decision

Run the speech path on llama.cpp's `llama serve` `/v1/realtime` WebSocket API with the LFM3-Audio-2B GGUF, on the RTX 3060. Open two sessions per conversation. A `realtime` session holds the conversation, and a `?intent=transcription` session gives the orchestrator its partial transcript. Send task snapshots and updates as `function_call_output` items at turn boundaries. Every capability the plan needs works on this runtime, and it generates speech about 7.5 times faster than real time on this GPU (measured, see "llama.cpp realtime results"). No separate ASR is needed.

The measured numbers for this setup come from 5 runs with both sessions open at once. Speech generates at RTF 0.13. The final transcript lands 0.14 s and the first reply audio 0.23 s after the user stops talking. Those runs used the Q4_K_M weights, not bf16, so part of the gap to Transformers is precision rather than runtime (inferred). Reply quality at Q4_K_M was not scored beyond exact transcripts and on-topic replies.

Transformers is the reference implementation, and it shows every capability too, but too slowly. On this GPU in eager bf16 the reply generates at RTF 1.35, slower than playback (median of 5, measured). The limiter is CPU-side kernel launch overhead, about 3,000 launches per 80 ms speech frame, with the GPU busy about 40% of the time (measured with the PyTorch profiler).

Full precision on this Desktop's CPU is not viable. Transformers fp32 generates at RTF 6.42, with first audio 11.8 s after the user stops (medians of 5, measured). Each decode step streams the fp32 weights from RAM, which puts a floor above real time (inferred). llama.cpp Q4_K_M on the CPU alone just keeps up, at RTF 0.87 with first audio 0.76 s after the user stops, both sessions open (medians of 5, measured). A CPU VPS therefore needs quantized weights and at least this Desktop's 6 cores and memory bandwidth, with no headroom for a second conversation (inferred).

## Capability table

All checks ran in `voice/speech/probe.py` on the GPU (bf16), one run each, evidence in `$VOICE_HOME/evidence/stage1/probe_cuda.json` and `probe_cuda.log`. Text decoding is greedy and speech is sampled at the card's settings. Sampled speech frames feed back into the context, so the reply text varies run to run in the speech modes (measured, the benchmark replies differ). Each check below ran on Transformers. The llama.cpp column of "llama.cpp realtime results" repeats the checks that matter on that runtime.

| Capability | Status | Label | Evidence |
| --- | --- | --- | --- |
| Audio in, streaming | Yes. `model.prefill_audio` encodes each 100 ms mic chunk and appends the finished audio tokens to the chat KV cache while the user speaks. | Measured | Prefill stays 0.08 s behind the mic (median of 5, GPU). The first reply token comes 0.21 s after the audio ends, so the turn is not re-encoded at the end. The encoder is causal with 7 mel frames (70 ms) of lookahead (`modeling_lfm3_audio.py:161-216`). |
| Partial transcript before the turn ends | Yes, but only from a second context. `model.transcribe_live` with the prompt `Perform live ASR.` writes text for every 0.96 s block. The voice-chat context writes no transcript. English only, per the card. | Measured | `two_request.wav` came out exactly as the SAPI text. Updates arrive every 0.99 s (median of 5). |
| Partial transcripts revise earlier words | No. Each block only appends text. The correction clip came out verbatim: "Set a timer for 10 minutes. No wait. Make that 15 minutes." The orchestrator has to resolve corrections itself. | Measured (output), inferred (code has no rewrite path) | `probe_cuda.json`, `live_asr.correction` |
| Text before audio in the reply | Yes. Interleaved mode emits 6 text tokens, then 12 speech frames, per block (`config.json`, `interleaved_n_text`, `interleaved_n_audio`). | Measured | First reply text 0.21 s and first reply audio 0.56 s after the input ends (medians of 5, GPU). |
| Audio out, streaming | Yes. The `audio_streamer` of `generate` receives each 80 ms frame as it is sampled, and `LiquidCodecModel.decode(..., streaming_cache=...)` decodes frames incrementally. | Measured | First decoded PCM 0.56 s after the input ends. Generation is slower than playback, RTF 1.35 (voice turn) and 1.21 (TTS), medians of 5. |
| Tool calling | Yes, in a Qwen-style XML format: `<tool_call>\n<function=find_file>\n<parameter=query>\nsales report last week\n</parameter>\n</function>\n</tool_call>`. Values other than strings are JSON. | Measured | From the spoken two-request clip, the model called `find_file` only. After the result came back it asked for Dana's address instead of calling `send_email`, so multi-request turns resolve one call at a time. |
| Inject a structured message mid-session | Yes. Appending text to the live KV cache needs no re-prefill. A `tool` turn with a `<tool_response>` JSON snapshot, a later `tool` update, and a `system` turn all changed the next spoken reply. | Measured | After the update "Revenue was 48,200 dollars, up 12 percent", the reply said "It shows revenue of 48,200 dollars, up 12 percent". After the `system` turn "task_2 failed", the reply said "there was an issue sending the email to Dana". Cache grew 231, 446, 682, then 863 tokens with no reload. |
| Injection while the user is speaking | No safe slot. The cache is append-only, so text appended mid-utterance lands inside the user's audio turn. Hold updates until the turn boundary or interrupt the reply first. | Inferred from `prefill_audio` and `generate` | `modeling_lfm3_audio.py:957-1026` |
| Interruption and resume without reload | Yes. Raising an exception from the audio streamer stops `generate` after the current step. The KV cache keeps the partial reply. New user audio then prefills into the same cache, and the next reply answered it. | Measured | Stopped after 25 frames ("Sure, I can help you map that out. First, open your calendar and filter for"). The next question about Tokyo got an answer that also referred to "your other tasks". `generate` rejects `stopping_criteria` (`modeling_lfm3_audio.py:1382-1392`), so the streamer exception is the only stop hook. |
| Barge-in detection | Not provided. Nothing in the model detects that the user started talking. Use a VAD, or watch the live-ASR context for new text. | Inferred | No VAD in the nine modules |

Two consequences for the design follow (inferred). First, the orchestrator gets its transcript from the live-ASR context, and corrections arrive as appended text, never as revisions. Second, the audio service owns turn boundaries: it decides when to append a snapshot, when to interrupt, and when to resume.

## Latency table

Every number comes from `voice/speech/bench.py`, wrapped in `quiet_run.py` (and in `gpu_lease.py` for the GPU). Each case ran once to warm up, then 5 timed rounds with the cases alternating inside each round. Cells show the median, the range, and the run count. The input clip streams in as 100 ms chunks paced at real time, and times run from the start of the input audio unless the row says otherwise. The live-ASR rows use `long.wav` (19.1 s). The voice-turn rows use `two_request.wav` (4.4 s) with replies capped at 300 generation slots. The TTS rows synthesize one fixed 20-word sentence.

| Metric | GPU, RTX 3060, bf16 | CPU, Ryzen 5 5600X, fp32 |
| --- | --- | --- |
| Load time, process start excluded (s) | 5.10 (4.77 to 6.00), n=5 | 6.64 (5.97 to 9.00), n=5 |
| Live ASR: first partial transcript, from audio start (s) | 1.20 (1.18 to 1.23), n=5 | 2.50 (2.40 to 2.61), n=5 |
| Live ASR: interval between transcript updates (s) | 0.99 (0.97 to 1.00), n=5 | 1.75 (1.66 to 1.87), n=5 |
| Live ASR: lag of each update behind its audio, median per run (s) | 0.24 (0.23 to 0.29), n=5 | 7.24 (6.67 to 8.81), n=5 |
| Live ASR: final transcript after audio end (s) | 0.27 (0.25 to 0.35), n=5 | 19.49 (15.86 to 20.99), n=5 |
| Live ASR: compute real-time factor | 0.28 (0.26 to 0.34), n=5 | 1.97 (1.78 to 2.05), n=5 |
| Whole-clip ASR real-time factor, 19.1 s clip | 0.19 (0.15 to 0.22), n=5 | 1.20 (1.14 to 1.41), n=5 |
| Voice turn: streaming prefill lag behind the mic, median per run (s) | 0.08 (0.07 to 0.09), n=5 | 4.70 (4.09 to 8.08), n=5 |
| Voice turn: first reply text token after audio end (s) | 0.21 (0.20 to 0.36), n=5 | 9.73 (9.17 to 14.36), n=5 |
| Voice turn: first reply audio (decoded PCM) after audio end (s) | 0.56 (0.50 to 0.81), n=5 | 11.77 (11.27 to 16.72), n=5 |
| Voice turn: first reply audio from audio start, 4.4 s clip (s) | 4.94 (4.89 to 5.19), n=5 | 16.15 (15.66 to 21.11), n=5 |
| Voice turn: reply generation real-time factor (interleaved text and speech) | 1.35 (1.29 to 1.54), n=5 | 6.42 (5.94 to 6.86), n=5 |
| TTS: first audio (s) | 0.11 (0.10 to 0.14), n=5 | 1.19 (1.11 to 1.33), n=5 |
| TTS real-time factor (generation and streaming decode) | 1.21 (1.14 to 1.36), n=5 | 5.16 (5.05 to 5.79), n=5 |

Peak VRAM during the GPU benchmark was 3.89 GB allocated and 3.97 GB reserved (measured with `torch.cuda.max_memory_allocated`). The probe, which holds three sessions in turn, peaked at 4.34 GB. About 8 GB of the 12 GB card stays free for other lanes.

### Both contexts on one GPU in Transformers

`voice/speech/bench_combined.py` feeds every 100 ms chunk to the live-ASR context and then to the voice-chat context, as `demo.py` does. It ran one warm-up turn and then 5 timed turns of `two_request.wav` on the GPU, inside `quiet_run.py` and the lease (measured).

| Metric | GPU, RTX 3060, bf16 |
| --- | --- |
| First partial transcript, from audio start (s) | 1.21 (1.20 to 1.34), n=5 |
| Lag of each chunk behind the mic, median per run (s) | 0.24 (0.22 to 0.79), n=5 |
| Final transcript after audio end (s) | 0.66 (0.60 to 1.20), n=5 |
| First reply text token after audio end (s) | 0.71 (0.65 to 1.25), n=5 |
| First reply audio (decoded PCM) after audio end (s) | 1.04 (0.96 to 1.57), n=5 |

Running both contexts roughly doubles the time to first audio, from 0.56 s to 1.04 s. Both contexts run one after the other on one CUDA stream, so the live-ASR block decode and its final flush sit in front of the reply (inferred from the code path in `demo.py`). Starting the reply before the final ASR flush, or running the ASR context on its own thread and stream, would remove most of that wait (guess, not tried). The CPU does not get this table, because the CPU cannot keep up with either context alone.

Conditions (measured). The GPU is an RTX 3060 12 GB (driver 610.88), with torch 2.15.0+cu132, bf16, and transformers 5.20.2 in eager mode. The CPU is an AMD Ryzen 5 5600X with 6 cores and 12 threads, running fp32 with 6 torch threads. These CPU numbers come from this Desktop's CPU, not from a VPS. Load times use a warm OS file cache, because the five loads ran back to back (inferred). "RTF" is wall time divided by audio duration, so below 1 keeps up with real time. The voice-turn RTF counts the interleaved text slots, because they share the decode loop with the speech frames.

### Why speech is slower than real time

`voice/speech/profile_step.py` timed one TTS generation after a warm-up (measured, one run, for attribution only). It produced 86 frames (6.88 s of speech) in 7.45 s. The backbone decode step took 43.5 ms per call and the depth decoder 40.5 ms per frame, so one 80 ms frame costs about 84 ms. The PyTorch profiler counted 3,025 kernel launches per frame. CUDA kernel time was 2.97 s across a 14.26 s profiled run, and `cudaLaunchKernel` was the largest CPU cost (38.8% of self CPU time). The GPU sits idle most of the time while Python launches small kernels one by one.

So the GPU number is untuned: it measures eager PyTorch, not the hardware. Three things would cut launch overhead.

- CUDA graphs or `torch.compile`. The code mentions compiled and CUDA-graph-captured forwards (`modeling_lfm3_audio.py:591`), but `generate` refuses `cache_implementation`, and Windows needs `triton-windows` for compile. Not tried (guess: worth a 2x or more).
- The fused conv kernels. `lfm3_conv.py` needs Triton and `transformers` wants `causal_conv1d`. Neither is installed here, so the convs run the reference PyTorch path (measured, warnings in every log).
- A C++ runtime such as llama.cpp, which drives the whole step without Python. The v0.8.0 release notes list "LFM3-Audio-2B and LFM3-Omni-2B: ASR, TTS and voice chat from a single model, with streaming audio input and output". See "llama.cpp realtime results" for the measured numbers.

### Why the CPU is slower still

On the CPU the same profile (measured, one run inside `quiet_run.py`) produced 84 frames in 32.61 s, RTF 4.85. The backbone step took 273.3 ms per call and the depth decoder 80.5 ms per frame. `aten::mm` was 84% of CPU time. A batch-1 decode step multiplies every weight matrix by one vector, so each step streams the fp32 weights from RAM. This Desktop has two DDR4-3600 DIMMs, about 57.6 GB/s at peak. Reading roughly 6 GB of fp32 backbone weights takes at least 104 ms per step at that peak (inferred, and the 6 GB is a guess at the backbone's share of the 1.98B parameters). That floor is already above the 80 ms a frame lasts. Full-precision CPU inference therefore cannot run real-time speech on this class of machine, and a CPU VPS at fp32 would face the same floor at its own memory bandwidth (inferred). Streaming prefill falls behind for the same reason, because one backbone forward per 100 ms chunk costs more than 100 ms. Batching several chunks per forward would recover prefill throughput but not the decode floor (inferred). The codec runs in bf16 on the CPU, as its config sets, and one non-streaming decode of 6.7 s of speech took 2.18 s. That side was not tuned.

## llama.cpp realtime results

`voice/speech/llamacpp_rt.py` starts `llama-server.exe` from the b12428 Windows CUDA 12.4 build with `LFM3-Audio-2B-Q4_K_M.gguf` and `mmproj-LFM3-Audio-2B-Q8_0.gguf` from `LiquidAI/LFM3-Audio-2B-GGUF` at `39670cb5c42e9c4bfe046519ba7ffbe47f9552ad`. The server flags are `--temp 0 -np 2`, plus `-ngl 99` for the GPU or `-ngl 0` with `CUDA_VISIBLE_DEVICES=` for the CPU. The script talks to `/v1/realtime` over a WebSocket and streams PCM16 in 100 ms chunks paced at real time. I checked both release zips against the SHA-256 digests GitHub publishes for them.

### Capabilities on llama.cpp

The probe ran four times: once on its own and once at the start of each benchmark job (two on the GPU, one on the CPU). All four produced the same tool calls and the same texts. The evidence is under `probe_tools` and `live_transcription_probe` in each `llamacpp_rt_*.json` (measured unless the row says otherwise).

| Capability | Result |
| --- | --- |
| Audio in, streaming | Yes. The first reply text arrives 0.16 s after the audio ends (median of 5, both sessions open), so the server prefilled while the clip streamed. |
| Partial transcript | Yes, from a second session opened with `?intent=transcription`. Word-level `conversation.item.input_audio_transcription.delta` events arrive in bursts about every 0.96 s. The correction clip produced "Set a timer for 10 minutes. No wait. Make that 15 minutes.", appended and never revised. |
| Audio out, streaming | Yes. `response.output_audio.delta` carries base64 PCM16 at 24 kHz while the reply generates. |
| Tool calling | Yes. The spoken two-request clip produced a `function_call` item `find_file` with arguments `{"query":"sales report last week"}`. |
| Inject a structured message | Yes, as a `function_call_output` item. After the task-update JSON went in, the next response called `send_email` with `{"to":"Dana","body":"The sales report for last week is ready. Revenue was $48,200, up 12%."}`. The server has no mid-session `system` item (inferred from the README), so updates that answer no tool call go in as user text. |
| Interruption | Yes. `response.cancel` after the first audio delta of a long story gave `response.done` with status `cancelled` 20 to 21 ms later on the GPU and 175 ms later on the CPU. The next question, "What was the revenue number again?", got "The revenue was 48,200 dollars, up 12 percent.", so the session kept its context. |

### Latency on llama.cpp

Each column ran 5 timed rounds after one warm-up, inside `quiet_run.py`, and inside the lease for the GPU columns. The voice session streams `two_request.wav` with no tools. "Sessions in turn" runs the transcription session on `long.wav` after the voice turn. "Both sessions at once" streams `two_request.wav` to both sessions at the same time, which is the recommended pattern. All runs produced exact transcripts and completed responses, and every saved reply WAV has a signal level close to the Transformers reply (RMS 0.038 to 0.044 against 0.043).

| Metric | GPU, sessions in turn | GPU, both sessions at once | CPU only, both sessions at once |
| --- | --- | --- | --- |
| Voice session: first reply text after audio end (s) | 0.19 (0.17 to 0.21), n=5 | 0.16 (0.14 to 0.17), n=5 | 0.51 (0.47 to 0.71), n=5 |
| Voice session: first reply audio after audio end (s) | 0.26 (0.24 to 0.40), n=5 | 0.23 (0.20 to 0.26), n=5 | 0.76 (0.74 to 1.01), n=5 |
| Voice session: reply generation real-time factor | 0.13 (0.13 to 0.13), n=5 | 0.13 (0.13 to 0.14), n=5 | 0.87 (0.85 to 0.93), n=5 |
| Transcription session: first partial, from audio start (s) | 0.96 (0.95 to 0.96), n=5 | 0.96 (0.95 to 0.96), n=5 | 1.18 (1.17 to 1.26), n=5 |
| Transcription session: final transcript after audio end (s) | 0.07 (0.03 to 0.09), n=5 | 0.14 (0.12 to 0.15), n=5 | 0.47 (0.45 to 0.66), n=5 |

What the numbers mean (inferred). On the GPU the model generates speech about 7.5 times faster than it plays, so a reply never stalls and a second conversation would fit. On the CPU alone, one conversation keeps up with about 13% to spare, which leaves no room for a second one. The CPU numbers still beat Transformers fp32 by more than 7x, because Q4_K_M weights are about 1.1 GB against about 8 GB at fp32, so each decode step reads far less memory. I did not profile llama.cpp, so I cannot name its limiter. Treat these as end-to-end numbers for this configuration, not as a runtime comparison at equal precision. A bf16 GGUF run would separate runtime from precision, but the 4 GB file does not fit above the disk stop line today.

## Runtime options

| Runtime | What it offers | Label |
| --- | --- | --- |
| llama.cpp `llama serve` `/v1/realtime`, Q4_K_M | Every capability in both tables. Speech at RTF 0.13 on the GPU and 0.87 on the CPU, both sessions open. | Measured |
| Transformers, eager | Every capability, plus `system` turns mid-session. Speech at RTF 1.35 on the GPU and 6.42 on the CPU at fp32. Use it as the reference implementation and for fine-tuning. | Measured |
| Transformers with CUDA graphs, `torch.compile`, or the Triton conv kernels | Could cut the launch overhead the profile found. | Guess, not tried |
| ONNX Runtime (`LiquidAI/LFM3-Audio-2B-ONNX`) | Separate graphs for the audio encoder, the decoder step, the depth decoder, and the codec. You write the decode loop yourself. The card notes that the CUDA provider has no fp32 GroupQueryAttention kernel. | Inferred from the card |
| Separate streaming ASR | Not needed. Live ASR in the same model gives partials every 0.96 s on both runtimes. | Measured |

## Demo

The demo streams a WAV in at real-time pace. It prints timestamped partial transcripts from the live-ASR context and prefills the voice-chat context from the same chunks. Then it streams the reply out and saves it as a 24 kHz WAV.

Run it from the repo root:

```
python voice/speech/demo.py "$VOICE_HOME/evidence/stage1/audio/two_request.wav" --out "$VOICE_HOME/evidence/stage1/demo_two_request_reply.wav"
```

Add `--device cpu` for the CPU and `--fast` to feed the clip without real-time pacing. Below is the observed output of one cold run on the GPU, inside `quiet_run.py` and the lease (measured). The per-token reply lines are cut after the first six, and the saved path shows `$VOICE_HOME` in place of the local root.

```
loaded LiquidAI/LFM3-Audio-2B@2d6c1f99 on cuda (torch.bfloat16) in 4.2 s
[  1.29s] partial (audio to  1.00s): Find
[  2.42s] partial (audio to  2.00s): Find last week's sales
[  3.34s] partial (audio to  2.90s): Find last week's sales report and then
[  4.43s] partial (audio to  3.90s): Find last week's sales report and then email a summary to
[  5.14s] final transcript (audio ended at 4.38s): Find last week's sales report and then email a summary to Dana.
[  5.20s] reply text: 'I'
[  5.24s] reply text: ' don'
[  5.29s] reply text: "'t"
[  5.33s] reply text: ' have'
[  5.38s] reply text: ' access'
[  5.43s] reply text: ' to'
...
[ 20.62s] done. first reply audio 1.25s after the input ended; 11.92s of speech; reply text: "I don't have access to your files or the ability to send emails, so I can't look up that report or send it to Dana for you. But I can definitely help you put together a summary if you share the details!"
saved $VOICE_HOME/evidence/stage1/demo_two_request_reply.wav
```

The demo passes no tools, so the model says it cannot act. With the tools system prompt the same clip produces a `find_file` call (see the capability table). An earlier demo run on `correction.wav` replied "Sure, I set it for fifteen minutes. What are you planning to do while it runs?" So the voice-chat context resolves the correction, even though the live transcript keeps both values (measured, one cold run, reply audio in `demo_correction_reply.wav`).

## Reproduce

Set up `$VOICE_HOME` as the [speech README](../../voice/speech/README.md) describes. Every script sets `HF_HUB_OFFLINE=1` and loads revision `2d6c1f99c129b3ef07a8d168bc0ff08e2bdb93f1`. Run each step from the repo root with the venv's Python. On this Desktop, every timed run ran inside `quiet_run.py`, and every GPU run ran inside the GPU lease (`gpu_lease.py`). Those two local wrappers schedule timing runs and share the GPU between lanes. They are not in this repo.

1. Synthesize the test clips: `powershell -ExecutionPolicy Bypass -File voice/speech/make_audio.ps1`.
2. Run the CPU smoke test: `python voice/speech/smoke.py`.
3. Run the capability probe on the GPU: `python voice/speech/probe.py cuda`.
4. Run the benchmark: `python voice/speech/bench.py cuda load` five times, then `python voice/speech/bench.py cuda 5`. For the CPU, set `CUDA_VISIBLE_DEVICES=` and pass `cpu`.
5. Run the llama.cpp checks: `python voice/speech/llamacpp_rt.py 5 gpu`, `python voice/speech/llamacpp_rt.py 5 gpu concurrent`, and, with `CUDA_VISIBLE_DEVICES=`, `python voice/speech/llamacpp_rt.py 5 cpu concurrent`.
6. Print every latency table: `python voice/speech/report.py`.

## Licenses

The model is under the LFM Open License v1.0. Commercial use is licensed only while the user's legal entity has annual revenue under $10M (section 5). Redistribution needs the license text and change notices (section 4). The GGUF repo carries the same license, and llama.cpp is MIT. See [licenses.md](licenses.md) for the quotes and the package and binary licenses.

## Remote-code review

`trust_remote_code=True` runs nine Python modules from the model snapshot with the permissions of the user who runs the script. I read every module at revision `2d6c1f99c129b3ef07a8d168bc0ff08e2bdb93f1` before running any of them.

| Module | Lines | SHA-256 |
| --- | --- | --- |
| `configuration_lfm3.py` | 132 | `176cced35657aedfea7d7002221360582d1c05fcaca882c21508bc3a73b282e5` |
| `configuration_lfm3_audio.py` | 273 | `820dd7b7aaf5c70677569b282ba4e3c31866a03e25c06d5744ba6e0b465e9a32` |
| `configuration_liquid_codec.py` | 134 | `ad89deb17164b8ae9a59d9c86545fd01ad281786c8702850aff9042e5f94a463` |
| `feature_extraction_lfm3_audio.py` | 292 | `494061ef2db58af5fe3d4964afd36e334225b7ca8db546e740272da38841f70d` |
| `lfm3_conv.py` | 202 | `80d4a985382a22774212759248a6b4c737bfbf9c2a47b3dd7f797dda748e4bb4` |
| `modeling_lfm3.py` | 881 | `e8cd5254ea99ce6bd731b57b10908c2e8b5010ce8315341f895a38484885eeab` |
| `modeling_lfm3_audio.py` | 1753 | `8d7225d892b8b09edbe2285a8add883ce46e28c4027edc2b8c59b501eeadb9e0` |
| `modeling_liquid_codec.py` | 381 | `5ca87e1b6e0549b40dc90fad5ee9d55620e9defb78c67e19c5937208c8e14f74` |
| `processing_lfm3_audio.py` | 371 | `fc87b2d078f292cb45a317395f9dc69aefb98ac383ae68cc16686025e87bbc38` |

Reproduce the hashes with `sha256sum *.py` in the snapshot directory.

What the review found (measured, by reading the code and by `grep` for `exec`, `eval`, `compile`, `__import__`, `importlib`, `subprocess`, `os.system`, `socket`, `urllib`, `requests`, `http`, `pickle`, `torch.load`, `open(`, `shutil`, `tempfile`, `ctypes`, `marshal`, `base64`, `os.environ`, and `getenv`):

- No module calls `exec`, `eval`, `subprocess`, `os.system`, `ctypes`, `marshal`, `pickle`, or `torch.load`. No module opens a socket or imports an HTTP client.
- No module reads environment variables or writes files during inference.
- The only imports outside the standard library are `torch`, `numpy`, `transformers`, `huggingface_hub.dataclasses`, and `triton`. `lfm3_conv.py` imports `triton` only if `transformers.utils.is_triton_available()` is true. The venv has no `triton`, so that branch is dead here.
- Three paths can reach the Hub through `transformers` library functions, never through the module's own code:
	- `feature_extraction_lfm3_audio.py:116` calls `transformers.utils.cached_file` to look for `processor_config.json` and `preprocessor_config.json`. It passes through only `cache_dir`, `force_download`, `local_files_only`, `token`, `revision`, `subfolder`, and `proxies`.
	- `processing_lfm3_audio.py:178` calls `LiquidCodecModel.from_pretrained` on the `audio_tokenizer` subfolder of the same repo. It reads `audio_tokenizer/model.safetensors` (safetensors, not pickle). This is the codec load round 1 flagged. It forwards `revision`, so a pinned load stays pinned.
	- `processing_lfm3_audio.py:148-154` can call `push_to_hub`, but only from `save_pretrained(..., push_to_hub=True)`. Stage 1 never calls `save_pretrained`.
- `config.json` names a tokenizer repo, `LiquidAI/liquid7.1_tokenizer` at `39badc470af6cdf6f6a6fbb4d6d1c4d1929b5b96`. `configuration_lfm3_audio.py:268` drops that key in `_convert_legacy_config`. No code downloads it. The snapshot carries its own `tokenizer.json`.
- `modeling_lfm3_audio.py:1731` and `modeling_liquid_codec.py` call `register_checkpoint_conversion_mapping(..., overwrite=True)`. That edits a dict inside the running `transformers` process to rename checkpoint keys. It changes nothing on disk.

Verdict (inferred from the review). The code downloads nothing beyond the files of the pinned repo and phones home nowhere. `transformers` itself still contacts the Hub to check for updates unless told not to. Every Stage 1 script therefore sets `HF_HUB_OFFLINE=1` and `HF_HOME=$VOICE_HOME/hf`, and passes `revision=` with the pinned SHA. The CPU smoke test loaded the model and transcribed `single.wav` exactly under `HF_HUB_OFFLINE=1`, so the runs used only the cached files (measured).

## Files outside the repo

| Path | Contents | Size |
| --- | --- | --- |
| `$VOICE_HOME/venv-lfm3` | Round 1's venv with torch replaced by `2.15.0+cu132` | 3.39 GB |
| `$VOICE_HOME/hf` | Round 1's HF cache, the pinned snapshot | 3.9 GiB (4.17 GB) |
| `$VOICE_HOME/evidence/stage1` | Test clips, probe and benchmark JSON and logs, reply WAVs | 6.6 MB |
| `$VOICE_HOME/research/cards` | GGUF and ONNX model cards, llama.cpp server README, release JSON | 1.1 MB |
| `$VOICE_HOME/llamacpp` | llama.cpp b12428 CUDA 12.4 binaries, zips deleted after extraction | 1.3 GB |
| `$VOICE_HOME/gguf` | `LFM3-Audio-2B-Q4_K_M.gguf`, `mmproj-LFM3-Audio-2B-Q8_0.gguf`, and LICENSE | 1.4 GB |

This round added about 5.6 GB of disk: 2.9 GB for the CUDA torch swap, 2.7 GB for llama.cpp and the GGUF files, and 7 MB of evidence. C: had 18 GB free at the end (measured with `df -h /c`).
