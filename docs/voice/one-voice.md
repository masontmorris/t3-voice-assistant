# One voice for conversation and T3

Keep one speaker for the whole call and preserve the exact checked T3 words. None of the three routes qualified for the live server in this test. The cascade produced the closest speaker match, but it answered fewer requests than the interleaved baseline.

## Results

All rows are measured. The tests used the local LFM3-Audio GGUF, ten synthetic spoken requests, and [the twenty fixed T3 lines](../../voice/research/t3_voice/lines.txt). The speaker score is the median cosine similarity between call and T3 clips. The prompt and cascade each have 200 cross-group comparisons. The readback compares its nine accepted T3 clips against ten interleaved replies.

| Run and route | Speaker score | Conversational requests answered | Call first audio, median (range) | T3 WER | T3 lines with a name, status, or number mismatch | T3 first audio, median (range) |
| --- | ---: | ---: | --- | ---: | ---: | --- |
| 1. Interleaved baseline with #67 T3 references | 0.688 | 10/10 | 0.200 s (0.164 to 0.370) | 5.2% from #67 | 5/20 from #67 | 0.039 s (0.037 to 0.069) from #67 |
| 1. Interleaved with female speaker instruction | 0.664 | 7/10 | 0.206 s (0.176 to 0.346) | Same #67 audio | Same #67 audio | Same #67 audio |
| 2. Interleaved baseline with fresh T3 references | 0.674 | 10/10 | 0.226 s (0.192 to 0.376) | 4.8% | 6/20 | 0.039 s (0.035 to 0.053) |
| 2. Text-only, synthesize the whole reply | 0.850 | 6/10 | 0.319 s (0.260 to 0.370) | Same run 2 T3 audio | Same run 2 T3 audio | Same run 2 T3 audio |
| 2. Text-only, start on the first clause | 0.864 | 6/10 | 0.290 s (0.230 to 0.400) | Same run 2 T3 audio | Same run 2 T3 audio | Same run 2 T3 audio |
| 3. Interleaved readback, check audio before release | 0.830 | Uses the interleaved baseline | Uses the interleaved baseline | 11.8% before the gate | 8/20 before the gate | 1.820 s (1.603 to 2.279), 9/20 eligible |

The WER and mismatch counts come from LFM3 live ASR. The recognizer sometimes merges words or picks homophones, including “VoiceSandbox” and “ad” for “Add.” Neither the speaker score nor the ASR transcript establishes what a person hears. The T3 line “all 42 tests pass” came back as “all four due tests pass” in run 2. Its spoken exactness remains unresolved.

The call timer starts at the end of paced input and stops at the first decoded PCM. It excludes the application's endpointer, reply screening, and playback. The T3 timer starts at the synthesis request. For run 3 it stops when the gate would release the clip. The cases alternate order within each run. Each configuration has one run, so the ranges show variation across prompts, and the data gives no estimate of run-to-run noise.

## Why each route stopped

- **Speaker instruction.** The GGUF has a dedicated TTS template that puts the language and speaker into the system prompt and starts audio generation. The realtime chat template inserts the conversation's system instructions. The server source builds “Speak in … using a … voice” for LFM3 speech settings and separately selects the generation mode. The explicit instruction in run 1 left the chat clips closer to baseline chat than to TTS, and three replies echoed or rephrased the request.
- **Cascade.** Both routes sent text to the same TTS endpoint and used its `female` speaker. The first-clause route reached the 0.86 speaker target and emitted audio before the last text event on 4/10 replies. The median paired delay against baseline was 42 ms, compared with 79 ms for the whole-reply route. The order of events identifies the wait for text or punctuation, but the run did not profile the server. The model repeated three requests and emitted tool markup on a greeting. The model card's text-only example uses assistant prefill, which the realtime endpoint's item creation does not accept. On the joke clip, the longest internal low-energy interval was 0.85 s for interleaved speech and 0.47 s for the first-clause cascade. The wording differs, so this is a listening cue rather than a controlled prosody comparison.
- **ASR before playback.** A fresh chat session generated the checked line, then the probe transcribed its entire waveform. Both text and transcript had to match after word and number normalization. This would withhold 11/20 lines. ASR itself added a median of 0.219 s (0.145 to 0.397). The accepted clips would play about 1.78 s later at the median than streamed TTS. Holding the whole audio before any release takes most of that time. On the stop request, the generated text matched and the recognizer heard “start.”

## Review the clips

The run logs, per-clip scores, and review notes are under `$VOICE_HOME/evidence/one-voice`. The `listening` folder has three diagnostic pairs from the first-clause cascade, each with a conversation reply followed by a T3 line. The pairs include the joke and the test count. The probe and review scripts are under `$VOICE_HOME/research/one-voice`.
