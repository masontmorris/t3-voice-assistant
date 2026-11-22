# Would a decision model on audio beat the text decider?

**Short answer.** Audio helps in one place, end-of-turn detection, and only because the live transcript lags the voice by about 0.7 s. It does not help classify requests, and it is a weak, unproven help for backchannels. The hosted d1.2 is too slow and too variable for the hot path. The local d1-Omni-2B is fast enough (40 ms median) at +2.7 GB of VRAM. The local 600M does not work on audio.

Every number below is **measured** on this machine unless it is marked **inferred** or **guess**. The probe is throwaway code in `voice/research/audio_probe/`. Raw results, clips and ledgers are under `$VOICE_HOME/research/audio-decider/` and are not committed.

## Goal and smallest change

Mason wants an assistant that understands what he says and when, quickly and reliably. Today every decision is made on text from the live ASR. The question is whether a decider that also hears the audio would catch mistakes the text path makes. The smallest change that could meet it is one extra call at the end of speech that replaces the fixed 0.7 s silence wait. Nothing else in the pipeline needs to change.

## A trap in Liquid's hosted API

The hosted d1.2 documents audio in a top-level `media` array. The API ignores it. It returns 200, bills no audio tokens, and answers as if the clip were silence (measured: the same 35 input tokens with 0, 1, 5 and 20 s of audio, and with malformed `media` entries). Audio works in a top-level `audio` field holding data-URI WAVs, and billing then matches the documented 10 tokens per second (measured: 135 tokens for 10 s). The first audio runs here carried no audio and gave chance-level results, which is how it was found. Any other client that sends `media` is testing a text-only model.

## Evidence

- Mason's real turns from the 2026-11-16 15:26 call (37 turns) and three from the 08:03 call. Turns 8 and 9 are the cut mid-sentence turns that were joined into 10.
- Barge-in clips: three real first-word clips, nine dismissed overlaps, and 13 synthetic TTS clips from the barge-in work.
- 70 live-gate utterances read by a Windows TTS voice, clean and with white noise at 5 dB SNR (synthetic, one assumed noise level).
- 40 synthetic long backchannels and interruptions (Windows TTS, two voices), because Mason's recordings hold no long "mm-hm" over a playing reply.
- Models: hosted `d1.2`, local `d1-Omni-2B` Q8_0, local `d1-omni-600M` Q8_0 (llama.cpp b12428, `--mmproj`, `-ub 2048`). Each local file matched Hugging Face's SHA-256.
- The first prompt written for each question was used. No prompt was tuned on audio.

## (a) End of turn: is he done, or pausing mid-thought?

Test: each turn cut at its speech end plus T of silence (finished), and each internal pause cut at its start plus T (continues). 38 finished turns, 15 internal pauses, three of them 0.7 s or longer. Those three are the pause after the opening "Uhm," and the real cuts that ended turns 8 and 9.

The text baseline sees what the live ASR had emitted at the cut. That comes from a real-time paced run, so a partial that arrived at time t used only audio up to t.

| Decider | T=150 ms | T=300 ms | T=500 ms | All |
|---|---|---|---|---|
| d1.2 audio | 0.945 [0.82, 1.00] | 0.942 [0.81, 1.00] | 0.995 [0.97, 1.00] | 0.955 [0.89, 1.00] |
| d1-Omni-2B local, audio | 0.923 | 0.921 | 0.981 | 0.932 [0.86, 0.98] |
| d1-omni-600M local, audio | 0.530 | 0.568 | 0.638 | 0.585 (unusable) |
| d1.2 text on the live transcript | 0.435 | 0.462 | 0.579 | 0.483 [0.36, 0.60] |
| d1-Omni-2B text on the live transcript | 0.625 | 0.628 | 0.707 | 0.637 |
| d1.2 audio plus the live transcript | 0.616 | 0.715 | 0.743 | 0.690 |
| d1.2 text on the *final* transcript (not causal) | | | | 0.966 [0.93, 0.99] |

Values are AUC with a bootstrap 95% interval over items.

