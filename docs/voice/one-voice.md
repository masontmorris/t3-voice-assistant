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

The call timer starts at the end of paced input and stops at the first decoded PCM. It excludes the application's endpointer, reply screening, and playback. The T3 timer starts at the synthesis request. For run 3 it stops when the gate would release the clip. Runs 1 and 2 rotate the starting mode across requests. Each configuration has one run, so the ranges show variation across prompts, and the data gives no estimate of run-to-run noise.

## Why each route stopped

- **Speaker instruction.** The GGUF has a dedicated TTS template that puts the language and speaker into the system prompt and starts audio generation. The realtime chat template inserts the conversation's system instructions. The server source builds “Speak in … using a … voice” for LFM3 speech settings and separately selects the generation mode. The explicit instruction in run 1 left the chat clips closer to baseline chat than to TTS, and three replies echoed or rephrased the request.
- **Cascade.** Both routes sent text to the same TTS endpoint and used its `female` speaker. The first-clause route reached the 0.86 speaker target and emitted audio before the last text event on 4/10 replies. The median paired delay against baseline was 42 ms, compared with 79 ms for the whole-reply route. The order of events identifies the wait for text or punctuation, but the run did not profile the server. The model repeated three requests and emitted tool markup on a greeting. The model card's text-only example uses assistant prefill, which the realtime endpoint's item creation does not accept. On the joke clip, the longest internal low-energy interval was 0.85 s for interleaved speech and 0.47 s for the first-clause cascade. The wording differs, so this is a listening cue rather than a controlled prosody comparison.
- **ASR before playback.** A fresh chat session generated the checked line, then the probe transcribed its entire waveform. Both text and transcript had to match after word and number normalization. This would withhold 11/20 lines. ASR itself added a median of 0.219 s (0.145 to 0.397). The accepted clips would play about 1.78 s later at the median than streamed TTS. Holding the whole audio before any release takes most of that time. On the stop request, the generated text matched and the recognizer heard “start.”

## Follow-up: assistant prefill for text-only replies

Measured on llama.cpp b12428, text-only generation accepts the model card's direct-answer prefill through a realtime chat template override. With the cascade persona, prefill answered 6/10 requests. Without a system prompt, it answered 7/10. Both runs removed the tool markup seen in the original cascade. Both fell short of the 10/10 gate.

