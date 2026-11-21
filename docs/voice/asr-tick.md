# The live transcript's tick rate

The decider acts on the live transcript, so a word that reaches it sooner can start work sooner. This note finds what sets the transcript's update interval and what it costs to shorten. Each claim carries a label. "Measured" means a run on this Desktop produced it, "inferred" means I reasoned it from code, and "guess" means neither.

## Answer

The tick is the model's own text block, 12 audio rows or 0.96 s, and the model does not work with any other size. llama.cpp fixes that size in code. Our server adds no delay. Blocks of 6, 4 or 3 rows produce garbage (word error rate 78% to 139%, measured). Shifting the block grid does work. Send the same audio to N transcription sessions, where session i hears i/N of a block of silence first. Their blocks end 0.96/N apart, and the merged transcript updates that often. It needs no llama.cpp change.

On Mason's recordings, 2 sessions update every 0.48 s and put each word on screen 0.18 s earlier on average (0.48 s earlier at the 90th percentile). 4 sessions update every 0.24 s and gain 0.25 s on average. The final transcript is identical to today's in 54 of 54 clips. Two sessions add 54 ms to the first reply audio. `--asr-stagger N` on the live server turns it on. The default stays 1.

## What sets the tick

| Layer | Finding | Label |
| --- | --- | --- |
| Model | The live-ASR context is trained on `[12 audio rows] text <\|text_end\|>` blocks. `interleaved_n_audio` is 12 in the config, and 12 rows at 80 ms is 0.96 s. | Measured (config and `transcribe_live` docstring) |
| llama.cpp | `mtmd_audio_stream_lfm3a` holds `chunk_rows = 12` as a `static constexpr`. `step_live` waits until 12 rows are ready, feeds them, then samples text until `<\|text_end\|>`. The encoder runs once per 12 rows. | Measured (source at b12428, `tools/mtmd/mtmd.cpp`) |
| Our server | `MicTurn.stream` sends 40 ms of audio per message with no coalescing. | Measured (`voice/live/server.py`) |
| Recorded calls | In 37 real turns (84 intervals) the gap between text bursts has median 0.959 s, 10th to 90th percentile 0.888 to 0.998 s. A block's tokens arrive within about 30 ms, so each tick is a burst. | Measured |
| After audio end | 0.81 bursts per turn arrive after the audio ends, 18% of the delta pieces. That is the last partial block, flushed. | Measured |

The limiter is therefore the block size. A word spoken just after a block closes waits up to 0.96 s, and the average wait is about half that (inferred from the geometry). Compute is not the limiter, because the final transcript lands a median of 0.05 s after the audio ends (measured, single session, 54 clips).

## Options

