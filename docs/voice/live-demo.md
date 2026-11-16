# Live demo: the laptop talks, the Desktop serves

Mason talks into the laptop. The Desktop runs the whole assistant and streams the spoken reply back. This page covers how live mode works, what has been measured, and a one-page runbook. Each claim carries a label. "Measured" means a run produced it, "inferred" means it follows from code or docs, and "guess" means neither.

## Result

Live mode now runs the `gated` pipeline, the default on `voice/main` after PR #12 merged (measured: merge commit `4979ae3df`). Each turn takes the task gate, makes no decider call at the end of the utterance, and has LFM3's task session settle the tasks before the voice reply.

The CPU suite passes at this head. That is 16 live tests, 37 prototype tests and 89 segmentation tests (measured on 2026-11-16).

A real-GPU loopback ran on the previous head, `af51cdfc6`, before #12 merged. It ran 7 turns from Mason's clips with the reply audio played and every turn recorded (measured). The merged head has no real-GPU loopback yet, because this phase is CPU only. The PM's GPU slot runs the one command in [Loopback](#loopback).

The built-in persona narrates the task before answering ("The task is to find out the weather...") and sometimes speaks about Mason in the third person (measured, turn records from that loopback). Mason's chosen prompt is the fix, and the prompt is a file, so the demo needs no code change (measured: a test shows a prompt file reaching the turn as its persona).

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
| `voice/prototype/run.py` | The hook. `run_clip` takes an audio source, a persona and a voice-event callback, and keeps the prototype's `gated` default. `WavInput` keeps the clip path the same. |

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

llama.cpp has no `response.cancel`, so closing the voice session is how the reply stops (inferred from the string search). A turn that is waiting for a task update when speech starts is cancelled the same way, and that update is lost.

In the loopback, barge-in reached the client 125 ms after the reply's first audio, with the client sending speech at once (measured, turn 3 of the record). That is the 100 ms start window, up to one 20 ms frame, and the socket trip.

## Network and security

- The server binds to `tailscale ip -4`, and `check_bind` refuses any address outside 100.64.0.0/10 other than loopback. It never binds 0.0.0.0 or the LAN. Both refusals were run by hand at this head (measured: `--host 0.0.0.0` and `--host 192.168.1.5` each exit 1 with "refusing to bind").
- Every connection needs the `X-Voice-Secret` header. The secret is 64 hex characters made once in `$VOICE_HOME/live/secret`, read in-process, and never printed. A connection with an `Origin` header is refused, so a web page cannot reach it. One client at a time. A second gets close code 1013.
- The laptop zip carries the secret. `tailscale file cp` sends it over the tailnet to Mason's own device.
- The loopback's socket RTT was 0.6 ms median (measured, 28 pings, max 1.1 ms). `tailscale ping omen-laptop` answered in 5 ms over a direct LAN path (measured on 2026-11-16). So the tailnet adds about 5 ms of round trip when both machines are on the same LAN, not a WAN figure (inferred).
- Mason's audio goes only from the laptop to the Desktop. Each turn's input audio, reply audio and JSON record go under `$VOICE_HOME/evidence/live/<start time>/`. The client's log and the reply audio it got go under `$VOICE_HOME/live-client/` on the laptop. None of it is in git. The decider gets the transcript text only, as in every prototype run.

The Desktop already has `Tailscale-In` allow rules, and the tailnet interface is on the Private profile (measured). No rule names port 8770. If the laptop cannot connect, run this in an admin PowerShell, one line, and remove it with the second:

```powershell
New-NetFirewallRule -DisplayName "Voice live 8770" -Direction Inbound -Protocol TCP -LocalPort 8770 -RemoteAddress 100.64.0.0/10 -Action Allow
Remove-NetFirewallRule -DisplayName "Voice live 8770"
```

I did not change the firewall.

## Prompt

The voice persona comes from `$VOICE_HOME/live/prompt.txt`. The server reads it again at each turn start, so the next turn uses a new prompt with no restart. Each turn's record names the file and the first 12 hex characters of its SHA-256 under `live.prompt`. Delete the file to go back to the built-in persona. The server adds the format line "Respond with interleaved text and audio." itself, so leave it out of the file. The tool session's instructions are fixed by the `gated` design and are not part of the persona.

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

The fake `run_clip` stands in for the GPU pipeline in these tests. It exercises the server, endpointer, outbox, chunking and records, not LFM3.

## Loopback

The real loopback ran on `af51cdfc6`, before the gated merge, with Mason's clips through the client in place of the microphone. Evidence is under `$VOICE_HOME/evidence/live/loopback-20261116-064109/` (server records, `server.out`, client logs and the reply WAVs). Numbers below are single-run observations from those records, not a `quiet_run` benchmark.

- 7 turns, 7 turn records. 5 turns completed and 2 were barge-in cancellations (measured).
- Speech end to first reply audio at the client was 0.86 to 1.14 s, median 1.09 s (measured). The 0.7 s detector is inside that.
- Turn end to first audio sent was 0.16 to 0.44 s, median 0.39 s (measured).
- First audio to playback was 0.02 s median, and no reply underran (measured).
- llama-server was healthy 2.1 s after launch (measured).

The same loopback has to run once on the merged head, because `gated` adds the tool session before the voice reply. The command is below. It needs Mason's GPU slot, and it is the only step left before the demo.

```bash
export VOICE_ENV_FILE=<path to the .env that holds OPENROUTER_API_KEY>
python ~/.gpu/gpu_lease.py run --holder voice-live --purpose "live loopback on the gated head" --max-minutes 30 -- \
  python C:/3jsc/tools/quiet_run.py --holder voice-live --max-minutes 20 -- \
  "$VOICE_HOME/venv-lfm3/Scripts/python" -m voice.live.server --max-minutes 18 \
  --out "$VOICE_HOME/evidence/live/loopback-gated"

# second terminal, once the server prints its address
"$VOICE_HOME/venv-lfm3/Scripts/python" voice/live/client.py \
  --server ws://100.86.25.114:8770 --secret-file "$VOICE_HOME/live/secret" \
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

It is ready when it prints `live server on ws://100.x.y.z:8770`. The llama-server load took 2.1 s in the last loopback (measured), and the whole start a few seconds more.

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
- `gated` opens a tool session as well as the voice and ASR sessions, so each turn takes longer to first audio than the pre-merge loopback numbers above (inferred).
- The CPU tests use a fake `run_clip`, so they do not cover LFM3's timing or its replies. Only a real-GPU loopback does.

## Open decisions

- The prompt. Mason's best prompt from the kit replaces the built-in persona through `prompt.txt`. The kit's campaign 2 stays held for the demo.
- Whether 700 ms is the right silence. Mason's own pauses will tell. `--silence-ms` changes it per run.
