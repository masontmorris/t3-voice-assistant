# Local decider: d1-2B on llama.cpp beside LFM3

Measured on the Desktop's RTX 3060 (12 GB) on 2026-11-16, with `LiquidAI/d1-2B-GGUF` file `d1-2B-Q4_K_M.gguf` served by llama.cpp b12428 on `/v1/systemone`. No run made an OpenRouter call. Every number below comes from `python -m voice.local_decider.report`, which writes `voice/local_decider/results/report.json` from the bench output under `$VOICE_HOME/evidence/local-decider/` and the committed scenario results. Run the report from the LFM3 venv so the clip section can import the prototype. Each claim says whether it is measured, inferred, or a guess.

d1 fits beside LFM3 and keeps up with speech, but it loses too many scenarios to replace pplx-decider-flash as served today. The hybrid with d1 passed 8 of the original 11 scenarios and 7 of the held-out 18, against pplx's 11 and 14, and 6 of Mason's 10 clips against pplx's 7 (measured). Most of the new failures are corrections that d1 points at the wrong target or mode. Per call, d1 took a median of 395 ms while LFM3 listened, against the cloud's 101 ms (measured). The largest cost is that llama.cpp reads the whole state once per question, and d1's attention design rules out any cache that could share it.

## Recommendation

- Keep the cloud decider as the default. The decider is chosen by `--decider cloud|d1` on `voice.segmentation.run` and `voice.prototype.run`, or by `$VOICE_DECIDER`, and the default stays `cloud`.
- Start LFM3 with `-c 32768`. Beside d1, total GPU memory fell from 7,154 MiB to 4,430 MiB, and the prototype gave the same result on every clip (measured, 10 clips at each context).
- Do not adopt answer reuse. It cut 11% of the questions and changed 17% of the answers it reused (measured, 94 calls).
- Do not retune thresholds on llama.cpp's d1 answers yet. The packed Transformers path answers differently, so tuning on one serving path would not carry over to the other (measured, 23 changed answers on 5 requests, below). Pick the serving path first, then retune on a fresh scenario set. That retune is a question-wording change for `corrects` and `depends` plus a tuning run, about one to two days of one worker (guess).
- The next speed step is a packed serving path that fits in memory. Transformers' packed path was 2.2 times faster than llama.cpp on the slowest requests and agreed with pplx more often, but it peaked at 8.3 GB of allocated memory (measured). mistral.rs packs the same way with the small GGUF, but trying it is blocked on disk. See [mistral.rs is blocked on disk](#mistralrs-is-blocked-on-disk).

## d1 alone

`python -m voice.local_decider.bench replay` sends all 565 recorded decisions requests once per server setting per round. They come from the prototype's decider cache (80) and the fork's `voice/segmentation/cache/` (485). Settings alternate within each round, over 3 rounds.