All rows ran 54 clips (11 of Mason's scripted recordings and 43 live-call turns, 244 s of audio) streamed at real-time pace to llama.cpp b12428 with the Q4_K_M weights on the RTX 3060, inside the GPU lease and `quiet_run`. "Gain" is how much earlier each word first appears than in a single session on the same clip (paired by word position). WER is against `script.txt` for the 11 recordings (139 words), and the scripted words are approximate because Mason spoke naturally. "Drift" is the word error rate of a session's final text against the single session's.

| Option | Update interval | First text from audio start | WER vs script | Drift | Word gain, mean (90th pct) | GPU beside a voice session |
| --- | --- | --- | --- | --- | --- | --- |
| One session (today) | 0.960 s | 1.06 s | 3.6% | 0 | 0 | Voice first audio 0.187 s after the end, GPU 23% busy, 2.8 GB |
| 2 staggered sessions | 0.480 s | 1.01 s | 3.6% (final) | 0 (final) | 0.18 s (0.48 s) | 0.241 s (+54 ms), 29%, 2.9 GB |
| 4 staggered sessions | 0.241 s | 1.00 s | 3.6% (final) | 0 (final) | 0.25 s (0.63 s) | 0.275 s (+88 ms), 31%, 3.0 GB |
| Block of 6 rows | 0.49 s median, 1.86 s at the 90th | 1.05 s | 78% | 64% | not usable | not run |
| Block of 4 rows | 0.63 s, 1.33 s | 0.98 s | 85% | 99% | not usable | not run |
| Block of 3 rows | 0.48 s, 1.15 s | 1.01 s | 139% | 82% | not usable | not run |
| Block of 6 rows, encoder still stepping 12 rows | 0.96 s | n/a | 81% | 83% | not usable | not run |
| Peek at the open block (not built) | any, for example every 0.24 s | up to 0.96 s sooner | 10.7% of peeks had a wrong tail word | n/a | 2.0 words ahead on average | inferred, see below |

Notes on the table.

- The staggered rows' WER and drift are for the merged transcript's final text. It comes from the unshifted session, so it equals today's output (54 of 54 clips identical, measured through the prototype classes). Each shifted session on its own scored 4.3% and 4.0% against the script with drift of 0.6% and 0.9% from the single session (measured, the benchmark's per-session numbers). Those gaps are one or two words in 139.
- Time to the first text does not improve. The first word of speech still has to finish inside a block (measured, 1.00 to 1.06 s).
- The 6-row run with the encoder held at 12 rows tells apart two causes. The encoder step is not why small blocks fail. The language model was only trained on 12-row blocks (measured). A 6-row block reaches the language model with the same encoder rows, and it still outputs text such as multilingual fragments for an English clip.
- GPU numbers come from `nvidia-smi` samples at 0.5 s during the voice-beside runs, which held the GPU lease alone (measured, 11 recordings, 11 replies each). Reply generation stayed at a real-time factor of 0.13 to 0.14 in all three. Each extra session adds about 0.06 GB. The ASR final flush grows with sessions, from 0.14 s to 0.18 s (2 sessions) and 0.27 s (4 sessions) after the audio ends (measured, prototype path).
- Peek quality comes from fresh sessions fed each clip's prefix at 0.24 s steps (989 cuts). That is what a forked-context peek would see, because the model's final block is trained with a short audio run plus `<|mixed_end|>`. Its cost needs a llama.cpp change and I did not build it. Each peek prefills up to 12 rows and samples a few tokens on a copy of the slot's KV cache, which is cheaper than a session (inferred, a guess about throughput).

## Merging the sessions

Lane 0 hears no silence and is the authority. Its text is what a single session would have given. When a shifted lane emits, the merge aligns lane 0's text to a prefix of the lane's text by word edit distance and appends the lane's remaining words. Lane 0's next block replaces the lot. The rule keeps the transcript append-only between lane 0's blocks. A latest-writer-wins merge was measured first and rewrote earlier words in 11% to 14% of updates (offline, 2 and 4 sessions). Aligned appending rewrites an earlier word (other than the one being spelled out) in 1.6% (2 sessions) and 6.2% (4 sessions) of updates (measured, prototype path). Most of those come at the start of a clip, where a shifted lane hears a word before lane 0's first block closes.

The decider receives whole-utterance snapshots with a revision number (`TranscriptUpdate`), so a rewritten tail word is an ordinary new revision (inferred from `voice/segmentation/contract.py` use in `prototype/transcript.py`).

## What I did not try

- A llama.cpp patch for peeking. It needs a second sequence in the slot, a copy of the KV state, the final-block encoder flush on a throwaway encoder state, and a new event for provisional text. It is the only route to an update interval shorter than 0.24 s without multiplying sessions.
- Overlapping windows over a restarted context. The model's ASR context restarts at 15 s to 30 s anyway, and a per-window restart loses the lookback (guess, not tried).
- The bf16 weights, and the Transformers path. The safetensors are deleted, and the Transformers path writes the same 12-row blocks.

## Reproduce

`docs/voice/asr-tick-llamacpp.patch` makes the block size and the encoder step settable by environment variable. It is a research aid. Apply it to llama.cpp b12428 and build with CUDA. Do not replace the shared binary.

```
python voice/research/speech/asr_tick_bench.py --out RUN.json --clips all                      # one session
python voice/research/speech/asr_tick_bench.py --out RUN.json --clips all --stagger 4          # 4 shifted sessions
python voice/research/speech/asr_tick_bench.py --out RUN.json --clips real --with-voice --stagger 2
python voice/research/speech/asr_tick_bench.py --out RUN.json --clips all --prefix-step 0.24   # peek quality
python voice/research/speech/asr_tick_report.py BASE.json RUN.json ...
```

`--bin` and `--chunk-rows` point the bench at a patched build. Timing runs went through the GPU lease and `quiet_run.py`, which are local wrappers outside the repo. Turn recordings stay under `$VOICE_HOME/evidence/live`, never in the repo.

To use the prototype in a call, start the live server with `--asr-stagger 2`. It opens 3 slots instead of 2.
