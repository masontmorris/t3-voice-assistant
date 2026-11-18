# T3 lines: the call's voice and first audio

Mason's goal: T3 lines play in the call's own voice, streamed like a normal reply, with the words still exactly as checked. This page records what LFM3-Audio on our llama.cpp build can do toward that, and what shipped.

## Answer

No exact same-voice path exists on this build. T3 lines stay in the TTS voice, which now streams. First audio before a T3 line drops by 0.47 s per line, median, through the live server's own code (measured).

## Why the voices differ

- **Two modes, three speakers.** `say_verbatim` used llama-server's `/v1/audio/speech` with voice `female`, which is LFM3's TTS mode under the GGUF's `tts` template: `Perform TTS in American English using a female voice.` (read from the GGUF). TTS mode knows only `female` and `male` (model card, `GET /props`). The call's voice session runs chat mode with `audio.output.voice: null` and no voice in its prompt (measured from `session.created`), and the model card gives chat mode no voice choice.
- **The chat speaker is neither TTS voice.** A speaker-embedding check (`voice/research/t3_voice/speaker.py`) scores the call's chat replies against TTS `female` at 0.70, against same-speaker pairs at 0.86 to 0.89 (measured, run 1). The metric separates LFM3's own voices: TTS `female` against `male` is 0.66 (measured, CPU).
- **No TTS prompt reaches it.** TTS with no gender in its prompt still speaks `female` (0.88 against `female`). Setting `voice: "female"` on a chat session leaves chat's speaker unchanged (0.87 against unset chat, 0.69 against TTS `female`). Both measured on CPU, 4 clips each.

## The rejected path: chat mode asked to repeat

`voice/research/t3_voice/call_voice.py` asks a fresh persona session to read the line aloud and releases its audio through the reply's `Release` gate, keyed to the line's exact words. On the GPU it spoke in the call's voice (0.855 against chat replies) and its text matched the line on 19 of 20 lines. The audio, though, did not say its own text. One line's text was "Should I stop the Fix Login Redirect thread?" and our ASR heard "She restarted the fixed login reader X thread." The realtime API exposes no audio sampling setting to tighten this; only `/tts` takes `temperature` (inferred from the server README and the binary's strings). PR #60 already showed that chat mode rewords T3 lines when trusted.

## Measurements

Every number below comes from runs inside the GPU lease and `quiet_run`, on the 20 synthetic lines in `voice/research/t3_voice/lines.txt`, one run per configuration. Evidence: `$VOICE_HOME/evidence/t3-same-voice/run1` and `run2`.

Run 1, `python -m voice.research.t3_voice.bench`: each voice through a stand-in for the live sender, transcribed by the server's own live ASR session and scored by `score.py`. A slot miss is a thread name, status or number transcribed wrong.

| voice | first audio, median (range) | total | WER | lines with a slot miss |
| --- | --- | --- | --- | --- |
| old: whole WAV, TTS `female` | 0.557 s (0.316 to 1.089) | 0.569 s | 3.1% | 4 of 20 |
| streamed TTS `female` | 0.039 s (0.037 to 0.069) | 0.605 s | 5.2% | 5 of 20 |
| call voice, text-gated | 0.247 s (0.227 to 0.280) | 1.593 s | 23.1% | 13 of 20 |

Most TTS slot misses come from the ASR, not the voice (inferred). "Add" comes back "ad" on every voice, and "Voice sandbox" as "VoiceSandbox". The same TTS speaker scores 3.1% and 5.2% on two sets of samples of the same lines, which gives a rough sense of the spread.

Run 2, `python -m voice.research.t3_voice.integrated`: the shipped `say_verbatim`, `Conversation.speak_t3` and sender, to a WebSocket client. All 40 lines were delivered `sent` with every byte.

| render | first audio at the client, median (range) |
| --- | --- |
| old whole clip | 0.516 s (0.295 to 0.984) |
| streamed | 0.048 s (0.042 to 0.061) |
| saving per line | 0.466 s (0.249 to 0.941) |

What limits each number. The old path's first audio is 0.11 to 0.13 s per second of line audio plus 0.05 s (r 0.98 and 0.99 in runs 1 and 2), so it waits on generating the whole line at the RTF Stage 1 measured. The streamed path is flat at 0.04 to 0.05 s whatever the line's length. Its floor is the request's prompt prefill and first decoded chunk (inferred, not profiled inside llama.cpp). Transport from the sender to the client is under 3 ms on loopback (measured). A remote client adds its network round trip, which was not measured.

## The whole path, end of speech to first audio

From the dress rehearsal's turn records (`m1-dress-rehearsal-7f9b9bcb04`, one run, measured), in seconds:

| turn | silence detector | ASR final | snapshot | render | decider | TTS, whole line | total |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 001 | 0.700 | 0.088 | 0.302 | 0.485 | 0.275 | 0.586 | 2.42 |
| 002 | 0.700 | 0.356 | 0.254 | 0.402 | 0.321 | 0.590 | 2.61 |
| 004 | 0.700 | 0.024 | 0.142 | 0.681 | 0.168 | 0.391 | 2.10 |
| 005 | 0.700 | 0.225 | 0.010 | 1.195 | 0.141 | 0.992 | 3.24 |
| 006 | 0.700 | 0.124 | 0.121 | 0.454 | 0.128 | 0.609 | 2.13 |

Before this change, after the fixed 0.7 s silence detector, whole-line TTS was the largest stage by median (0.59 s), just ahead of the render (0.485 s). Streaming takes the TTS stage to about 0.05 s, which would put these turns at 1.57 to 2.30 s (inferred: the rehearsal minus the measured saving, not a new call). That leaves the detector as the largest cost and the cloud render as the largest variable one.

## Gaps

- The old path served a repeated line from its disk cache and wrote every T3 line's WAV under `$VOICE_HOME/live`. The streamed path renders every line, at about 0.05 s to first audio, and writes nothing.
- Mason has not listened to the clips. `call_08.wav` against `tts_08.wav` in run 1 is the clearest pair to hear the chat voice's drift.
