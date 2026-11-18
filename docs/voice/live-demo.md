# Live demo: the laptop talks, the Desktop serves

Mason talks into the laptop. The Desktop runs the whole assistant and streams the spoken reply back. This page covers how live mode works, what has been measured, and a one-page runbook. Each claim carries a label. "Measured" means a run produced it, "inferred" means it follows from code or docs, and "guess" means neither.

## Result

Live mode now runs the `gated` pipeline, the default on `voice/main` after PR #12 merged (measured: merge commit `4979ae3df`). Each turn takes the task gate, makes no decider call at the end of the utterance, and has LFM3's task session settle the tasks before the voice reply.

One client connection is now one LFM3 voice session, so every turn of a call adds to the same conversation (measured: one session for all five turns of the scripted call below, and a probe that answers recall questions about its own earlier audio turn).

A real-GPU loopback ran on `d6fb7f968`, the head before the playback-stop change described under [Talking over the reply](#talking-over-the-reply), on 2026-11-16 inside an ordinary `voice-live` lease and `quiet_run`, with no priority (measured). Five turns from Mason's clips went through the live client, the server and LFM3. The client received 24 kHz PCM16 for every reply, byte-identical to the reply WAVs the turn wrote, so the model's audio reached it through `run_clip` (measured: SHA-256 match on 6 of 6 client WAVs). Turn IDs ran 1 to 5 across two client connections. A pause in one clip continued into the next turn with 4.22 s of carried audio, and the real weather tool answered with specific forecasts (measured).

The CPU suite passes at this head. That is 36 live tests (14 server, 6 endpointer, 7 recording, 9 client) and 41 prototype tests, plus 89 segmentation and 16 tools tests that this change does not touch (measured on 2026-11-16; the client tests run in the client venv, the rest in `$VOICE_HOME/venv-lfm3`). Two live tests are new: one asserts that a second turn reuses the first turn's voice session, and one that a long call closes it and seeds a new one with the call's own text. Two live tests changed: the barge-in test now also asserts the session stays open and the next turn uses it, and the prompt test asserts the call's rules follow the persona. Three prototype tests are new, and they assert that `run_clip` adds to a caller's session, sends its settings once and leaves it open. The seam test still drives the real `run_clip` against a scripted realtime server, so it fails when the voice session's events stop reaching the live callback.

The built-in persona narrates the task before answering ("The task is to find out the weather...") and sometimes speaks about Mason in the third person (measured, turn records from the earlier loopback). Mason's chosen prompt is the fix, and the prompt is a file. The loopback above ran with the kit v2 reference prompt in that file, which the records name by hash (measured). No prompt has been chosen yet, so the demo still needs one dropped in.

## What runs where

The laptop runs `voice/live/client.py` and nothing else. It sends 16 kHz mono PCM16 in 20 ms frames and plays what comes back.

The Desktop runs `python -m voice.live.server`. It starts llama-server with LFM3 and three slots (voice, ASR, tools), opens the real weather and time tools, and listens on its Tailscale address. Each turn goes through the same `run_clip` that the recorded-clip runs use, with the `gated` end-of-turn design.

| File                         | What it does                                                                                                                                                                                                                                                                               |
| ---------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `voice/live/server.py`       | The WebSocket server, the shared secret, the bind check, and one `Conversation` per client that turns endpointer events into turns. It owns the call's one LFM3 voice session.                                                                                                             |
| `voice/live/endpoint.py`     | The energy-and-silence endpointer.                                                                                                                                                                                                                                                         |
| `voice/live/client.py`       | The laptop client. It uses the microphone or WAV clips, plays the reply, and has a push-to-talk fallback.                                                                                                                                                                                  |
| `voice/live/web/`            | The browser client, served by the server on GET `/`. See [browser-client.md](browser-client.md).                                                                                                                                                                                           |
| `voice/live/script.py`       | Renders a scripted conversation to WAV clips with the running server's own TTS, for a `--wav` run.                                                                                                                                                                                         |
| `voice/live/package.py`      | Builds the laptop zip under `$VOICE_HOME/live/dist/`.                                                                                                                                                                                                                                      |
| `voice/live/SETUP-LAPTOP.md` | The laptop's install steps. It ships in the zip.                                                                                                                                                                                                                                           |
| `voice/prototype/run.py`     | The hook. `run_clip` takes an audio source, a persona and a voice-event callback, hands each voice-session event to that callback, and keeps the prototype's `gated` default. `WavInput` keeps the clip path the same. Live mode also passes its own long-lived voice session as `voice=`. |

## Turn-taking

llama.cpp b12428's `/v1/realtime` has no server-side voice activity detection. Its server DLL holds none of the strings `server_vad`, `turn_detection`, `speech_started`, `silence_duration_ms` or `response.cancel` (measured with a string search of `llama-server-impl.dll`). The event names it does hold are the ones the prototype already uses. So the live server ends turns itself.

`endpoint.py` reads 20 ms frames. A frame counts as speech when it is louder than both -50 dBFS and the noise floor plus 10 dB. The noise floor starts at the first frame, drops at once to any quieter frame, and rises 1 dB per second. A turn starts after 100 ms of speech and keeps the 300 ms before it. It ends after 700 ms with no speech, or after 30 s. Every value is a server flag (`--silence-ms`, `--start-ms`, `--preroll-ms`, `--floor-dbfs`, `--margin-db`, `--max-turn-s`).

I ran the endpointer over Mason's 11 clips with a noise bed added at -55 dBFS and at -45 dBFS (measured, CPU only). With a 10 dB margin, 9 of 11 clips made one turn at both noise levels. `08_cancel` split into two turns at the pause before "cancel that". `05_topic_change` split at -45 dBFS. A 15 dB margin split more clips: 2 at -55 dBFS and 3 at -45 dBFS. So the default is 10 dB.

A split costs little when the second part starts before any reply audio. The server cancels the first turn and starts a new one with the first turn's audio in front ("continued" in the record). The cost is the first turn's decider calls.

## Talking over the reply

The server knows how long the client will keep playing: each checked reply adds its audio's duration to a playback clock, starting from when it was sent or when the audio before it ends. The model streams a reply much faster than the client plays it (4.88 s of reply audio reached the client in 0.63 s, measured in an earlier loopback), so the clock follows the audio, not the turn's task. Speech is weighed against that clock. [barge-in.md](barge-in.md) has the evidence and the numbers.

Speech that starts while a reply is audible is an overlap, and the client ducks the reply at once. The overlap keeps every frame it hears until one of these verdicts.

1. **Barge-in.** It reaches 600 ms of voiced audio (`--confirm-ms`), or it is shorter but its one-shot transcript is not a backchannel. The server marks the playing turn muted, so the sender drops its frames still queued, and sends `barge_in`; the client empties its playback buffer. The turn finishes on the call's shared session, so the reply it started stays in the history. The overlap's audio opens the next turn, so the words that interrupted are not lost.
2. **Dismissed.** Its transcript is a backchannel ("okay", "mm-hm", "yeah, totally"), empty, or mostly the reply's own words coming back through speakers. The server sends `unduck` and the reply plays on. The overlap's audio and transcript go to `overlaps.jsonl` and `overlap_NNN.wav` in the record directory.
3. **Turn.** The reply finishes playing first. The overlap becomes an ordinary turn and nothing is stopped.

Speech that starts after the reply has played starts a turn at once and sends nothing to the client. A turn that has spoken and is still running, such as one waiting for a task update, is muted then, and its update reply is generated but not sent.

llama.cpp has no `response.cancel` (measured: a string search of the server DLL). Before the shared session a turn was cut off by closing its voice session, which also threw the call's history away. Now the reply is left to finish on the shared session and only its audio is dropped, so the next turn still knows what was said.

## Memory across a call

A client connection is a call, and a call is one LFM3 voice session. The first turn opens it and every later turn adds its audio, its snapshot and its reply to the same conversation, so the model hears both sides of the call when it answers. The ASR, segmenter, task service and tools session are still built per turn, which is why `run_clip` keeps its shape and takes an optional already-open voice session instead of owning one.

Two things follow from never being able to delete a conversation item (measured: llama.cpp b12428's server library names `conversation.item.create`, `added` and `done`, and nothing that removes an item).

**Snapshots.** Every turn injects its task snapshot as a user text item with the machine prefix. Over a call those items pile up, and an old snapshot reads exactly like a current one. The call's instructions therefore say that only the newest snapshot is current, and that a task or result from an older snapshot must not be spoken. The rule lives in one place, in the session instructions, rather than in every injected item, so a snapshot costs the same as before.

**A long call.** llama.cpp divides `-c` across its slots, so the voice session has 11,008 tokens at the default `-c 32768` and three slots (measured: `n_ctx_slot = 11008` in the server log). Past that a slot runs out of room, and what llama.cpp does then was not observed at this head (guess: it drops the oldest tokens, which is the start of the call). So a call that reaches `--call-max-turns` (default 8) closes its session and opens a new one seeded with the call's own transcript, up to `--call-keep-turns` earlier turns (default 6), as one machine-prefixed item. The seed is one item, so it cannot be mistaken for something the person said, and it is what would keep "what did I ask you first" answerable after the slot fills. llama.cpp refuses new instructions once a session holds a message, and answers the refusal with an `error` event, not `session.updated` (measured on build 12428 by a probe that sends both orders, `$VOICE_HOME/evidence/live/callfix-20261116/refusal_probe.py`). So `run_clip` sends the new session's settings first and creates the seed after them, and any refused `session.update` ends the turn with the server's text in the record instead of a 120 s wait.

**Reseeds, checked 2026-11-16.** Two real calls in an ordinary `voice-live-memory` lease under `quiet_run`, no priority, with the server's own TTS clips. With `--call-max-turns 1`, turn 2 reseeded and finished in 6.50 s, first audio 0.45 s after its turn ended. With `--call-max-turns 2` and five turns, `voice_sessions` read 1, 1, 2, 2, 3, every reseeded turn's seed was accepted after its settings, turns took 5.2 to 8.1 s, and first audio came 0.40 to 0.68 s after each turn ended (measured, turn records). No turn hit the timeout. Each record's `live.call.reopened` names why its session opened: `first turn`, `turn limit` or `persona changed`.

**Checking it.** `voice/live/script.py` renders a file of one utterance per line to WAV clips with the running server's own TTS, and the client's `--wav` path plays them as one call, in order. Both run while the server holds the lease:

```bash
python -m voice.live.script --turns turns.txt --out clips --server http://127.0.0.1:8095
"$VOICE_HOME/venv-live-client/Scripts/python" voice/live/client.py --server ws://127.0.0.1:8770 \
  --secret-file "$VOICE_HOME/live/secret" --wav clips/turn_*.wav --log-dir "$VOICE_HOME/evidence/live/<run>/client"
```

Each clip after the first starts once the previous reply has played, the way a person waits. `--barge-in-ms 1500` sends the next clip 1.5 s into a reply instead, so it overlaps the reply and the gate decides.

**Checked, 2026-11-16.** The model's own TTS rendered five turns and a second three-turn set, and the real client played them as two calls over loopback, in an ordinary `voice-live-memory` lease under `quiet_run`, with no priority and no `--priority` (measured). Mason's `prompt.txt` was in place and the records name it (`sha256:02700e254b8f`, measured). The five-turn call kept one voice session for all five turns: turn 1 records `voice_session: started`, turns 2 to 5 record `shared`, and every turn records `voice_sessions: 1` (measured, from the turn records). Asked "And what about Saturday?" after "What's the weather in Denver tomorrow?", the reply was "I'm running the request to get the weather in Denver for Saturday", so the city and the follow-up both carried (measured, turn 2).

`voice/live/probe.py` says what the runtime keeps, without the task pipeline in the way. One session, three exchanges: with text items it answered all three recall questions, and with one turn said as audio instead, the way live mode speaks, it answered "What did I just say?" with the clip's own words and "Which city did I mention?" with "Denver" (measured, 3 of 3 kept in both runs).

What still limits the call is the reply, not the session. Every turn's snapshot enters as a user item just before the reply, and the persona says to say only what the snapshot says, so the model answers the snapshot: "What did I just ask you?" came back as "I just asked you, 'What did I just ask you?'" (measured, turn 3). Sharpening the call's instructions to answer from the conversation made it narrate in the third person instead ("The user asked, ...", measured in an earlier run of the same script, turn 2), so this change keeps only the snapshot rule and leaves the persona to Mason's choice. A persona that answers conversationally is what turns this session's memory into conversational replies.

Timing, measured in the same run. Turn end to first audio was 0.47 to 0.62 s, median 0.53 s over five turns, against 0.32 to 0.70 s, median 0.61 s, for the earlier per-turn loopback. The client's speech end to first audio was 1.17 to 1.32 s, median 1.23 s, against 1.02 to 1.40 s before. First audio to playback stayed 0.004 to 0.039 s. So a normal turn costs nothing measurable here. The cost sits in a turn that follows a real barge-in: the muted turn runs to its end before the new one starts, and the turn after a barge-in waited 2.99 s once (measured, an earlier run of the same script, turn 8) when the muted turn still had a task update to speak. Speech ends that follow a barge-in on a turn with nothing left to do waited 0.53 s (measured, the shipped run, turn 7).

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

The voice persona comes from `$VOICE_HOME/live/prompt.txt`. The server reads it again at each turn start, so the next turn uses a new prompt with no restart. llama.cpp keeps a session's instructions once it holds audio, so a changed prompt starts a new voice session for the call, seeded with the call so far like a long call's. Each turn's record names the file and the first 12 hex characters of its SHA-256 under `live.prompt`. Delete the file to go back to the built-in persona. The server adds the format line "Respond with interleaved text and audio." itself, so leave it out of the file. The tool session's instructions are fixed by the `gated` design and are not part of the persona. The current-head loopback used the kit v2 reference prompt this way (measured: `prompt.txt sha256:020fd0f20f5e` in all five records), and the file was removed after the run.

A CPU test writes "Be brief." to the prompt file and confirms the turn receives it as its persona (measured, `test_the_prompt_file_sets_the_turn_persona`).

## CPU checks

All of these ran at this head, CPU only, in `$VOICE_HOME/venv-lfm3` (measured on 2026-11-16).

| Check                                     | Test                                                                                            | Result                                                                       |
| ----------------------------------------- | ----------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------- |
| Bind to Tailscale or loopback only        | `BindTest.test_only_tailnet_or_loopback`, plus two hand runs of the module                      | pass, both refusals exit 1                                                   |
| Secret and `Origin` rejection             | `test_a_missing_or_wrong_secret_is_refused`                                                     | pass, HTTP 401 and 403                                                       |
| One client at a time                      | `test_a_second_client_is_turned_away`                                                           | pass, close 1013                                                             |
| PCM chunking                              | `test_upstream_audio_goes_to_llama_in_40_ms_chunks`, `test_a_turn_streams_its_reply_back`       | pass, 1280 bytes up, no frame over 40 ms down                                |
| Prompt config                             | `test_the_prompt_file_sets_the_turn_persona`                                                    | pass                                                                         |
| Silence ends a turn                       | five `EndpointerTest` cases                                                                     | pass, 700 ms, pre-roll kept, click ignored                                   |
| Barge-in and continuation                 | `test_speech_during_the_reply_interrupts_it`, `test_speech_before_the_reply_continues_the_turn` | pass                                                                         |
| Speech after the turn ends stops playback | `test_speech_after_the_reply_stops_playback`                                                    | pass                                                                         |
| Voice events reach the live callback      | `test_run_clip_hands_voice_audio_to_the_live_callback`                                          | pass, real `run_clip` against a scripted realtime server                     |
| One voice session per call                | `test_a_call_keeps_one_voice_session`                                                           | pass, turn 2 is given turn 1's session                                       |
| A long call seeds a new session           | `test_a_long_call_seeds_a_new_session_with_the_call_so_far`                                     | pass, the seed holds the call's text and reaches `run_clip`, not the session |
| The seed goes in after the settings       | `SessionOrderTest.test_the_seed_goes_in_after_the_settings`                                     | pass, against a fake that refuses as llama.cpp does                          |
| A refused update ends the turn            | `SessionOrderTest.test_a_refused_update_ends_the_turn_with_the_servers_words`                   | pass, the server's text, no timeout                                          |
| A new prompt starts a new session         | `test_a_new_prompt_mid_call_starts_a_new_session`                                               | pass, seeded with the call so far                                            |
| A barge-in keeps the session              | `test_speech_during_the_reply_interrupts_it`                                                    | pass, session open, next turn on it                                          |
| The hook adds to a caller's session       | `test_a_callers_session_is_added_to_and_left_open`                                              | pass, settings sent once, not closed                                         |

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
.\.venv\Scripts\python client.py --input "<mic>" --output "<speakers>"
```

For a speaker call, use the built-in microphone with its driver effects enabled in Windows. The client uses those
effects by default. For a headphone call, add `--raw-mic` if you want to bypass the effects.

If silence detection misbehaves, add `--ptt` and press Enter to start and stop talking.

**Swap the prompt.** Write Mason's chosen persona to `$VOICE_HOME/live/prompt.txt`. The next turn uses it, on a new voice session seeded with the call so far. The record names the file and its hash. Delete the file to go back to the built-in persona.

**Stop everything.** Press Ctrl+C in the laptop window. On the Desktop, press Ctrl+C in the server window, or run `touch "$VOICE_HOME/live/STOP"`. The server stops llama-server and the MCP time server on the way out, and the lease ends with it. Check with `python ~/.gpu/gpu_lease.py status`. A run started with `--stop-file PATH` watches that file instead, so a worker's check cannot stop Mason's demo.

## Runbook for a call with T3

This call starts and asks about T3 threads by voice. The threads run in the fork's own T3 environment (`fork-env.md`), never in the daily install. Run every command from the repo checkout, in Git Bash.

**Bring up the fork environment.** The checkout needs `pnpm install --frozen-lockfile` once. Mint the voice session while the environment is down, then start it. The first start builds for a few minutes.

```bash
python -m voice.t3.fork_env token   # only if $VOICE_HOME/t3-fork-home/t3-seam-token is missing or older than 30 days
python -m voice.t3.fork_env up
python -m voice.t3.fork_env status  # state: up, and the web port the pairing link uses
```

**Set the model.** Threads run on the sandbox's default model, which is Replay (dev) until it is changed. Set it while the environment is down, then bring it up again. `model devReplay/showcase` puts Replay back.

```bash
python -m voice.t3.fork_env down
python -m voice.t3.fork_env model <provider instance>/<model>
python -m voice.t3.fork_env up
```

**Start the live server with T3 on.** LFM3 drafts each T3 line in a separate text-only session. The call's own voice says the draft, and the server checks both texts before playing the audio. Thread news waits for a quiet moment. `VOICE_ENV_FILE` supplies the Liquid key for the speech check. [Spoken acts](spoken-acts.md)

```bash
export VOICE_ENV_FILE=<path to the .env that holds LIQUID_API_KEY>
python ~/.gpu/gpu_lease.py run --holder voice-live --purpose "call with T3" --max-minutes 60 -- \
  "$VOICE_HOME/venv-lfm3/Scripts/python" -m voice.live.server --t3 on --decider liquid --max-minutes 55
```

**Pair Mason's apps.** The link expires in 5 minutes, so mint it just before he links the environment in Settings > Connections. It is written to `$VOICE_HOME/t3-fork-home/pairing-url.txt` and never printed. Hand it to him without opening it.

```bash
python -m voice.t3.fork_env pair
```

**Shut it all down.** Stop the live server as in the runbook above, then stop the fork server and check that the GPU is free.

```bash
touch "$VOICE_HOME/live/STOP"
python -m voice.t3.fork_env down
python ~/.gpu/gpu_lease.py status
```

The rehearsal on synthetic audio, its transcript, and what it cannot show are in the seam unit 10 PR.

## Limits

- The voice session keeps the call, but the ASR, segmenter, task service and tools session still start fresh on every turn. A follow-up whose meaning lives in the task layer ("And what about Saturday?") reaches that layer as its own words only, so the task it creates may be missing a place or a day the person named earlier. The voice model remembers; the task service does not.
- A snapshot cannot be deleted from llama.cpp's conversation. Each turn adds one as a user text item, so a long call fills its slot with snapshots as well as speech. The instructions say only the newest one is current, and `--call-max-turns` bounds the rest.
- Echo cancellation on speaker calls depends on the microphone driver's support and Windows settings.
- The endpointer listens to loudness only. A cough starts a turn, and a long thinking pause ends one.
- `gated` opens a tool session as well as the voice and ASR sessions. At this head that put the first reply audio 0.32 to 0.70 s after the audio ended (measured).
- The CPU tests stand in for LFM3, except one, which drives the real `run_clip` against a scripted realtime server to cover the event seam. The shared-session tests stand in for the same socket. None of them covers LFM3's replies, its memory or its timing. Only a real-GPU run does.
- The current-head loopback ran on loopback, so the Tailscale hop and the firewall rule are unexercised at this head (measured: bind address in `server.out`).

## Open decisions

- The prompt. Mason's best prompt from the kit replaces the built-in persona through `prompt.txt`. The kit's campaign 2 stays held for the demo. It is also what decides whether this session's memory reaches the reply: with the current prompt the model answers the snapshot and repeats the current question, and instructions that push it to answer from the conversation made it narrate in the third person instead (measured, two runs of the script below). The persona is Mason's choice, not the lane's.
- Whether 700 ms is the right silence. Mason's own pauses will tell. `--silence-ms` changes it per run.
- Whether a call should also give the task layer the call's earlier text. That is what "And what about Saturday?" needs to become a weather task for Denver, and it belongs to the segmenter and tool lane, not to this one.
- `--call-max-turns 8` is a judgment, not a tuned value. The realtime path does not report its slot's token count, so what a turn costs was not measured; 8 turns of audio, snapshots and replies at 11,008 tokens a slot is the estimate behind it.
