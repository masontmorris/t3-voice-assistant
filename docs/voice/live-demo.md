# Live demo: the laptop talks, the Desktop serves

Mason talks into the laptop. The Desktop runs the whole assistant and streams the spoken reply back. This page covers how live mode works, what has been measured, and a one-page runbook. Each claim carries a label. "Measured" means a run produced it, "inferred" means it follows from code or docs, and "guess" means neither.

## Result

Live mode now runs the `gated` pipeline, the default on `voice/main` after PR #12 merged (measured: merge commit `4979ae3df`). Each turn takes the task gate, makes no decider call at the end of the utterance, and has LFM3's task session settle the tasks before the voice reply.

A real-GPU loopback ran on `d6fb7f968`, the head before the playback-stop change described under [Talking over the reply](#talking-over-the-reply), on 2026-11-16 inside an ordinary `voice-live` lease and `quiet_run`, with no priority (measured). Five turns from Mason's clips went through the live client, the server and LFM3. The client received 24 kHz PCM16 for every reply, byte-identical to the reply WAVs the turn wrote, so the model's audio reached it through `run_clip` (measured: SHA-256 match on 6 of 6 client WAVs). Turn IDs ran 1 to 5 across two client connections. A pause in one clip continued into the next turn with 4.22 s of carried audio, and the real weather tool answered with specific forecasts (measured).

The CPU suite passes at this head. That is 18 live tests, 37 prototype tests and 89 segmentation tests (measured on 2026-11-16). Two live tests are new. One drives the real `run_clip` against a scripted realtime server, so it fails when the voice session's events stop reaching the live callback. The other covers speech that starts after a turn has finished and its reply is still playing.

The built-in persona narrates the task before answering ("The task is to find out the weather...") and sometimes speaks about Mason in the third person (measured, turn records from the earlier loopback). Mason's chosen prompt is the fix, and the prompt is a file. The loopback above ran with the kit v2 reference prompt in that file, which the records name by hash (measured). No prompt has been chosen yet, so the demo still needs one dropped in.

## What runs where

The laptop runs `voice/live/client.py` and nothing else. It sends 16 kHz mono PCM16 in 20 ms frames and plays what comes back.

The Desktop runs `python -m voice.live.server`. It starts llama-server with LFM3 and three slots (voice, ASR, tools), opens the real weather and time tools, and listens on its Tailscale address. Each turn goes through the same `run_clip` that the recorded-clip runs use, with the `gated` end-of-turn design.

| File | What it does |
| --- | --- |
| `voice/live/server.py` | The WebSocket server, the shared secret, the bind check, and one `Conversation` per client that turns endpointer events into turns. |
| `voice/live/endpoint.py` | The energy-and-silence endpointer. |
| `voice/live/client.py` | The laptop client. It uses the microphone or WAV clips, plays the reply, and has a push-to-talk fallback. |
| `voice/live/package.py` | Builds the laptop zip under `$VOICE_HOME/live/dist/`. |
| `voice/live/SETUP-LAPTOP.md` | The laptop's install steps. It ships in the zip. |
| `voice/prototype/run.py` | The hook. `run_clip` takes an audio source, a persona and a voice-event callback, hands each voice-session event to that callback, and keeps the prototype's `gated` default. `WavInput` keeps the clip path the same. |

## Turn-taking

llama.cpp b12428's `/v1/realtime` has no server-side voice activity detection. Its server DLL holds none of the strings `server_vad`, `turn_detection`, `speech_started`, `silence_duration_ms` or `response.cancel` (measured with a string search of `llama-server-impl.dll`). The event names it does hold are the ones the prototype already uses. So the live server ends turns itself.

`endpoint.py` reads 20 ms frames. A frame counts as speech when it is louder than both -50 dBFS and the noise floor plus 10 dB. The noise floor starts at the first frame, drops at once to any quieter frame, and rises 1 dB per second. A turn starts after 100 ms of speech and keeps the 300 ms before it. It ends after 700 ms with no speech, or after 30 s. Every value is a server flag (`--silence-ms`, `--start-ms`, `--preroll-ms`, `--floor-dbfs`, `--margin-db`, `--max-turn-s`).

I ran the endpointer over Mason's 11 clips with a noise bed added at -55 dBFS and at -45 dBFS (measured, CPU only). With a 10 dB margin, 9 of 11 clips made one turn at both noise levels. `08_cancel` split into two turns at the pause before "cancel that". `05_topic_change` split at -45 dBFS. A 15 dB margin split more clips: 2 at -55 dBFS and 3 at -45 dBFS. So the default is 10 dB.

A split costs little when the second part starts before any reply audio. The server cancels the first turn and starts a new one with the first turn's audio in front ("continued" in the record). The cost is the first turn's decider calls.

## Talking over the reply

If speech starts while a turn has sent reply audio, the server cancels that turn. It does these things in order.

1. It marks the turn muted, so the sender drops the turn's audio frames that are still queued.
2. It sends `barge_in` to the client. The client empties its playback buffer.
3. It cancels the turn's task. Its voice, ASR and tool sessions close, and its decider wrapper refuses further calls.
4. It starts the new turn after the old turn's teardown finishes, so the turns never hold more than llama-server's three slots.

Speech that starts after a turn has finished still drops that turn's audio. The model streams a reply much faster than the client plays it. In the current-head loopback, 4.88 s of reply audio reached the client in 0.63 s (measured). So a turn is usually done while its audio is still playing, and waiting for the turn's task to finish before reacting would leave the old reply running over the new turn. A new turn now mutes the previous turn and sends `barge_in` even when that turn has finished, which clears the client's playback buffer (measured in the CPU test `test_speech_after_the_reply_stops_playback`).

llama.cpp has no `response.cancel`, so closing the voice session is how the reply stops (inferred from the string search). A turn that is waiting for a task update when speech starts is cancelled the same way, and that update is lost.

In the earlier head's loopback, barge-in reached the client 125 ms after the reply's first audio, with the client sending speech at once (measured, turn 3 of that record). That is the 100 ms start window, up to one 20 ms frame, and the socket trip.

## Network and security

- The server binds to `tailscale ip -4`, and `check_bind` refuses any address outside 100.64.0.0/10 other than loopback. It never binds 0.0.0.0 or the LAN. Both refusals were run by hand at this head (measured: `--host 0.0.0.0` and `--host 192.168.1.5` each exit 1 with "refusing to bind").
- Every connection needs the `X-Voice-Secret` header. The secret is 64 hex characters made once in `$VOICE_HOME/live/secret`, read in-process, and never printed. A connection with an `Origin` header is refused, so a web page cannot reach it. One client at a time. A second gets close code 1013.
- The laptop zip carries the secret. `tailscale file cp` sends it over the tailnet to Mason's own device.
- The loopback's socket RTT was 0.5 ms median over 15 pings, max 0.7 ms, at the current head (measured). The previous head measured 0.6 ms over 28 pings. `tailscale ping omen-laptop` answered in 5 ms over a direct LAN path (measured on 2026-11-16). So the tailnet adds about 5 ms of round trip when both machines are on the same LAN, not a WAN figure (inferred).
- Mason's audio goes only from the laptop to the Desktop. Each turn's input audio, reply audio and JSON record go under `$VOICE_HOME/evidence/live/<start time>/`. The client's log and the reply audio it got go under `$VOICE_HOME/live-client/` on the laptop. None of it is in git. The decider gets the transcript text only, as in every prototype run.

The Desktop already has `Tailscale-In` allow rules, and the tailnet interface is on the Private profile (measured). No rule names port 8770. If the laptop cannot connect, run this in an admin PowerShell, one line, and remove it with the second:

```powershell
New-NetFirewallRule -DisplayName "Voice live 8770" -Direction Inbound -Protocol TCP -LocalPort 8770 -RemoteAddress 100.64.0.0/10 -Action Allow
Remove-NetFirewallRule -DisplayName "Voice live 8770"
```

I did not change the firewall.

## Prompt

The voice persona comes from `$VOICE_HOME/live/prompt.txt`. The server reads it again at each turn start, so the next turn uses a new prompt with no restart. Each turn's record names the file and the first 12 hex characters of its SHA-256 under `live.prompt`. Delete the file to go back to the built-in persona. The server adds the format line "Respond with interleaved text and audio." itself, so leave it out of the file. The tool session's instructions are fixed by the `gated` design and are not part of the persona. The current-head loopback used the kit v2 reference prompt this way (measured: `prompt.txt sha256:020fd0f20f5e` in all five records), and the file was removed after the run.

A CPU test writes "Be brief." to the prompt file and confirms the turn receives it as its persona (measured, `test_the_prompt_file_sets_the_turn_persona`).

## CPU checks

All of these ran at this head, CPU only, in `$VOICE_HOME/venv-lfm3` (measured on 2026-11-16).

| Check | Test | Result |
| --- | --- | --- |
| Bind to Tailscale or loopback only | `BindTest.test_only_tailnet_or_loopback`, plus two hand runs of the module | pass, both refusals exit 1 |
| Secret and `Origin` rejection | `test_a_missing_or_wrong_secret_is_refused` | pass, HTTP 401 and 403 |
| One client at a time | `test_a_second_client_is_turned_away` | pass, close 1013 |
| PCM chunking | `test_upstream_audio_goes_to_llama_in_40_ms_chunks`, `test_a_turn_streams_its_reply_back` | pass, 1280 bytes up, no frame over 40 ms down |
| Prompt config | `test_the_prompt_file_sets_the_turn_persona` | pass |
| Silence ends a turn | five `EndpointerTest` cases | pass, 700 ms, pre-roll kept, click ignored |
| Barge-in and continuation | `test_speech_during_the_reply_interrupts_it`, `test_speech_before_the_reply_continues_the_turn` | pass |
| Speech after the turn ends stops playback | `test_speech_after_the_reply_stops_playback` | pass |
| Voice events reach the live callback | `test_run_clip_hands_voice_audio_to_the_live_callback` | pass, real `run_clip` against a scripted realtime server |

The other live tests use a fake `run_clip` that stands in for the GPU pipeline. They exercise the server, endpointer, outbox, chunking and records, not LFM3. The seam test above runs the real `run_clip` against a scripted server, and still not LFM3.

## Loopback

The current-head loopback ran on 2026-11-16 at `d6fb7f968`, in the ordinary `voice-live` GPU lease with `quiet_run` and no priority. Mason's clips went through the live client in place of the microphone. Evidence is under `$VOICE_HOME/evidence/live/loopback-gated-20261116-071438/` (server turn records and reply audio, `server.out`, the two client logs and their reply WAVs). Numbers below are single-run observations from those records, not a benchmark. The server bound loopback (`--host 127.0.0.1`) and the client reached it on `ws://127.0.0.1:8871`, so the tailnet hop was not part of this run.

- 5 turns from 5 clips over two client connections. 4 completed and 1 was a `continued` cancellation that carried 4.22 s of audio into the next turn (measured).
- Every client reply WAV is byte-identical to the server's `turn_00x_reply.wav` or `turn_00x_update.wav`, 6 of 6, all 24 kHz (measured, SHA-256). That is the proof the client got the model's PCM through `run_clip`.
- Turn end to first audio sent was 0.32 to 0.70 s, median 0.61 s (measured). Speech end to the client's first audio was 1.02 to 1.40 s (measured). The 0.7 s detector is inside that.
- First audio to playback was 0.018 to 0.024 s, and no reply underran (measured).
- llama-server was healthy 2.6 s after launch and the live server listened 1.9 s later (measured).
- Socket round trip was 0.5 ms median over 15 pings, max 0.7 ms, on loopback (measured).
- Each turn made one `delegate` call, and the task service ran the real lookups (measured). Turn 1's update reply carried a specific Denver forecast for the next day, turn 4's carried a weekend forecast for Saturday and Sunday, and turn 5's reply reported a failed weather lookup because that clip names no place, which is the real tool's error (measured, reply text in the records). Turn 4's update did not mention Tokyo, though the request asked for its time (measured, reply text).
- The 08_cancel clip split at its pause. Its first part ended before any reply audio, so the server cancelled it as `continued` and turn 3 carried the audio (measured). That is the continuation path at the real model.

The staged barge-in did not fire. The second clip's first 0.68 s is quieter than the -50 dBFS floor, and the client exits 0.73 s after it starts sending that clip, so the endpointer never saw 100 ms of voiced audio in time (measured: per-frame levels and client timings). Live barge-in is covered at this head only by the CPU test. Starting the next clip sooner (`--barge-in-ms 300`) or using a clip that opens loudly stages it. The earlier loopback on `af51cdfc6`, before the gated merge, is still the only live barge-in record: 7 turns, 2 barge-in cancellations, speech end to first audio 0.86 to 1.14 s, median 1.09 s, and turn end to first audio 0.16 to 0.44 s, median 0.39 s (measured).

The command below repeats the current-head run. An ordinary `voice-live` lease is authorized for it. `--priority` is only for the live demo itself, with Mason's approval.

```bash
export VOICE_ENV_FILE=<path to the .env that holds OPENROUTER_API_KEY>
python ~/.gpu/gpu_lease.py run --holder voice-live --purpose "live loopback on the gated head" --max-minutes 30 -- \
  python <path to quiet_run.py> --holder voice-live --max-minutes 20 -- \
  "$VOICE_HOME/venv-lfm3/Scripts/python" -m voice.live.server --host 127.0.0.1 --max-minutes 18 \
  --out "$VOICE_HOME/evidence/live/loopback-gated"

# second terminal, once the server prints its address
"$VOICE_HOME/venv-live-client/Scripts/python" voice/live/client.py \
  --server ws://127.0.0.1:8770 --secret-file "$VOICE_HOME/live/secret" \
  --wav <clip.wav> [<clip.wav> ...] --output "<device>" --log-dir "$VOICE_HOME/evidence/live/loopback-gated/client"
```

The client exits after the last clip's turn settles. Then stop the server with `touch "$VOICE_HOME/live/STOP"` and check `python ~/.gpu/gpu_lease.py status`.

## Runbook

All Desktop commands run in Git Bash from the repo checkout. The demo needs the GPU, so the server runs inside one lease. `--priority` is for the demo only, with Mason's approval.

**Start the server.**

```bash
export VOICE_ENV_FILE=<path to the .env that holds OPENROUTER_API_KEY>
python ~/.gpu/gpu_lease.py run --holder voice-live --purpose "live demo with Mason" --max-minutes 60 --priority -- \
  "$VOICE_HOME/venv-lfm3/Scripts/python" -m voice.live.server --max-minutes 55
```

It is ready when it prints `live server on ws://100.x.y.z:8770`. The llama-server load took 2.6 s in the last loopback (measured), and the whole start a few seconds more.

**Send the laptop its client** (once, or after a code change).

```bash
"$VOICE_HOME/venv-lfm3/Scripts/python" -m voice.live.package
tailscale file cp "$VOICE_HOME/live/dist/voice-live-client.zip" omen-laptop:
```

The packager prints the zip's SHA-256. The zip is under `$VOICE_HOME/live/dist/`, never in git.

**Start the client on the laptop**, after `SETUP-LAPTOP.md` once.

```powershell
cd $HOME\voice-live-client
.\.venv\Scripts\python client.py --input "<mic>" --output "<headphones>"
```

Use headphones. If silence detection misbehaves, add `--ptt` and press Enter to start and stop talking.

**Swap the prompt.** Write Mason's chosen persona to `$VOICE_HOME/live/prompt.txt`. The next turn uses it. The record names the file and its hash. Delete the file to go back to the built-in persona.

**Stop everything.** Press Ctrl+C in the laptop window. On the Desktop, press Ctrl+C in the server window, or run `touch "$VOICE_HOME/live/STOP"`. The server stops llama-server and the MCP time server on the way out, and the lease ends with it. Check with `python ~/.gpu/gpu_lease.py status`.

## Limits

- Each turn is a fresh conversation. The voice session, the segmenter, the tool session and the task service start over, so turn 2 cannot refer to turn 1 or to its tasks. `run_clip` is one utterance by design. Multi-turn memory needs a session that outlives the turn.
- There is no echo cancellation. Reply audio from speakers would cancel the reply.
- The endpointer listens to loudness only. A cough starts a turn, and a long thinking pause ends one.
- `gated` opens a tool session as well as the voice and ASR sessions. At this head that put the first reply audio 0.32 to 0.70 s after the audio ended (measured).
- The CPU tests stand in for LFM3, except one, which drives the real `run_clip` against a scripted realtime server to cover the event seam. None of them covers LFM3's replies or its timing. Only a real-GPU loopback does.
- The current-head loopback ran on loopback, so the Tailscale hop and the firewall rule are unexercised at this head (measured: bind address in `server.out`).
- Live barge-in while a reply plays was not observed at this head. The staged attempt missed the -50 dBFS floor, as described in [Loopback](#loopback). The CPU test covers the path, and the earlier head has a live record.

## Open decisions

- The prompt. Mason's best prompt from the kit replaces the built-in persona through `prompt.txt`. The kit's campaign 2 stays held for the demo.
- Whether 700 ms is the right silence. Mason's own pauses will tell. `--silence-ms` changes it per run.
