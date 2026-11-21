# Where the first reply audio time goes, and what cuts it

**Short answer.** On the recorded call, 1.62 s passes between the end of Mason's speech and the first reply audio the server sends (median, measured). The 0.70 s silence rule takes 0.70 s of it. On the current code, replayed on the same 37 clips, the median is 1.58 to 2.34 s, and the largest piece is no longer the silence rule. It is the wait for the whole reply to be generated before any of it is sent, which costs 0.14 s for every second of reply speech (measured, fit r = 1.00 on four record sets). That wait is 1.03 to 1.18 s at the median on current replies. Model compute for the first audio frame is 0.2 to 0.45 s after the turn ends, the smallest large piece (measured).

The three changes with the most expected gain, in order:

1. **Release the reply as it is generated, gated by the fact screen on the text so far.** Median gain 0.75 to 0.93 s on current replies and 0.17 s on the live call's shorter replies (measured upper bound from the records, with one block of lookahead).
2. **Turn `--eou audio` on by default.** Median gain about 0.4 s (measured earlier, 0.46 s in the replays recomputed here). It costs 2.7 GB of VRAM and cut 3 of 37 turns mid-thought.
3. **Start the reply at the end of the turn instead of after the final transcript and the decider.** Median gain 0.10 to 0.21 s (inferred from the records, one number is a guess).