**Reading it.**
- At the moment of the decision the text path is blind. The live ASR delivers words in about 1 s batches. The last word lands a median 0.69 s after speech ends (p90 0.82 s, max 0.90 s, 38 turns, paced, run under `quiet_run`). At T=150 ms the transcript held 63% of the final words on average and was complete in 0 of 38 turns. That is why the 0.7 s rule exists.
- Given the complete transcript the text decider is as good as audio (0.966). So audio adds timeliness, not understanding. Whether it adds prosody beyond the words is **not shown**: audio got "Oh, did you?" right where text was fooled, and got "Wow, that's pretty good." wrong at 0.88.
- Mixing the stale transcript into the audio call makes it worse (0.690 against 0.955). Send audio alone.
- Length is not a shortcut. Cut length alone scores AUC 0.31 to 0.41 on these items. The last 3 s of audio alone keeps 0.950.
- Turns 8 and 9 would not have been cut. At 700 ms of silence the end of turn 8, the end of turn 9 and the opening pause scored P(done) 0.14 / 0.06 / 0.24 on d1.2 and 0.18 / 0.03 / 0.41 on the local 2B, all below 0.5.

**Policy replay.** Check at 150, 300 and 500 ms of silence, fire at the first check with P(done) at or above theta, otherwise fire at 700 ms. Theta is a grid, not a tuned value.

| theta | d1.2: turns fired early / mean saved | local 2B: turns fired early / mean saved | mid-thought pauses cut (policy) | cut by today's 700 ms rule |
|---|---|---|---|---|
| 0.5 | 37 of 38 / 501 ms | 27 of 38 / 391 ms | 1 of 15 | 3 of 15 |
| 0.7 | 26 / 350 ms | 15 / 213 ms | 1 of 15 | 3 of 15 |
| 0.9 | 8 / 108 ms | 2 / 29 ms | 0 of 15 | 3 of 15 |

The one false cut is "Wow, that's pretty good." followed by a 0.32 s pause. It is a complete sentence, and the turn went on. "Mean saved" is silence time only. The call itself costs more: about 280 ms hosted and 40 ms local. **Net saving at theta 0.5, inferred:** about 0.2 s hosted and about 0.35 s local, per finished turn.

## (b) Backchannel against interruption

Test: the first 0.6, 1.0 and 1.5 s of speech after onset. The labels need care.
- "Interrupt" for Mason's 36 turn openings is **by construction**. They were ordinary turns, not interruptions of a playing reply.
- Many start with "Okay" or "Uhm", which is genuinely ambiguous at 0.6 s.
- Real backchannels are few: two "Okay" turns, three first words, nine dismissed overlaps. The long-backchannel case that fails today exists only as synthetic speech.

Share of each group that the audio decider would let the reply keep playing, with the shipped rule for comparison (stop at 600 ms voiced, energy VAD here, so approximate):

| Group | n | rule | d1.2 th 0.5 | d1.2 th 0.7 | 2B th 0.5 | 2B th 0.7 |
|---|---|---|---|---|---|---|
| Real backchannels (should be kept) | 18 | 0.39 | 0.94 | 0.50 | 0.89 | 0.50 |
| Long synthetic backchannels (kept) | 60 | 0.37 | 0.97 | 0.60 | 0.87 | 0.20 |
| Real turn openings at 0.6 s (should stop) | 36 | n/a | 0.33 | 0.06 | 0.14 | 0.03 |
| Real turn openings at 1.0 s | 36 | 0.03 | 0.17 | 0.00 | 0.19 | 0.00 |
| Real turn openings at 1.5 s | 36 | 0.00 | 0.08 | 0.00 | 0.19 | 0.00 |
| Synthetic interruptions | 65 | 0.25 | 0.00 | 0.00 | 0.09 | 0.00 |

- The rule always stops on a backchannel that lasts 600 ms or more. Audio can keep playing through one. That is the win, and it is proven only on synthetic speech, where the model may be reading words rather than tone.
- Audio costs a trade. At theta 0.5, d1.2 lets the reply continue through 33% of real turn openings at 0.6 s. A person who starts "Okay, and what about..." would be talked over. At theta 0.7 that falls to 6% but only half the real backchannels are kept.
- Text cannot compete at 0.6 s. The live transcript was empty for 36 of 36 openings. By 1.0 s it was empty for 5 of 36.
- AUC on Mason's real clips: d1.2 audio 0.952, 2B audio 0.949, d1.2 text 0.807, 2B text 0.747. The 600M is inverted (0.219).

## (c) Request, conversation or correction, with wrong or late ASR

Hand labels for 35 real turns (the 3 ambiguous ones are dropped).