| Setting | Median | p95 | Max | Calls per second | GPU memory over idle | Errors |
| --- | --- | --- | --- | --- | --- | --- |
| Batch 8192, flash attention on (the card's flags) | 118.6 ms | 514.4 ms | 2,383 ms | 4.83 | 2,383 MiB | 0 |
| Batch 4096, flash attention on | 120.3 ms | 523.9 ms | 2,377 ms | 4.83 | 1,703 MiB | 0 |
| Batch 2048, flash attention on (the new default) | 120.4 ms | 519.5 ms | 2,409 ms | 4.84 | 1,411 MiB | 0 |
| Batch 512, flash attention on | 117.6 ms | 466.7 ms | 968 ms | 5.48 | 1,213 MiB | 12 of 565 per round |
| Batch 8192, flash attention off | 122.5 ms | 548.0 ms | 2,707 ms | 4.60 | 6,079 MiB | 0 |

- Batch 4096 and batch 2048 gave byte-identical answers to batch 8192 on all 565 requests (measured). Every question prompt fits in 2048 tokens, so the GPU does the same work at either size (inferred). `voice/local_decider/client.py` now starts d1 with batch 2048.
- Batch 512 fails the 12 requests whose single-question prompt is longer than 512 tokens. The server answers "input (521 tokens) is too large to process" (measured).
- Flash attention off changed 14 of 3,346 answers by at most 0.04 in probability, used 2.5 times the memory, and was 3% slower (measured). It stays on.
- No setting changed any answer between rounds (measured).
- Latency fits 22 ms plus 122 ms per 1,000 input tokens, about 8,200 tokens per second (measured, batch 2048). That matches the PM's prefill bench of LFM3-Audio at 7,900 to 8,600 tokens per second, so prompt prefill is the limit (inferred).
- The server starts in 1.1 to 1.6 s (measured, 18 starts).

The tail comes from requests with many questions. llama.cpp evaluates each question as its own prompt of the state plus that question, so a request costs about questions times state tokens (measured from the server's per-task token counts). The 5 slowest requests had 28 or 29 questions, 16,425 to 19,589 input tokens, and took 1.97 to 2.38 s. All 5 came from the prototype's decider cache, and none is a hybrid call, so they come from its earlier baseline-segmenter runs (inferred). The hybrid's own calls are smaller. Their median was 323 ms and their slowest 953 ms (measured, 94 calls).

## Agreement with pplx-decider-flash

d1's answers at batch 8192, round 0, against pplx's recorded answers to the same 94 hybrid requests (measured). A noul answer agrees when both are on the same side of 0.5.

| Question | Answers | Agreement |
| --- | --- | --- |
| `kind` | 175 | 94.3% |
| `depends` | 82 | 91.5% |
| `side_effect` | 175 | 91.4% |
| `ready` | 175 | 80.6% |
| `finished` | 70 | 80.0% |
| `at` | 70 | 75.7% |
| `corrects` | 82 | 74.4% |
| `same_as` | 3 | 100% |

## The hybrid with d1 on the scenarios

`python -m voice.segmentation.run --option h --mode stream --decider d1` ran once per set, without and with `--reuse`. The pplx columns are the committed PR #6 runs.

| Set | pplx pass | d1 pass | d1 with reuse pass | Premature, pplx / d1 | Calls, pplx / d1 | Final segments changed |
| --- | --- | --- | --- | --- | --- | --- |
| Original 11 | 11 | 8 | 8 | 0 / 1 | 32 / 34 | 1 |
| Held-out 18 | 14 | 7 | 7 | 1 / 3 | 36 / 38 | 2 |

d1 found the same final segments as pplx in all but 3 of 29 scenarios, so the losses come from classifying segments, not from finding breaks (measured).

- Corrections account for 7 of the 11 new failures: `04_mid_sentence_correction`, `05_correction_after_dispatch`, `actually`, `i_mean`, `no_wait`, `pause_correction` and `cancel_second` (measured). d1 labels these segments `kind: correction`, but its `corrects` answer is wrong. In `04_mid_sentence_correction` and `no_wait` it chose a cancel where the speaker replaced the request. In `actually` and `cancel_second` it chose `none` (measured, final decision points). The earlier task is then left for clarification, withdrawn, or dispatched.
- Dependency answers account for 3: `08_delegate_after_dispatch`, `museum_chain` and `post_and_read`. `museum_chain` also changed its final segments (measured).
- `assistant_question` dispatched "What did you just say?" as a task (measured).
- d1 passed `market_three`, which pplx failed. It ended the first request at the right marker (measured).

## Mason's clips beside LFM3

`python -m voice.prototype.run <clips> --inject c --decider d1` ran the 10 clips in `$VOICE_HOME/audio/real/` once at LFM3's default context and once with `--lfm-ctx 32768`. The cloud column is the hybrid's PR #6 run, whose decider calls were live.

| Measurement | Cloud | d1, LFM3 at 128k | d1, LFM3 at 32k |
| --- | --- | --- | --- |
| Clips passed | 7 of 10 | 6 of 10 | 6 of 10 |
| Premature dispatches | 0 | 0 | 0 |
| Total GPU memory after both load | not measured | 7,154 MiB | 4,430 MiB |
| Decider call while audio streams, median (p95) | 101 ms (291 ms) | 395 ms (840 ms) | 426 ms (847 ms) |
| Decider call after the audio ends, median (p95) | 106 ms (181 ms) | 324 ms (730 ms) | 339 ms (747 ms) |
| Snapshot ready after the audio ends, median (max) | 0.258 s (0.310 s) | 0.414 s (1.619 s) | 0.502 s (1.643 s) |
| First reply audio after the audio ends, median (max) | 0.408 s (0.504 s) | 0.567 s (1.695 s) | 0.585 s (1.722 s) |
| Lag from a word's first partial to the decision that covers it, median (p95) | 1.093 s (3.050 s) | 1.283 s (3.473 s) | 1.257 s (3.406 s) |
| Words no partial decision covered | 52 of 120 | 49 of 120 | 49 of 120 |

- d1 failed `04_correction.wav` as well as the 3 clips pplx fails (measured). It is the same correction weakness as in the scenarios (inferred).
- `-c 32768` cut total GPU memory by 2,724 MiB with d1 loaded (measured). The PM's estimate was about 2,250 MiB from the KV cache alone. Every clip passed or failed the same way at both contexts (measured).
- Every d1 call in the clip runs finished in under 1 s (measured, max 961 ms). Transcript updates arrive in bursts about once a second, so d1 keeps up with speech on these clips (inferred).
- The slow snapshots, 1.6 s on `08_cancel.wav` and 1.5 s on `09_run_on.wav`, are two calls in a row. A partial call that started just before the audio ended finished 0.68 s and 0.77 s after it, and the final-transcript call ran after that (measured, run timelines). The planned design makes no decider call at the end of an utterance, so these snapshots would have been ready at 0.68 s and 0.77 s (inferred).
- About 40% of the words were never covered by a partial decision under either decider (measured). The hybrid calls only when a break marker appears or moves, so the last words of an utterance often wait for the final transcript. That count reflects the call policy, not decider speed (inferred).
- The lag metric indexes words by position in the partials. Every partial in these runs extended the one before it, so positions hold (measured, the 10 runs at 32k).

### Barge-in: d1 while LFM3 speaks

`python -m voice.local_decider.bench bargein 02_two_requests.wav --lfm-ctx 32768` asked LFM3 for a spoken reply 12 times. In 6 of them, d1 answered recorded hybrid requests back to back for the whole reply. The two kinds alternated.

| Measurement | Without d1 | With d1 busy |
| --- | --- | --- |
| LFM3 first audio after the request, median | 0.322 s | 0.404 s |
| LFM3 time to finish the reply, median | 0.852 s | 2.279 s |
| d1 call while LFM3 speaks, median (p95) | not applicable | 678 ms (809 ms) |

The replies held 3.4 to 4.6 s of audio (measured). With d1 busy the whole time, LFM3 still produced audio faster than real time, at about 0.6 s of work per second of audio (inferred from the medians). d1 calls took about twice as long as alone (measured). A back-to-back load is the worst case. The segmenter calls at most about once per second of speech (inferred).

## llama.cpp's prompt cache reuses nothing

`python -m voice.local_decider.bench promptcache` ran 6 flag sets with debug logging. It read the server's own per-task `cached n_tokens` counts. The flag sets were the default, `--cache-ram 4096`, context checkpoints with `--checkpoint-min-step 0 --ctx-checkpoints 128`, both together, both with `--swa-full`, and `--cache-ram 4096 --cache-reuse 16`.

- **Across calls.** Over the hybrid's 68 scenario calls in order, every flag set reused 0 of 179,483 prompt tokens (measured, round 0).
- **Within a request.** On the 5 slowest requests, each question's prompt shared 465 to 552 tokens with the question before it. Every flag set reused 0 of them, in all 3 rounds (measured).
- **An identical repeat.** Sending each slow request twice in a row also reused 0 tokens (measured).
- Latency did not move with any flag set. The median on the slow requests was 2,330 to 2,354 ms (measured).

The model rules this out. d1-2B's config sets `"d1_attention": "set"`, and its `attention.py` says that under `set` "a state token sees the state and every question" on the full-attention layers. A state's cached values therefore depend on the question that follows it, and no cache can carry the state to another question. llama.cpp's converter records the same thing: "a lone question reads the whole prompt both ways on the full attention layers". No server flag changes it (inferred from the code and the zero counts).

## Answer reuse

`voice/local_decider/reuse.py` keeps each answer keyed by its question, its piece's text, and whether that piece is still being spoken. It re-asks a question when that key changes. It always re-asks the break questions of the newest piece and the one before it, and any question that names no segment. `python -m voice.local_decider.bench reuse` replayed the hybrid's 94 calls in order. It sent each request twice, once in full and once through the reuse layer, alternating which went first.

| Measurement | Full request | With reuse |
| --- | --- | --- |
| Questions asked | 835 | 742 |
| Input tokens | 237,683 | 208,496 |
| Call latency, median (p95) | 323 ms (651 ms) | 291 ms (557 ms) |

Of the 93 answers it reused, 16 differed from a fresh answer to the full request, a reuse error of 17% (measured). 12 of the 16 were `ready`, because readiness depends on the speech after a piece, not only on its own words (inferred). The saving is small because the hybrid calls only 2 or 3 times per utterance, so few pieces repeat with unchanged text (inferred). Answer reuse did not change any scenario pass count (measured).

## The packed Transformers path

`voice/local_decider/transformers_bench.py` ran d1-2B's own `system_one` in bf16 from the Hugging Face cache, revision `652f920e544ea37f81e4b0ce89b81d4ed0da90b1`, read-only, in decision-lab's venv (torch 2.11.0+cu128, transformers 5.19.0). This torch build has no flash attention, so the run routed the model's `_fused` check to false and used its portable masked-attention path, as decision-lab does. Each request ran 5 times at the default group size of 16 questions per state and at 1, alternating.

| Requests | llama.cpp | Transformers, packed 16 | Transformers, one question at a time |
| --- | --- | --- | --- |
| 5 slowest, 28 or 29 questions | 1,970 to 2,376 ms | 944 to 1,098 ms | 3,647 to 4,316 ms |
| 5 median, 3 to 5 questions | 108 to 126 ms | 112 to 138 ms | 187 to 269 ms |
| Input tokens, 5 slowest | 16,425 to 19,589 | 4,387 to 4,739 | the same as llama.cpp |
| Changed answers against pplx, 5 slowest (144 answers) | 21 | 7 | 15 |

- Packing reads the state once per 16 questions. On the slowest requests it read about a quarter of the tokens and ran 2.2 times faster than llama.cpp, although its kernels ran 1.8 times slower per token (measured).
- On median-size requests the two paths were within 30 ms (measured).
- Packed answers changed 23 of llama.cpp's 144 answers on the slowest requests, by up to 0.55 in probability. One question at a time changed 6 (measured). Packing is d1's trained mode, and it agreed with pplx on 137 of 144 answers against llama.cpp's 123 (measured, 5 requests).
- The model loaded at 3,787 MiB over idle, but torch's peak allocation reached 8,302 MiB and total GPU use reached 11,694 MiB (measured). That does not fit beside LFM3. The portable attention path builds full mask tensors for long packed rows (guess).

## mistral.rs is blocked on disk

mistral.rs serves d1-2B on `/v1/systemone` and packs questions in groups of `set_size`, 16 by default, so it could give the packed path's speed from the 1.1 GB GGUF. Disk is the only blocker. A source build would take C: below the 20 GB floor, so it was not installed.

The build has to come from source, which is an ordinary path on this machine. Rust and Visual Studio are already installed.

- No release includes d1 support yet. It merged on 2026-11-01 as pull request 2530, after the newest release, v0.9.5 (2026-10-19) (measured, GitHub API).
- Releases ship CUDA builds only for Linux. Windows gets only a CPU build (measured, GitHub releases). A Windows CUDA build is a `cargo build --release --features cuda` from the main branch.
- The build needs the CUDA Toolkit components, about 2.5 to 3.5 GB, which this machine lacks. It also needs a build tree of about 5 to 10 GB and about 1 GB of crates. That totals about 8 to 14 GB (guess). C: has 20.4 GB free, so the build would leave about 7 to 12 GB (inferred).
- The first compile probably takes 30 to 60 minutes (guess). Whether it compiles cleanly on Windows the first time is unknown. The flash-attention feature may not build there, and leaving it out costs only attention speed (guess).
- The voice lane's own files that could go total about 1.1 GB, mostly the d1 GGUF. The large folders that are not the lane's are `voice/venv-lfm3` (3.5 GB), the d1-2B Hugging Face weights (3.6 GB), and `voice/llamacpp` (1.3 GB). Removing all of them still would not keep C: above 20 GB through a build (inferred).
- Building elsewhere would work. Build mistral.rs with CUDA for the 3060's sm86 on a machine with free disk, then copy only the server binary and its CUDA libraries here. That copy is probably about 1 GB (guess).

Mason decides whether to take that route.

## Code

- `voice/local_decider/client.py` holds `D1Client`, the same `call(route, body, label)` interface as the cloud client, and the one adapter between the two APIs. The request drops the OpenRouter `model` field. Each choice answer's `confidence` is rescaled from d1's top probability to pplx's scale, (p_max - 1/n) / (1 - 1/n). That matters because the hybrid holds a `dispatch_now` whose confidence is under 0.2, and a top probability over 3 options is never under 0.33 (measured on cached pplx answers).
- `voice/local_decider/reuse.py` is the answer-reuse layer, as `ReusingClient`.
- `voice/local_decider/bench.py` and `voice/local_decider/transformers_bench.py` produce every bench in this doc.
- `voice/prototype/realtime.py` takes extra LFM3 server arguments, for `--lfm-ctx`.
- Offline tests: `python -m unittest discover -s voice/local_decider/tests -t .`.

Weights, run records, clip transcripts and server logs stay under `$VOICE_HOME/evidence/local-decider/`. They are not committed.