The model card's direct-answer example ends the prompt with `<think></think>` and suppresses both audio and mixed generation. The GGUF's default generation header ends at `<|im_start|>assistant\n`, as measured by reading `tokenizer.chat_template` from the local Q4_K_M file. The GGUF supplies separate ASR and TTS templates. [LFM3-Audio model card](https://huggingface.co/LiquidAI/LFM3-Audio-2B/blob/cddb56b5ec9b4703f9b3b2d0500143d1fff2fee0/README.md#speech-to-text-chat-with-or-without-reasoning).

Source inspection shows that realtime item creation accepts user messages and tool outputs only. Realtime derives `assistant_header` from the active generation template and appends it before sampling. In text mode, the generation pipeline tokenizes that header with special tokens enabled and bans the audio and mixed start tokens. The probe used `--chat-template-file` to append the closed think block, and `/apply-template` confirmed the rendered suffix on the running server. [Item creation](https://github.com/ggml-org/llama.cpp/blob/b12428/tools/server/server-realtime.cpp#L461-L475), [header rendering](https://github.com/ggml-org/llama.cpp/blob/b12428/tools/server/server-realtime.cpp#L267-L320), [response prompt](https://github.com/ggml-org/llama.cpp/blob/b12428/tools/server/server-realtime.cpp#L558-L573), [text mode](https://github.com/ggml-org/llama.cpp/blob/b12428/tools/mtmd/mtmd-helper-gen.cpp#L1277-L1307), [template flag](https://github.com/ggml-org/llama.cpp/blob/b12428/common/arg.cpp#L3813-L3825).

The same build's chat completions source accepts `input_audio` with the audio projector and supports a continued final assistant message. The realtime runs kept the paced input and first-clause TTS path from the original comparison. HTTP chat completions remain unmeasured. [Audio input](https://github.com/ggml-org/llama.cpp/blob/b12428/tools/server/server-common.cpp#L1431-L1444), [assistant continuation](https://github.com/ggml-org/llama.cpp/blob/b12428/tools/server/server-common.cpp#L1592-L1613), [multimodal prompt processing](https://github.com/ggml-org/llama.cpp/blob/b12428/tools/server/server-context.cpp#L5275-L5283).

- Run 1 added only `<think></think>` to the realtime generation header. It kept the original cascade's persona and text-only instruction.
- Run 2 kept the header and removed the entire system message. The model received the same audio and the model card's direct-answer prefix.

All rows are measured on the same ten synthetic clips. Each new configuration ran once with temperature zero, the same GGUF and projector, and PR #70's `female` first-clause TTS. The archived baseline and cascade rows are from the original run 2.

| Route | Answered | Replies with tool markup | First decoded audio, median (range) |
| --- | ---: | ---: | ---: |
| Interleaved baseline | 10/10 | 0/10 | 0.226 s (0.192 to 0.376) |
| First-clause cascade | 6/10 | 1/10 | 0.290 s (0.230 to 0.400) |
| Prefill with system prompt | 6/10 | 0/10 | 0.289 s (0.225 to 0.380) |
| Prefill without system prompt | 7/10 | 0/10 | 0.348 s (0.267 to 1.002) |

The probe measured from the end of paced input to the first decoded PCM. Both prefill runs saved ten nonempty WAV files, ten text replies, and ten ASR transcripts with zero request errors. Run 2's numbered quiet-evening answer took 1.002 s to first audio. Its text finished at 0.924 s and the probe passed the whole reply to TTS. The first punctuation match was `1.`, which is too short for the reused clause splitter. That explains the whole-reply fallback for this turn, inferred from the splitter and saved text. The ranges show variation across requests. Run-to-run variation and the cause of the median difference remain unmeasured.

The same manual rule applies to every arm. A reply passes if it answers the request, preserves the given fact, and avoids parroting or tool markup. The review uses reply text and ASR transcripts. A verbatim greeting with changed punctuation counts as an echo. Even if you credit the echoed greeting, both prefill runs fall short of 10/10. The T3 count mismatch reported above is still unresolved.

### Text replies

The replies below preserve wording and punctuation. Whitespace is collapsed for display. Raw text, ASR transcripts, timing, and WAV files are under `$VOICE_HOME/evidence/one-voice/prefill-realtime` and `$VOICE_HOME/evidence/one-voice/prefill-no-system`. The original arms and their grades are in `$VOICE_HOME/evidence/one-voice/reply-review.md`. The probe and template are under `$VOICE_HOME/research/one-voice/prefill`.

| Spoken request | Run 1, with system prompt | Run 2, no system prompt |
| --- | --- | --- |
| Hi, it's nice to talk with you. | Hi, it's nice to talk with you.<br>**Fail. Repeats the greeting verbatim.** | Hi! It's nice to talk with you.<br>**Fail. Repeats the greeting with different punctuation.** |
| I could use a little encouragement today. | I could use a little encouragement today.<br>**Fail. Repeats the request for encouragement.** | I hear you—sometimes a little encouragement can make a big difference. If you’d like, I can share a quick, uplifting thought or a simple tip to help you get through the day. Let me know what would be most helpful!<br>**Fail. Offers encouragement later.** |
| Say something nice about a rainy morning. | On a rainy morning, the world seems to pause, and the gentle patter of rain on the windows creates a soothing, calming ambiance.<br>**Pass. Say something positive about a rainy morning.** | The gentle rain taps a soothing rhythm on the window, turning the world outside into a soft, muted symphony. Each drop feels like a quiet reminder that even the rainiest moments bring a fresh, clean breath to the air, and the world seems to pause in a calm, refreshing hush.<br>**Pass. Say something positive about a rainy morning.** |
| Give me two ideas for a quiet evening. | How about reading a book in a cozy nook, or taking a leisurely walk in a nearby park?<br>**Pass. Give two distinct quiet activities.** | 1. &#42;&#42;A cozy reading nook&#42;&#42; – Set up a comfy armchair or floor cushion with a soft throw, add a small lamp or string lights, and stock it with a few beloved books, a journal, and a cup of herbal tea. The gentle glow and quiet atmosphere create a perfect space to unwind and lose yourself in a good story. 2. &#42;&#42;A gentle nature walk&#42;&#42; – Take a leisurely stroll through a nearby park or garden, carrying a light blanket and a thermos of hot tea. The soft rustle of leaves, distant birdsong, and the cool evening air make the walk feel serene, allowing you to enjoy the tranquility of nature without any distractions.<br>**Pass. Give two distinct quiet activities.** |
| Tell me a short joke about a cat. | Why did the cat sit on the windowsill? Because it wanted to be the sun!<br>**Pass. Tell a short joke about a cat.** | Why did the cat sit on the windowsill? Because it wanted to be the sun!<br>**Pass. Tell a short joke about a cat.** |
| My favorite color is blue. What color did I just say? | The user mentioned that their favorite color is blue. They asked, "What color did I just say?" So, the answer is blue.&lt;/think&gt;The color you just said is blue.<br>**Fail. Repeats the question in spoken reasoning before the answer.** | You said the color is blue.<br>**Pass. Recall blue.** |
| I'm going to make tea. What's something nice about that? | I'm going to make tea. What's something nice about that?<br>**Fail. Repeats the tea question.** | The thing that’s nice about making tea is that it’s a simple, soothing, and refreshing way to start the day. It’s easy to prepare, requires just a few ingredients, and the warm, aromatic drink can be enjoyed alone or with friends and family. Plus, it’s a great opportunity to unwind and enjoy a moment of calm.<br>**Pass. Offer a positive thought about tea.** |
| Give me one idea for a short break. | How about taking a short walk in a nearby park to clear your mind?<br>**Pass. Suggest one short break.** | How about taking a 10‑minute walk outside to clear your head and enjoy some fresh air?<br>**Pass. Suggest one short break.** |
| What word did I use just now, apple or orange? I said orange. | The word you used just now is "orange."<br>**Pass. Recall orange.** | The word you used just now is &#42;&#42;orange&#42;&#42;.<br>**Pass. Recall orange.** |
| Thanks for keeping me company. Goodbye. | Understood. Goodbye.<br>**Pass. Acknowledge the farewell.** | Sure! Here are a few ways you can respond, depending on the context: &#42;&#42;Friendly and casual&#42;&#42; “Great to have you with me! Take care!” &#42;&#42;Professional or polite&#42;&#42; “Thank you for the company. Have a wonderful day!” &#42;&#42;Short and to the point&#42;&#42; “Thanks for the company. Goodbye!” Let me know if you’d like a different tone or more options!<br>**Fail. Lists sample farewells in place of a direct goodbye.** |

## Review the clips

The run logs, per-clip scores, and review notes are under `$VOICE_HOME/evidence/one-voice`. The `listening` folder has three diagnostic pairs from the first-clause cascade, each with a conversation reply followed by a T3 line. The pairs include the joke and the test count. The probe and review scripts are under `$VOICE_HOME/research/one-voice`.