| | accuracy | request AUC |
|---|---|---|
| d1.2 audio | 0.800 [0.66, 0.91] | 0.905 |
| d1.2 text (the real ASR transcript) | 0.800 [0.66, 0.91] | 0.962 |
| d1.2 both | 0.771 | 0.917 |
| 2B audio / 2B text | 0.829 / 0.800 | 0.951 / 0.924 |

70 synthetic gate items: d1.2 accuracy 0.857 on true text, 0.843 on clean ASR, 0.871 on audio. With noise: 0.843 on ASR text, 0.829 on audio. LFM3 made 20 transcript errors on the 70 noisy clips (for example "Boulder" to "folder", "Delete" to "leave"). The kind answer survived them. The 2B scored 0.69 to 0.74 on every route. **Audio does not beat text here.** The errors are the same questions on both routes (chat questions read as requests), so they belong to the criteria, not the modality. Audio also cannot fix a wrong slot value such as the city name, because it returns no transcript.

## (d) Deciding before the transcript arrives

- The first partial arrives a median 0.74 s after voice onset (p90 1.64 s, 38 turns).
- Kind from the first 1, 2 and 3 s of speech, audio against the live partial at that moment: accuracy 0.71 against 0.75, 0.60 against 0.60, 0.62 against 0.69 (n = 28, 20, 13). Neither route is reliable, and there is no audio advantage in numbers this small.
- The usable form of (d) is end-of-turn and barge-in, which are above. Early dispatch is **not supported** by this evidence.

## Cost, latency and memory

| | d1.2 hosted | d1-Omni-2B local | d1-omni-600M local |
|---|---|---|---|
| Audio request p50 / p90 | 281 / 455 ms (eot), 190 / 298 ms (barge-in) | 40 / 73 ms | 47 / 70 ms |
| Text request p50 | 121 ms (this machine, network) | 23 ms | 13 ms |
| Cost per call | about 3.3e-6 USD | 0 | 0 |
| VRAM, delta over a 777 MiB baseline | none | +2,659 MiB after load, +2,710 after the run | +1,035 / +1,096 MiB |
| Quality on (a) and (b) | good | good | fails |

- **Hosted latency scales with the upload.** p50 is 222 ms for clips under 2 s and 388 ms for 8 to 16 s. A 5 s clip is about 213 KB of base64. Sending only the last 3 s trims p50 from 281 to 243 ms at no accuracy cost.
- **Hosted cost is not a factor.** Audio is 10 tokens per second at 0.02 USD per million. All 2,682 hosted calls in this study cost 0.0084 USD.
- **Hosted answers are not deterministic.** The same audio gave P(done) with a standard deviation of 0.027 across five repeats (ranges up to 0.14). Use a margin around any threshold.
- **Hosted depends on the network and sends Mason's voice to a third party.** Total Liquid spend: 0.0084 USD.
- **VRAM.** The published GGUF is Q8_0 only (1.85 GB model plus 0.33 GB projector). The earlier note that LFM3 plus the local d1 took 4.5 GB used the Q4_K_M d1-2B. Swapping in the Q8 Omni adds about 1 GB to that, **inferred** from file sizes. LFM3's own footprint was not re-measured in this run. A Q4 quantization of Omni should be possible with `llama-quantize` (untested).

## Recommendation

1. **Use local d1-Omni-2B audio for end-of-turn.** It would replace the fixed 0.7 s silence wait with an adaptive one: ask at 150, 300 and 500 ms of silence, commit at P(done) of 0.5 or more, and keep 0.7 s as the fallback. It would also let the gate hold on a low P(done), where today's rule cuts the turn. The signal for that hold is in the data (all three real mid-thought pauses scored under 0.42), but a longer hold was not tested. It replaces the silence timer, not the text decider.
2. **Do not use audio for request, correction or conversation.** It ties text on real turns and on noisy synthetic ones.
3. **Barge-in: prototype it behind a flag, second stage only.** Stop on the voiced gate as now, then let audio veto the stop only at P(backchannel) of 0.7 or more. First record 10 to 20 real long "mm-hm"s over a reply. The current evidence for the main benefit is synthetic.
4. **Do not put hosted d1.2 audio in the hot path.** Use it as an offline labeler for end-of-turn and barge-in sets.
5. **Drop the 600M for audio.**
6. **Re-run (a) when the ASR tick fix lands.** The audio advantage in (a) is the transcript lag. If the other worker's chunk change shrinks the 0.69 s tail, text recovers toward 0.966 and the case for audio shrinks. `python -m voice.research.audio_probe.asr_run`, then `liveview`, then the `run_api` runners redo it.
7. **Tell the liquid-decider worker about the `media` trap** if their client ever sends audio.