All three together put the median at about 0.55 s from speech end to first audio sent, from 1.58 s in the same replay (inferred by replaying the records' own timestamps), and about 0.67 s heard once the client's playback start and device latency are added. That is inside the 600 to 700 ms that production voice agents quote (see section 2). Nothing here has been run end to end, and Mason's real-microphone result decides acceptance.

Every number below carries one of three labels. **Measured** means it came from a record or a run on this machine. **Inferred** means it follows from measured numbers by arithmetic or code reading. **Guess** means neither.

## Goal and smallest change

Mason wants the assistant to feel like talking to a person. When he stops, it should answer fast. The smallest change that could meet that is the one that removes the largest wait without touching what the voice says. Section 3 argues that is streaming the checked reply.

## How the numbers were made

Sources, all read-only:

- The live call, `$VOICE_HOME/evidence/live/20261116-152653/turn_NNN.json`. 37 records, 35 of them with a reply. The server was running the older snapshot mode, and the voice session got the turn's audio as it arrived.
- Replay A, `$VOICE_HOME/evidence/turnloss/after/server/`. 37 turns, `--eou silence`, current code, the voice session gets the audio at the end of the turn.
- Replay B, `$VOICE_HOME/evidence/eou-audio/silence/server/`. 37 turns, same configuration as A, a separate run.
- Replay C, `$VOICE_HOME/evidence/eou-audio/audio/server/`. 37 turns, `--eou audio`.
- Client logs for A, B and C (`client.jsonl`), for the delivery and playback rows.

The script is `$VOICE_HOME/evidence/latency-research/budget.py` and `whatif.py` (12 KB, not in the repo). Each stage is read from the turn record. The timeline gives the final transcript and the snapshot. The voice session's event log gives `response.create` sent, the first text delta, the first audio delta and `response.done`, each with a time. `live.turn_end_to_first_audio_sent_s` gives the moment the server sent the first checked audio. Speech end is the turn end minus the endpointer's trailing silence.

**What limits each number.** The held-reply wait fits `done - first frame = 0.14 s per second of speech` with r = 1.00 in all four sets (slopes 0.137, 0.138, 0.141, 0.142, measured). That is the model's generation speed, about 7 times real time with the ASR session open, and it matches the 0.13 real-time factor that `stage1-speech-path.md` measured. The 0.70 s is a setting. The gate before `response.create` is the final transcript plus the decider call, in sequence (measured, `to_rc` equals `asr_final + decider` within a few ms in the live call). So each big stage has a named limiter and none is an unexplained residue.

**What else these numbers could be measuring.**

- A cached decider. In replays B and C only 2 and 3 of 37 turns made a live decider call (the rest hit the decider cache), and in A 26 of 37 made none (measured, `metrics.decider_live_calls`). The decider row for the replays is therefore a floor. The live call's 0.16 s median and `liquid-decider.md`'s 138 ms per call are the honest figures.
- Mock tools. The replays run `--mock-tools`, so only 3 of 37 turns carry a task note.
- Reply length. Replies are sampled at audio temperature 1.0. The same configuration (A and B) produced median replies of 8.3 and 7.0 s and median first audio of 2.34 and 2.04 s. That 0.30 s spread is the noise floor for any claim made from one replay run. The 0.4 s gain from `--eou audio` is explained by its mechanism, because the detector row falls by 0.55 s, so the claim does not rest on the replay gap alone.
- Reply length differs between the live call and the replays. The live call's replies had a median of 3.3 s of speech against 7.0 to 8.3 s now (measured). The persona and the snapshot mode changed in between. The held-reply wait scales with this, so it is the number most likely to differ in a new call.
- Network and playback. The replays ran on loopback, so the delivery rows are floors. The laptop client logs from the 15:39 and 15:44 calls (`evidence/latency-research/client-logs/`, 17 turns matched to server records) were measured afterwards: send to receive over the LAN tailnet is 0.013 s median (0.015 p90, clock offset included), and receive to playback callback is 0.024 s median (0.041 p90). Delivery adds about 0.04 s plus the 0.1 s device buffer, so the latency is server-side.

## 1. Measured latency budget

Median and p90 in seconds. Stage times in the middle rows run from the end of the turn (the endpointer firing), not from speech end, except the first and last rows.

| Stage                                                            | Live call (n=35)              | Replay A, silence 700 (n=37) | Replay B, silence 700 (n=37) | Replay C, `--eou audio` (n=37) | Label                                                                                                     |
| ---------------------------------------------------------------- | ----------------------------- | ---------------------------- | ---------------------------- | ------------------------------ | --------------------------------------------------------------------------------------------------------- |
| 1. Speech end to endpointer fires                                | 0.70 / 0.70                   | 0.70 / 0.70                  | 0.70 / 0.70                  | 0.15 / 0.70                    | measured                                                                                                  |
| 2. Turn end to ASR final transcript                              | 0.09 / 0.18                   | 0.12 / 0.30                  | 0.16 / 0.29                  | 0.14 / 0.19                    | measured                                                                                                  |
| 3. ASR final to decider and snapshot done                        | 0.16 / 0.28                   | 0.02 / 0.17                  | 0.00 / 0.02                  | 0.01 / 0.01                    | measured, replays understate it (cached decider)                                                          |
| 4. Turn end to `response.create` sent (rows 2 and 3 in sequence) | 0.26 / 0.44                   | 0.15 / 0.46                  | 0.18 / 0.32                  | 0.15 / 0.21                    | measured                                                                                                  |
| 5. `response.create` to first text token                         | 0.12 / 0.16                   | 0.06 / 0.13                  | 0.07 / 0.17                  | 0.05 / 0.12                    | measured                                                                                                  |
| 6. `response.create` to first audio frame                        | 0.18 / 0.23                   | 0.10 / 0.20                  | 0.12 / 0.21                  | 0.09 / 0.17                    | measured                                                                                                  |
| 6b. Turn end to first audio frame generated (rows 4 plus 6)      | 0.45 / 0.60                   | 0.33 / 0.52                  | 0.31 / 0.48                  | 0.25 / 0.36                    | measured                                                                                                  |
| 7. First frame to `response.done` (the held-reply wait)          | 0.44 / 1.26                   | 1.18 / 1.80                  | 1.03 / 1.31                  | 1.09 / 1.40                    | measured                                                                                                  |
| 8. `response.done` to first audio sent (fact screen)             | 0.00 / 0.01                   | 0.01 / 0.01                  | 0.00 / 0.01                  | 0.01 / 0.01                    | measured, one retry turn per run costs 1.1 to 1.4 s                                                       |
| 9. Turn end to first audio sent                                  | 0.92 / 1.71                   | 1.64 / 2.32                  | 1.34 / 1.70                  | 1.38 / 1.75                    | measured                                                                                                  |
| 10. **Speech end to first audio sent**                           | **1.62 / 2.41**               | **2.34 / 3.02**              | **2.04 / 2.40**              | **1.58 / 2.17**                | measured                                                                                                  |
| Reply length, seconds of speech                                  | 3.3 / 8.6                     | 8.3 / 12.9                   | 7.0 / 9.4                    | 7.6 / 9.8                      | measured                                                                                                  |
| 11. Server to client receive                                     | 0.013 (p90 0.015), laptop log | 0.005 (max 0.010)            | 0.006 (max 0.008)            | 0.006 (max 0.008)              | measured on loopback. Tailnet RTT was 5 ms on the LAN (`live-demo.md`), a WAN or relay path is unmeasured |
| 12. Client receive to playback callback                          | 0.024 (p90 0.041), laptop log | 0.014                        | 0.012                        | 0.012                          | measured                                                                                                  |
| 12b. Output device latency                                       | 0.100                         | 0.100                        | 0.100                        | 0.100                          | inferred, the client's own `stream.latency` report                                                        |

Row 10 is the number the brief asks about. It matches the brief's 1.62 s for the live call. It does not match "1.7 to 2.0 s" for the replays, which measured 1.58 to 2.34 s here depending on the run and the mode.

**Which stage is largest.** Count the stage that is largest in each turn (measured).

| Set                 | Endpoint silence | Held-reply wait | Gate before `response.create` | Retry |
| ------------------- | ---------------- | --------------- | ----------------------------- | ----- |
| Live call, 34 turns | 26               | 8               | 0                             | 0     |
| Replay A, 37 turns  | 6                | 30              | 0                             | 1     |
| Replay B, 37 turns  | 9                | 27              | 0                             | 1     |
| Replay C, 37 turns  | 1                | 34              | 2                             | 0     |

The answer depends on how long the replies are. On the live call's short replies the silence rule is largest (0.70 of 1.62 s, 43%). On current replies the held-reply wait is largest (1.03 to 1.18 s, 50% to 69% of the total). Both are waits, not compute. The model's own work, from turn end to the first audio frame, is 0.25 to 0.45 s (row 6b).

**Two readings of the shares at the median** (sums of medians, so approximate).

|           | Endpoint   | Gate (rows 2 and 3) | First frame (row 6) | Held-reply wait |
| --------- | ---------- | ------------------- | ------------------- | --------------- |
| Live call | 0.70 (43%) | 0.26 (16%)          | 0.18 (11%)          | 0.44 (27%)      |
| Replay B  | 0.70 (34%) | 0.18 (9%)           | 0.12 (6%)           | 1.03 (50%)      |
| Replay C  | 0.15 (9%)  | 0.15 (10%)          | 0.09 (6%)           | 1.09 (69%)      |

**Why the first frame is cheap.** Audio generation is not the limit. 12 audio frames are 0.96 s of speech, and the model emits them within about 0.13 s of the first one (measured, from the audio delta timestamps). The server prefills the turn's audio after the turn ends in replays A to C, and that costs about 0.01 s per second of audio (inferred, regression of row 5 on audio length, r = 0.14 to 0.66). It is hidden by the gate in row 4 anyway.

## 2. What the best systems achieve, and how

### 2.1 Reported figures

| System                                | Figure                                                                                                                                                  | What it measures                                                 | Source                                                                                                                                                                                                                                            |
| ------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Human conversation                    | about 230 ms average gap between turns across 10 languages                                                                                              | gap between one speaker stopping and the next starting           | [Stivers et al. 2009](https://pubmed.ncbi.nlm.nih.gov/19553212/), as quoted in the [Moshi paper](https://arxiv.org/abs/2410.00037). [Jacoby et al.](https://arxiv.org/abs/2404.16053) cite 239 ms with a standard deviation of 519 ms for English |
| Moshi                                 | 160 ms theoretical, about 200 ms in practice                                                                                                            | full-duplex model, no turn endpoint                              | [Moshi](https://arxiv.org/abs/2410.00037)                                                                                                                                                                                                         |
| GPT-4o voice                          | 232 ms best, 320 ms average. The older Voice Mode chain took 2.8 s (GPT-3.5) and 5.4 s (GPT-4)                                                          | audio in to audio out                                            | [OpenAI, "Hello GPT-4o"](https://openai.com/index/hello-gpt-4o/). I read the figures in a search extract, because the page itself returned HTTP 403 to my fetch                                                                                   |
| Full-Duplex-Bench, smooth turn-taking | Moshi 0.265 s, dGSLM 0.352 s, Freeze-Omni 0.953 s, Gemini Live 1.301 s                                                                                  | response latency after the user stops (Candor set)               | [Full-Duplex-Bench, Table III](https://arxiv.org/abs/2503.04721)                                                                                                                                                                                  |
| LLaMA-Omni                            | 226 ms at speech-chunk size 10 units. 1,925 ms when it waits for all units                                                                              | start of speech response after the end of the speech instruction | [LLaMA-Omni, Table 2](https://arxiv.org/abs/2409.06666)                                                                                                                                                                                           |
| Freeze-Omni                           | 745 ms average model-side (753 median, 1,020 p90). About 1.2 s with 200 to 300 ms of network                                                            | interrupted LLM to first PCM chunk                               | [Freeze-Omni, Table 4](https://arxiv.org/abs/2411.00774)                                                                                                                                                                                          |
| VITA-Audio                            | first audio chunk after 53 ms, against 236 ms for the vanilla decode                                                                                    | first audio token chunk                                          | [VITA-Audio](https://arxiv.org/abs/2505.03739)                                                                                                                                                                                                    |
| Delayed Streams TTS                   | 150 ms at batch size 1 on an H100                                                                                                                       | text-in to audio-out stream latency                              | [DSM](https://arxiv.org/abs/2509.08753)                                                                                                                                                                                                           |
| CosyVoice 2                           | 150 ms first-packet latency                                                                                                                             | streaming TTS first packet                                       | [CosyVoice 2](https://arxiv.org/abs/2412.10117)                                                                                                                                                                                                   |
| Retell                                | about 600 ms, from 50 network + 200 turn-taking + 250 LLM first token + 100 TTS. One third-party report found 680 ms median and 920 ms p95 on 500 calls | end of caller speech to start of agent speech, server side       | [Retell](https://www.retellai.com/blog/how-real-time-voice-ai-works-stt-llm-tts). The independent figure came through a search result and I did not open its source                                                                               |
| Vapi                                  | stages are transport, transcription, endpointing, model first token, speech synthesis. No numbers are published                                         | per-turn latency in call logs                                    | [Vapi docs](https://docs.vapi.ai/assistants/model-intelligence/understanding-latency)                                                                                                                                                             |
| Unmute (Kyutai cascade)               | 1,195 ms baseline in the endpoint-anticipation study                                                                                                    | end of speech to first audio                                     | [Endpoint anticipation](https://arxiv.org/abs/2606.13450)                                                                                                                                                                                         |

Two of these bear directly on this stack.

- **LLaMA-Omni's table is our situation in miniature.** Its one knob is how many speech units the model generates before the first chunk goes to the vocoder. At 10 units the latency is 226 ms, at 100 units 528 ms, and at "wait for everything" 1,925 ms. The authors report that latency then no longer depends on the text length when units are chunked. Our `speak()` takes the whole reply, which is their infinite setting. The 1.7 s they saw is the same order as our 1.0 to 1.2 s.
- **The fastest end-to-end figures belong to systems that never wait for an endpoint** (Moshi) **or never wait for the end of the reply** (all the rest). Gemini Live, which waits on a server-side silence rule, measured 1.3 s on the benchmark. That is close to our 1.6 s.

### 2.2 Techniques

**Stream the first chunk, not the whole reply.** LLaMA-Omni, Freeze-Omni and VITA-Audio all start audio from a first chunk. LiveKit and Retell stream stage into stage, and Retell's TTS emits audio in 200 to 400 ms chunks ([Retell](https://www.retellai.com/blog/how-real-time-voice-ai-works-stt-llm-tts)). Vapi exposes the wait directly, in `voice.chunkPlan` with `minCharacters` and `punctuationBoundaries`, so the first voice request goes out at the first sentence boundary ([Vapi docs](https://docs.vapi.ai/assistants/model-intelligence/understanding-latency)). Moshi and LFM3 interleave text and audio, so the text for a block is already known when its audio plays (inferred from [Moshi](https://arxiv.org/abs/2410.00037)'s text-prefix design and from the [llama.cpp realtime README](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md), local copy `$VOICE_HOME/research/cards/srv_README.md`, which says the audio is prefilled as it arrives so the response starts right after the last chunk). LFM3-Audio's interleaved mode is 6 text tokens then 12 audio frames per block (`stage1-speech-path.md`, measured), and the trace of replay A's turn 4 shows the same pattern, with the first block's 12 frames arriving within 0.13 s of the first one (measured).

**End of turn.**

- Silence-only rules are the norm and sit at 0.5 to 0.8 s. OpenAI's server VAD example uses 500 ms and says shorter values detect stops sooner ([OpenAI](https://developers.openai.com/api/docs/guides/realtime-vad)). Gemini Live's internal default is about 800 ms, it recommends 500 to 800 ms, and warns that 2,000 ms or more raises perceived latency ([Gemini Live](https://aistudio.google.com/docs/live-api/capabilities)). LiveKit's `min_delay` is 0.5 s and `max_delay` 3.0 s ([LiveKit](https://docs.livekit.io/agents/logic/turns/tuning.md)). Our 0.70 s is mid-range.
- Model-based endpointing is how the leaders get under that. Pipecat runs Silero VAD at `stop_secs` 0.2, then Smart Turn judges the audio, and v3 is an 8 MB model with 12.6 ms CPU inference on a c7a.2xlarge and 94.31% accuracy on English ([Pipecat docs](https://docs.pipecat.ai/pipecat/learn/speech-input), [Daily, Smart Turn v3](https://www.daily.co/blog/announcing-smart-turn-v3-with-cpu-inference-in-just-12ms/)). LiveKit's turn detector v1.0 fuses audio and text and reports a 9.9% false-cutoff rate at a 300 ms budget and 4.5% at 600 ms, with mean latency 295 ms at 10% false cutoffs and 543 ms at 5% ([LiveKit](https://livekit.com/blog/solving-end-of-turn-detection)). Deepgram's Flux quickstart puts end-of-turn detection at about 260 ms ([Deepgram quickstart](https://developers.deepgram.com/docs/flux/quickstart), read in a search extract, page not opened).
- The research line runs from [TurnGPT](https://arxiv.org/abs/2010.10874) (words only, the paper opens by criticizing the fixed 700 ms rule that we use) to [Voice Activity Projection](https://arxiv.org/abs/2205.09812) (audio, self-supervised, predicts both speakers' activity over a 2 s window, with a [real-time demo](https://arxiv.org/abs/2401.04868)) to [Predictive EOU](https://arxiv.org/abs/2409.19990), which predicts an utterance end up to 300 ms before it happens.
- Our own `--eou audio` already sits in this family. It cut the silence to 0.22 s at the median with 3 of 37 mid-thought cuts (`live-eou-audio.md`, measured). That is an 8% cut rate at a median of 0.22 s. LiveKit's best published point is 9.9% at 300 ms. So the cut rate is not an outlier, and the model is doing about what the best public detectors do (inferred, the two test sets differ).

**Speculative generation before the turn is confirmed.**

- LiveKit enables preemptive generation by default. It starts the LLM on the final transcript before the turn is confirmed, skips turns longer than `max_speech_duration` (10 s), retries up to 3 times, and discards the draft if the context or tools change. Preemptive TTS is off by default, because cancelled runs waste compute. The docs warn it "doesn't always reduce latency" ([LiveKit](https://docs.livekit.io/agents/logic/turns/tuning.md)).
- Deepgram's `EagerEndOfTurn` starts the draft, `TurnResumed` cancels it, `EndOfTurn` commits it, and the final transcript is guaranteed to match the eager one. It trims 100 to 200 ms and adds 50% to 70% more LLM calls ([Deepgram](https://developers.deepgram.com/docs/flux/voice-agent-eager-eot)).
- Endpoint anticipation predicts the end up to 2.56 s ahead, generates 10 tokens of look-ahead into a TTS cache, and releases it if the endpoint arrives. On Unmute it took the average from 1,195 ms to 690 ms for 28.4% extra speculative compute ([endpoint anticipation](https://arxiv.org/abs/2606.13450)).
- [RelayS2S](https://arxiv.org/abs/2603.23346) drafts a short spoken prefix from a fast path and lets a slow path continue. P90 onset was 81 ms against 513 ms and 1,091 ms for 7B and GPT-4o cascades. A learned verifier decides whether to commit, and at its 0.50 threshold 45.7% of bad prefixes were still committed. That is a direct warning for any scheme that speaks before checking.
- All of these assume a stateless LLM call that can be thrown away. Ours is one append-only voice session per call. llama.cpp answers `conversation.item.delete` with an unknown event, and every response stays in the context (`live-call3-fix.md`, measured, and the [README](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md)). A cancelled draft therefore stays in the call's memory.

**Filler and backchannel masking.** [Jacoby et al.](https://arxiv.org/abs/2404.16053) propose a filler phrase for questions whose last words change the answer and warn that a filler on every turn gets unnatural quickly. They report no user study. LiveKit's guidance is a thinking sound during tool calls ([LiveKit](https://livekit.com/blog/understand-and-improve-agent-latency)). A vendor page claims fillers make waits feel up to 40% shorter, with no study cited, so I give it no weight. Liquid's own codec research on this machine (`$VOICE_HOME/research/liquidcodec/REPORT.md`) found that fresh LFM3 backchannels cost 213 to 231 ms to generate on the GPU, a bank replenished at idle is the preferred route, and listening review is still missing (measured, inferred, and open).

**KV and prefix caching.** The call's persona and history already live in one session's KV cache, so there is no prefix to rebuild. llama.cpp's prompt cache (`cache_prompt`, `--cache-reuse`, slot save and restore) works at the completion layer, and the realtime session does not expose it. The cached part of a turn is cheap already, because row 5 is 0.05 to 0.12 s (measured). I found nothing here worth ranking.

**Network and playback.** Retell lists 30 to 80 ms of network and cuts TTS inside a single audio chunk on interruption. Pipecat says local VAD is 150 to 200 ms faster than a remote one ([Pipecat docs](https://docs.pipecat.ai/pipecat/learn/speech-input)). Our client adds 5 ms on the LAN tailnet, 12 ms to the playback callback and a reported 100 ms of device latency (measured and inferred above). These are small next to the waits.

**Checking safety on streamed text.**

- [Qwen3Guard-Stream](https://arxiv.org/abs/2510.14276) puts a token-level head on the response stream. It scores about 2 F1 points below the full-context guard, flags from token i only if tokens i and i-1 are both unsafe, and in the host example CARE holds a 40-token buffer.
- [NeMo Guardrails](https://docs.nvidia.com/nemo/guardrails/latest/configure-guardrails/yaml-schema/streaming/output-rail-streaming) buffers `chunk_size` 200 tokens with `context_size` 50 carried over. With `stream_first: false` the rails run on each chunk before anything is sent, which is safer and slower. A failed chunk ends the stream.
- The [OpenAI Agents SDK](https://openai.github.io/openai-agents-python/realtime/guide/) runs realtime output guardrails on the transcript every `debounce_text_length` characters, 100 by default. One [issue](https://github.com/openai/openai-agents-python/issues/1912) reports that the Python SDK emits the trip but does not cancel the speech.
- Our screen is regular expressions and string checks (`voice/live/facts.py`, `refused` in `voice/prototype/run.py`), so running it on each text delta costs microseconds (inferred from the code, not timed). The published designs gate release on a checked prefix, which is the pattern in section 3, change 1.

## 3. Ranked changes for this stack

Gains are medians of speech end to first audio sent, from the records, unless stated. "Heard" adds about 0.12 s (rows 11 to 12b).

| #   | Change                                                                                        | Expected gain                                                                                                                | Cost                                                                                                      | Risk                                                                                                                                                | What it would break                                                                                                                                                                                                                                                                                                             |
| --- | --------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | Release reply audio as the text so far passes the screens, one block ahead                    | 0.75 to 0.93 s on replies of 7 to 8 s, 0.17 s on 3.3 s replies. p90 about 1.0 to 1.4 s on long replies. Measured upper bound | Medium. Incremental screening on `response.output_text.delta`, a release gate, incremental `Playback.add` | Medium. A late failure cuts a reply the person has already started to hear                                                                          | `answer()` and `respond()` return a whole reply today. `speak(text, pcm)` takes whole PCM. `spoken_part` and `cut_at_pause` estimate sentence ends from text share and become unnecessary. The retry note flow needs a decision for a half-spoken reply. Tests in `voice/prototype/tests` and `voice/live/tests/test_server.py` |
| 2   | `--eou audio` on by default                                                                   | 0.38 to 0.46 s, p90 0.2 s. Measured                                                                                          | Low. Already built and tested                                                                             | Medium. 3 of 37 mid-thought cuts. +2.7 GB VRAM and +2.0 s call start. The 0.70 s fallback still ended 6 of 39 turns (`live-eou-audio.md`)           | A cut turn is continued into the next, and its audio may sit twice in the call's voice session (unverified, see experiment 3). VRAM headroom for other lanes                                                                                                                                                                    |
| 3   | Send `response.create` at turn end, in parallel with the final transcript and decider         | 0.10 to 0.21 s. Inferred from row 4 minus a 0.05 s prefill (the 0.05 s is a guess)                                           | Low to medium. Reorder `run_clip`, keep the note path                                                     | Low to medium. In about 5% of turns (2 of 37 had a note decided only at the final) the model would answer without the "checking" note               | The note must precede `response.create` today. A late note means the reply cannot say it is checking. The `RUNNING_REFUSAL` screen then cuts "I can't check that" replies when the snapshot says a task runs                                                                                                                    |
| 4   | Pre-rendered acknowledgement or backchannel at about 0.3 s                                    | Time to first sound drops by 0.5 to 1.3 s on turns that still wait. It adds no content. Guess                                | Medium. A bank of LFM3-voice clips, idle replenishment, cancel on the real reply                          | Medium. Wrong register for a question. Jacoby et al. warn on overuse. A different voice was a measured complaint (turns 22 and 26 of the live call) | New audio path under the playback clock and barge-in. Needs listening review before anything ships                                                                                                                                                                                                                              |
| 5   | Shorter replies by prompt                                                                     | 0.14 s per second removed, so about 0.5 s from 8 s to 4 s. Inferred. Worth almost nothing once change 1 is in                | Low                                                                                                       | Medium. `live-fact-check.md` measured two persona rules the model broke                                                                             | The persona hash and every recorded comparison                                                                                                                                                                                                                                                                                  |
| 6   | Speculative generation before the endpoint (LiveKit preemptive, Deepgram eager, anticipation) | Another 0.1 to 0.3 s after change 2. Guess, from 505 ms on a stateless cascade                                               | High. Needs a throwaway session or slot                                                                   | High. The voice session is append-only, so a discarded draft stays in the call's memory                                                             | The rule "one voice session per call, fix stale context at the source". Not recommended now                                                                                                                                                                                                                                     |
| 7   | Lower `--silence-ms` from 700 to 500                                                          | 0.20 s by arithmetic. Cut rate unmeasured                                                                                    | Trivial                                                                                                   | High. More mid-thought cuts than `--eou audio` per second saved                                                                                     | Superseded by change 2                                                                                                                                                                                                                                                                                                          |
| 8   | Client prebuffer 80 to 40 ms                                                                  | 0.04 s. Guess                                                                                                                | Trivial                                                                                                   | Low to medium. Underruns                                                                                                                            | Nothing found. Skip                                                                                                                                                                                                                                                                                                             |

**Why the order.** Change 1 removes the largest stage on current replies, and it grows with reply length, so it also protects against the persona or the tools making replies longer. It needs text to lead audio, which the interleaved format gives, but I have not verified the alignment (experiment 1). Change 2 is already built, and with change 1 in place it becomes the largest remaining stage (0.70 s against 0.15 s). Change 3 is small but nearly free of model risk. Changes 4 to 8 are either cosmetic, superseded, or break the one-session rule.

**Projection.** Replaying the records' own timestamps with changes applied, speech end to first audio sent, median and p90 in seconds (inferred, one block of lookahead included).

| Set                      | As recorded | Change 1    | Changes 1 and 3 | Changes 1 to 3                     |
| ------------------------ | ----------- | ----------- | --------------- | ---------------------------------- |
| Live call                | 1.62 / 2.41 | 1.43 / 1.61 | 1.21 / 1.26     | no `--eou audio` data on this call |
| Replay A                 | 2.34 / 3.02 | 1.29 / 1.49 | 1.11 / 1.22     | not applicable                     |
| Replay B                 | 2.04 / 2.40 | 1.29 / 1.44 | 1.13 / 1.22     | not applicable                     |
| Replay C (`--eou audio`) | 1.58 / 2.17 | 0.68 / 1.28 | 0.55 / 1.15     | 0.55 / 1.15                        |

The p90 of the last column is set by the turns that still end at 0.70 s (6 of 39 in the earlier run), and by the three turns whose reply failed a screen and kept their recorded time. Change 1 applies to turns whose screen passed in the record. A turn that failed keeps its measured time.

**What the projection cannot see.** It assumes the first 12 frames plus 0.14 s of lookahead are enough to start playback. It does not account for how the client sounds when it starts on 0.96 s of audio, for any slowdown of generation while the screen runs, or for the retry path. The generation speed leaves headroom, since the model outruns playback by about 7 times (measured), and in the live call a turn that ran next to other work still managed 3.7 times (measured, turn 5: 8.4 s of speech in 2.3 s).

## 4. Experiments to run first

Each follows `briefs/COMMON.md`. The GPU runs go through the GPU lease and `quiet_run` (local wrappers outside the repo), one configuration per run, and each states its one-line difference and what it expects to learn.

### Experiment 1. Does text lead audio, and does a prefix screen agree with the whole-reply screen?

This decides whether change 1 is safe to build. It has a CPU part and a short GPU part.

**Part A, CPU only, existing records.**

- Difference from any earlier run. None, it reads records.
- Expects to learn. How many replies a prefix screen would cut, and whether any audio of an offending word would already have been released.
- Method. For every first reply in the four record sets (about 145 replies), walk `response.output_text.delta` after `response.create`. After each delta, run `refused(prefix, sources, running)` from `voice/prototype/run.py` with the same `sources` the turn used. Record the first delta that fires and compare it with `refused(full text)`. For each firing, count the audio frames the model had already emitted by that delta's time, and how many a one-block lookahead gate would have released.
- Also feed the three failing replies on record (`ablation/demo-runC`, `demo-facts`, `demo-subject`, which hold the invented "Sunday"), and the replies the live call's turns 2, 5 and 22 had refused (measured, `fact_check` events in those records).
- Pass. Every known failure fires before the audio of its offending block is released. On replies whose full text passes, the prefix screen never fires. Zero exceptions, because the known failures must fail first (`COMMON.md`, evaluation rule 1).
- Stop. If any known failure would have been spoken, experiment 2 does not start, and the lookahead has to grow.

**Part B, GPU, about 6 minutes.**

- Difference. Saved reply audio goes into the LFM3 transcription session, paced in real time.
- Expects to learn. The lead, in words and in blocks, of the model's text over the audio it plays.
- Method. Take 10 reply WAVs from replay C (24 kHz, set the session's `audio.input.format.rate` to 24000). Stream each at real-time pace to a `?intent=transcription` session. At each 0.96 s boundary, count the words the ASR has produced and the words in the model's own text up to the matching 6-token block. The lead is text words minus ASR words.
- Pass. The lead is at least 0 at every boundary and its maximum, in blocks, is the lookahead L that change 1 must use. A negative lead means text can trail audio, and change 1 as designed is unsafe.

### Experiment 2. Streamed release against held release, 37 clips, `--eou audio`

- Difference. A flag that releases audio once the text so far passes the screens, with one block of lookahead, against the held reply. Both runs use `--eou audio`, the same 37 clips, `--decider liquid`, `--mock-tools`, the same worktree.
- Expects to learn. Whether speech end to first audio sent falls from a median of 1.58 s to about 0.7 s (guess, from section 3), whether any ungrounded fact, third-person line or machinery word is heard, and whether playback underruns.
- Method. Run `python -m voice.live.server ... --eou audio` with and without the flag, driven by `$VOICE_HOME/evidence/eou-audio/run_replay.py`. Score with `score.py` and the fact scorer `scan_facts.py`. Read `live.speech_end_to_first_audio_sent_s` from the server records, `speech_end_to_first_audio_rx_s` and `underruns` from `client.jsonl`.
- Read-out. The median and p90 of both, the count of turns cut mid-reply, the count of ungrounded facts heard (must equal the held run's, which `scan_facts.py` counts first), and a fit of `gain = 0.14 x (speech_s - 2)` against each turn's own reply length. The fit matters because reply length is sampled. Replays A and B differed by 0.30 s with identical settings.
- Listen. Mason or the PM hears 10 replies from the streamed run with audio, including every turn that was cut and the three known-failure turns (`COMMON.md`, "A metric is only trusted after a human has read or heard real outputs").
- Stop. Any heard ungrounded fact, or any underrun in more than 1 of 37 turns.

### Experiment 3. Starting at turn end, and what a cut turn leaves in the call

Two small questions that change what changes 2 and 3 cost. About 10 minutes of GPU.

**Part A, start at turn end.**

- Difference. `response.create` goes out immediately after `input_audio_buffer.commit`, with no wait for the final transcript or the decider.
- Expects to learn. The prefill cost after a turn ends, as a function of the turn's length.
- Method. On a fresh llama-server with the call's slots, send saved turn audio of 2, 4, 8 and 12 s, sent in 1 s chunks as `send_voice` does, then `commit` and `response.create`. Take the time to the first text delta and to the first audio frame, 5 repeats per length. Each repeat starts a fresh session, so the context does not drift.
- Pass for change 3. The first text delta arrives within 0.15 s of `response.create` for turns up to 8 s. If prefill grows near 0.1 s per second of audio, the 0.10 to 0.21 s gain does not hold on long turns.

**Part B, what a cut turn leaves.**

- Difference. A forced mid-thought cut. Replay clips 4, 6 and 10, which `--eou audio` cut in the earlier run, with the continuation arriving 0.5 s after the cut.
- Expects to learn. Whether the cut turn's audio stays in the call's voice session and gets heard a second time when the next turn carries it.
- Method. Sum the bytes of `input_audio_buffer.append` sent to the voice session across the cut turn and its continuation, from `events.voice_sent` (the cancelled turn's record does not keep them, so read the llama-server log or add a counter on `MicTurn.send_voice`). Compare with the person's audio in the two clips.
- Pass. Bytes sent equal audio heard, within 5%. A larger total means the call heard the first part twice, which `live-call3-fix.md` fixed for the older path, and `--eou audio` would reopen it.

### After the three, before anything is called done

Run 10 real turns on the laptop client with the flags on. Mason hears them. The laptop's `client.jsonl` gives `speech_end_to_first_audio_rx_s`, which is the one number this machine cannot read, and it is the number that includes the tailnet and the speaker (`COMMON.md`, real audio decides acceptance). Ask for that log from the 15:26 call too. The difference between it and the server's 1.62 s is the unmeasured delivery cost.

## What these numbers cannot see

- One speaker, one afternoon, 37 turns, one run per replay configuration. The two silence replays differ by 0.30 s at the median.
- Replays use mock tools, loopback networking and a mostly cached decider. The live call used real lookups but an older persona and snapshot mode.
- The "gain" figures replace the recorded send time with the time of the 12th audio frame plus 0.14 s. They do not run the screen, and they do not model a retried reply. Experiment 1 and 2 test those two things.
- The text-leads-audio assumption is read from the model's interleaved format and one trace, not from an alignment of audio to words.
- Literature figures are the authors' own, measured on their hardware and with their own definitions of latency. Moshi, LLaMA-Omni and Freeze-Omni measure from the end of speech with an ideal endpoint. Production figures are vendor claims, and Retell's independent number came through a search result I did not open.
- I did not read the Kyutai TTS page itself (the fetch returned nothing), the OpenAI announcement page (HTTP 403), or the Qwen3Guard-Stream latency plot, which the paper gives without numbers.

## Open questions

- How often do real calls need a task note decided only at the final transcript? The replays say 2 of 37 turns, with mock tools. It matters for change 3.
- What does a streamed reply sound like when it is cut mid-sentence? Experiment 2's listening step answers it.
- Does the tailnet add anything on a path other than the LAN? Run `tailscale ping` against the laptop from a non-LAN position and record whether it goes direct or through a relay.

## Reproduce

```
python $VOICE_HOME/evidence/latency-research/budget.py     # the stage table, per set
python $VOICE_HOME/evidence/latency-research/whatif.py     # first-block and first-sentence gains
```

Both read the four record sets named above and print median, p90, minimum and maximum per stage. They need only the Python standard library and NumPy. No GPU, no network, no audio is used.

## Sources

Papers

- [Moshi](https://arxiv.org/abs/2410.00037), [LLaMA-Omni](https://arxiv.org/abs/2409.06666), [Freeze-Omni](https://arxiv.org/abs/2411.00774), [VITA-Audio](https://arxiv.org/abs/2505.03739), [Mini-Omni](https://arxiv.org/abs/2408.16725) (read as an abstract only)
- [Full-Duplex-Bench](https://arxiv.org/abs/2503.04721)
- [TurnGPT](https://arxiv.org/abs/2010.10874), [Voice Activity Projection](https://arxiv.org/abs/2205.09812), [real-time VAP](https://arxiv.org/abs/2401.04868), [Predictive EOU](https://arxiv.org/abs/2409.19990), [endpoint anticipation](https://arxiv.org/abs/2606.13450), [RelayS2S](https://arxiv.org/abs/2603.23346)
- [Delayed Streams Modeling](https://arxiv.org/abs/2509.08753), [CosyVoice 2](https://arxiv.org/abs/2412.10117) (project-page claim, abstract only)
- [Qwen3Guard](https://arxiv.org/abs/2510.14276), [Jacoby et al., human latency and fillers](https://arxiv.org/abs/2404.16053), [Stivers et al. 2009](https://pubmed.ncbi.nlm.nih.gov/19553212/)

Production systems and documentation

- OpenAI: [Hello GPT-4o](https://openai.com/index/hello-gpt-4o/), [Realtime VAD guide](https://developers.openai.com/api/docs/guides/realtime-vad), [Agents SDK realtime guide](https://openai.github.io/openai-agents-python/realtime/guide/), [issue 1912](https://github.com/openai/openai-agents-python/issues/1912)
- Google: [Gemini Live capabilities](https://aistudio.google.com/docs/live-api/capabilities)
- LiveKit: [turn tuning and preemptive generation](https://docs.livekit.io/agents/logic/turns/tuning.md), [turn detector v1.0](https://livekit.com/blog/solving-end-of-turn-detection), [agent latency guide](https://livekit.com/blog/understand-and-improve-agent-latency)
- Pipecat and Daily: [speech input](https://docs.pipecat.ai/pipecat/learn/speech-input), [Smart Turn v3](https://www.daily.co/blog/announcing-smart-turn-v3-with-cpu-inference-in-just-12ms/)
- Deepgram: [eager end of turn](https://developers.deepgram.com/docs/flux/voice-agent-eager-eot)
- Vapi: [understanding latency](https://docs.vapi.ai/assistants/model-intelligence/understanding-latency)
- Retell: [how real-time voice AI works](https://www.retellai.com/blog/how-real-time-voice-ai-works-stt-llm-tts)
- NVIDIA: [NeMo Guardrails output rail streaming](https://docs.nvidia.com/nemo/guardrails/latest/configure-guardrails/yaml-schema/streaming/output-rail-streaming)
- llama.cpp: [server README](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md), [LFM3-Audio-2B-GGUF card](https://huggingface.co/LiquidAI/LFM3-Audio-2B-GGUF)

Our own work: `stage1-speech-path.md`, `live-call3-fix.md`, `live-eou-audio.md`, `audio-decider.md`, `asr-tick.md`, `barge-in.md`, `live-fact-check.md`, `prompt-prewrite.md`, `live-snapshot-placement.md`, `eou-handoff.md`, `liquid-decider.md`, `prototype.md`, `live-demo.md`, `voice/live/server.py`, `voice/live/endpoint.py`, `voice/live/client.py`, `voice/prototype/run.py`, and `$VOICE_HOME/research/liquidcodec/REPORT.md`.