## End of turn in both directions: holding a turn open, and Smart Turn on the CPU

**Short answer.** A model can also hold a turn open past 0.7 s when it hears a mid-thought pause, and that removes cut-offs. Ending turns early adds them. Smart Turn v3.2 on the CPU is the better holder: no VRAM and about 40 ms a check. Nothing measured both ends turns as early as `--eou audio` (median wait 0.2 s) and cuts people off no more than today's silence rule does, so the default stays `--eou silence`. `--eou smart` holds only, and is one flag away.

Every number below is **measured** on this machine unless it is marked **inferred**.

### The mechanism

`Endpointer` takes `hold_ms`. A listener that is sure the person is pausing calls `hold()` before 0.7 s of silence, and the turn then waits for `hold_ms` of silence instead. `check_ms` points past 0.7 s still reach the listener, which can end a held turn on one. Speech, an early end and the end of the turn clear the hold. `Policy` carries `theta` (a P(done) at or above it ends the turn), `hold_theta` (at or below it holds) and `hold_ms`. A missing, failed, slow (past the check's deadline) or in-between answer, and an answer about a silence that has since ended, leave the 0.7 s rule exactly as it is. Nothing reads the transcript.

Smart Turn is Pipecat's open end-of-turn model, v3.2 (BSD-2-Clause): a Whisper-tiny encoder, int8 ONNX, 8.7 MB. It reads the last 8 s of the turn's audio, zero-padded in front, as Whisper log-mel features. The weights are `$VOICE_HOME/smart-turn/smart-turn-v3.2-cpu.onnx`, from `pipecat-ai/smart-turn-v3`. The one new Python dependency is `onnxruntime` 1.32.0, a 12 MB CPU wheel that brings `flatbuffers` and `protobuf`. Without the weights or the package the server says so once and keeps the silence rule.

### How it was measured

`voice/research/live/eou_hold_eval.py` feeds whole utterances through the real `Endpointer` and `Policy`, asks the model at each `Check` as the server does, and applies each answer when it would have arrived. A cut-off is a turn that ended while the person still had speech to come. Wait is the time from the true end of speech, by the endpointer's own voicing rule, to the end of the turn.

| Set | Utterances | True end known by |
|---|---|---|
| Real call | 35 | the recorded turn inputs: 37, less two that are prefixes of a third |
| Hesitant requests | 20 | the gate's hesitant caller lines, rendered whole by the server's TTS |
| Hesitant requests with a pause | 20 | the same lines cut after their first two comma-separated pieces and joined by 0.9, 1.2, 1.6 and 2.0 s of silence |
| Short finished lines | 27 | the gate's yeses and questions, rendered by the same TTS |

The three synthetic sets are TTS, with its own pause habits. They show the direction of each effect, not its size on a person.

### Comparison

Cut-offs count mid-utterance turn ends. Waits are median / p90 in seconds.

| Configuration | Real call: wait, cut-offs | Hesitant requests: cut-offs | With a pause: cut-offs | Short lines: wait, cut-offs | Finished turns held over 1 s (of 62) | VRAM | Time per check |
|---|---|---|---|---|---|---|---|
| Plain silence, 0.7 s | 0.70 / 0.70, 2 | 2 | 24 | 0.70 / 0.70, 0 | 0 | 0 | 0 |
| d1-Omni-2B, shorten only (`--eou audio`) | 0.20 / 0.70, 5 | 13 | 32 | 0.20 / 0.48, 3 | 0 | 2.7 GB | 60 ms, GPU |
| d1-Omni-2B, both directions | 0.22 / 0.70, 1 | 12 | 30 | 0.20 / 0.70, 3 | 0 | 2.7 GB | 60 ms, GPU |
| Smart Turn, shorten only | 0.24 / 0.70, 9 | 16 | 29 | 0.24 / 0.47, 2 | 0 | 0 | 36 ms, 74 ms of CPU |
| Smart Turn, both directions | 0.58 / 0.70, 0 | 2 | 13 | 0.58 / 0.63, 0 | 3 | 0 | 36 ms, 74 ms of CPU |
| **Smart Turn, hold only (`--eou smart`)** | 0.70 / 0.70, 0 | 0 | 12 | 0.70 / 0.70, 0 | 3 | 0 | 36 ms, 74 ms of CPU |
| Both models agree, both directions | 0.20 / 3.00, 1 | 8 | 25 | 0.20 / 0.65, 4 | 7 | 2.7 GB | both |

How each row was chosen. The two shorten-only rows are fixed: ask at 150, 300 and 500 ms, end at 0.5. The d1 and both-models rows with a hold are the policy with the fewest cut-offs, found by a grid search over check points, `theta`, `hold_theta` and `hold_ms` (about 2,000 policies a model), among those that keep the real call at a median of 0.22 s or less and 3 cut-offs or fewer. Smart Turn "both directions" ends at 0.9 and holds at 0.05, picked by hand from the same grid. Smart Turn hold only is the shipped preset: ask at 500, 600, 900, 1200 and 1600 ms, hold at 0.05 or less, cap the hold at 2 s. The grid was searched on the sets it is scored on, so the tuned rows are optimistic.

The charter's bar is the current `--eou audio` on the real call: a median wait of 0.22 s and 3 cut-offs. On the live replay of the 37 recorded clips, re-run today, the two existing configurations and `--eou smart` gave:

| Live replay of the 37 clips | Turns | Mid-thought cut-offs | Silence from speech end to turn end, median / p90 | First reply audio after speech end, median / p90 | VRAM during the call |
|---|---|---|---|---|---|
| `--eou silence` | 37 | 0 | 0.70 / 0.70 s | 1.07 / 1.27 s | 3.52 to 3.56 GB |
| `--eou audio` | 39 | 3, the same three clips as before | 0.28 / 0.70 s | 0.53 / 1.25 s | 6.18 to 6.25 GB |
| `--eou smart` | 37 | 0 | 0.70 / 2.00 s | 1.08 / 2.28 s | 3.52 to 3.56 GB |

The offline replay and the live one count differently: `--eou audio` has 3 cut-offs live and 5 offline, and the plain rule 0 live and 2 offline. The two extra are the recorded call's two pauses of 0.9 s, which every rule that ends turns at 0.7 s cuts and which the live scorer does not count (**inferred**). Under `--eou smart`, 5 of the 37 turns waited the full 2 s: two end where the person was mid-thought in the recording, and three are ends the model calls unfinished.

### What the numbers say

- **Ending early and holding pull against each other.** Every policy with a median wait of 0.3 s or less cuts people off more than the silence rule does on the hesitant sets. A pause after a sentence, a comma or "uhm" looks finished to both models for the first 150 to 600 ms, and a policy that ends the turn on it has cut the person off before a hold can matter. `--eou audio` more than doubles the cut-offs on the hesitant requests: 13 against 2.
- **Smart Turn separates a mid-thought pause from an end better than d1.** On the real call's pauses of 0.7 s or more its P(done) never rose above 0.02, and d1's ranged from 0.1 to 0.4. On the pause-joined set its median P(done) at those pauses was 0.01 and d1's about 0.3. Its misses go the other way: it calls 3 of 62 finished turns unfinished and holds them to the cap.
- **No configuration meets the bar.** The bar needs a median of 0.22 s or less and 3 real-call cut-offs or fewer, and no more cut-offs on the hesitant sets than the best alternative. Smart Turn reaches 0.24 s only with 6 or more real-call cut-offs. d1 reaches the median with 12 and 30 cut-offs on the two hesitant sets, against 0 and 12 for hold-only Smart Turn. The default stays `--eou silence`.
- **What `--eou smart` is for.** It never ends a turn before 0.7 s, so on the same audio it cuts no more than the silence rule: 24 cut-offs on the pause-joined set become 12, the real call's 2 become 0, the hesitant requests' 2 become 0. The price is a wait of up to 2 s on about 5% of finished turns (3 of 62 offline), a mean of 0.06 s over all of them. Ending early at 0.9 as well gains 0.12 s at the median and, in the whole-call gate, left two sessions with an extra turn.
- **The cap.** Mean extra wait over finished turns is 0.02 s at a 1.2 s cap, 0.06 s at 2 s and 0.11 s at 3 s. Cut-offs on the pause-joined set fall from 21 at 1.2 s to 13 at 2 s and 10 at 2.5 s. 2 s is where the cut-offs stop falling quickly; it covers a pause up to 1.3 s past the silence rule.
- **Check points.** 500 and 600 ms, so the answer is in before 0.7 s. The checks at 900, 1200 and 1600 ms let a held turn end early when the model changes its mind. A hold threshold of 0.05 held 3 of 62 finished turns, 0.1 held 5 and 0.2 held 9.
- **Cost.** Smart Turn takes 36 ms a check (p90 42 ms) on two CPU threads when the machine is idle, and 40 ms (p90 114 ms, worst 129 ms, 90 checks) in the live replay with LFM3 running. Its CPU time is 74 ms a check on two threads and 58 ms on one. The feature extractor runs on torch, whose thread pool spun every core for 380 ms of CPU a check until `torch.set_num_threads(1)`. d1-Omni-2B adds 2,655 MiB at load and 2,691 MiB after a run.

### The whole-call gate

`short-yes`, `news-done`, `short-yes-tell` and `speak-over`, three runs each, on `voice/main` with this change, with `VOICE_EOU` naming the mode and everything else equal:

| | `--eou silence` | `--eou smart` (hold only) | `--eou smart`, also ending at 0.9 |
|---|---|---|---|
| `speak-over` | 3 of 3 | 3 of 3 | 3 of 3 |
| `short-yes` | 3 of 3, marked check passed in all | 3 of 3, marked check failed in 1 run | 3 of 3, marked check failed in 2 runs |
| `news-done` | 3 of 3, marked check passed in all | 2 of 3, in one run a yes started no thread | 3 of 3 |
| `short-yes-tell` | 3 of 3, marked check passed in all | 3 of 3 | 3 of 3 |
| Sessions with an extra turn with no transcript | 0 of 8 | 0 of 8 | 2 of 8 |
| Turns that attempted a fallback | 0 | 0 | 0 |
| Audio delay, median per scenario | 1.87 to 2.1 s | 1.69 to 2.34 s | 1.51 to 2.1 s |

The gate renders every caller line afresh, so two gate runs never hear the same audio, and a policy that only holds cannot end a turn before the silence rule does. The failures under hold-only are the known yes fault, which varies between runs of the silence rule too. The offline replay is the same-audio comparison. The gate shows that the call still works with the model in it.

### What is unproven

- The hesitation sets are TTS. Nothing here measures how long a person pauses mid-thought. The only real long pauses are the recorded call's two, at 0.9 s.
- The pause-joined set uses four gap lengths chosen by hand, so the cut-offs that remain with a 2 s cap are largely those gaps.
- Smart Turn was trained on short, trimmed clips. How it hears room noise and echo through the browser or a phone is not measured.
- The grid search used the data it reports. A held-out call would move the tuned rows.
- Meeting the bar needs a model that says "finished" at 150 to 300 ms and does not say it about a pause a person continues after. Neither model does. What would be needed is a set of pauses labelled with whether the person went on, from real calls, to tune and score on.

## What these numbers cannot see

- One speaker, one microphone, one afternoon. Nothing was tuned on a held-out set, and theta is a grid read off the same data.
- Only 15 internal pauses and three real mid-thought cuts. The intervals above are wide and the pause counts are small.
- The kind labels are mine, from the transcripts. They are subjective, and a different labeler would move a few items.
- The backchannel "interrupt" labels are by construction, and the long backchannels are synthetic speech with clear words.
- The VAD is an energy detector, not the shipped one. The "rule" column is an approximation.
- The 70-item set uses one Windows voice and one noise level. Real ASR errors on real calls were not scored, since no reference transcripts exist.
- The local runs share one request in flight. Concurrent LFM3 load was not measured.
- Mason's live-call audio was sent to Liquid's hosted API for this study, as the brief asked. `audio/real/` recordings were not.

## Reproduce

All commands run from the repo root. The key is read in-process from the main checkout's `.env`.

```
python -m voice.research.audio_probe.build_sets       # clips from the live calls
python -m voice.research.audio_probe.make_tts         # 70 synthetic items, clean and noisy
python -m voice.research.audio_probe.make_bc_tts      # long backchannels and interruptions
python -m voice.research.audio_probe.asr_run          # GPU lease: LFM3 ASR, paced timing
python -m voice.research.audio_probe.liveview         # what the transcript held at each cut
python -m voice.research.audio_probe.run_api eot audio
python -m voice.research.audio_probe.job2             # GPU lease: local 2B and 600M, VRAM
python -m voice.research.audio_probe.summary
```
