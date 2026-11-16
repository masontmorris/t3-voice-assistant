# Loop archive

A loop archive is one live run, packed so a template can replay the loop in time with the audio. `voice/archive/export.py` builds it from what a run already saves. `voice/archive/validate.py` checks it. The schema is `voice.loop.archive/1`, defined in `voice/archive/schema.py`.

Archives hold real voices, so they live under `$VOICE_HOME/archives/` and never in git. The repo has the code and one synthetic archive, `voice/archive/example/`.

## Build and check one

```bash
python -m voice.archive.export --server "$VOICE_HOME/evidence/live/<run>" --client <client session dir>
python -m voice.archive.validate "$VOICE_HOME/archives/<run>"
```

`--server` is the directory of `turn_NNN.json` records and turn WAVs that `voice.live.server` writes. `--client` is the session directory that `voice/live/client.py` records, with `manifest.json`, `client.jsonl` and the stems. The validator exits 1 on any error.

To rebuild the synthetic example, run `python -m voice.archive.synthetic`. It writes the same files every time, and a test checks that.

## Layout

| Path | Contents |
| --- | --- |
| `manifest.json` | Schema, duration, stems, per-turn clock offsets, reply placement, warnings, event counts, and the SHA-256 of every other file. |
| `timeline.jsonl` | One event per line, sorted by `t`. |
| `waveform.json` | RMS at 100 frames per second, separately for the microphone and the assistant playback. |
| `stems/` | `microphone.wav`, `assistant-playback-NNN.wav` and `conversation.wav`, copied from the client recording. |
| `clips/` | The server's per-turn input, reply and update WAVs. The validator uses them. |

## The replay clock

`t` is seconds from the first frame of the client recording, which is the clock of `conversation.wav`. Each server record has its own clock, so the exporter finds an offset per turn. It takes the last voiced second of the turn's input WAV and finds it in `stems/microphone.wav` by cross-correlation, near the time the client logged `turn_end`. The server sent that WAV's bytes to the model, and the client recorded the same bytes as they left. So the match is exact, and the turn's audio end fixes the offset.

The exporter places a reply where its WAV sounds in the playback stem. It searches from the client's `reply_start` log time to 12 s later, because a reply can wait in the client's queue behind the one still playing. A reply stops at a barge-in, at the next turn's reply, or at the end of the recording, whichever comes first. `reply_end.cut` names the reason.

## Events

Every event has `t`, `seq` (its line number from 0), `type`, `turn` and `src`. `src` is `server` for an event the server logged, `client` for one the client logged, and `derived` for one the exporter inferred. Task IDs are `<turn>/<server task id>`, because the server numbers tasks from 1 in every turn.

| `type` | Payload | Source |
| --- | --- | --- |
| `speech_start` | `audio` pointer to the turn's input clip | derived: input start plus the endpointer's pre-roll |
| `speech_end` | | derived: `turn_end` minus the trailing silence |
| `turn_end` | `trailing_ms`, `forced` | the end of the input audio on the microphone stem |
| `transcript_partial` | `revision`, `text` | ASR update |
| `transcript_final` | `revision`, `text` | ASR final |
| `decision_point` | `revision`, `is_final`, `source`, `segments`, `decisions` (kind, readiness, side effect and confidences per segment), `latency_ms` | decider call |
| `task_split`, `task_merged` | `before`, `after` | derived: the segment count rose or fell between two decision points |
| `gate_verdict` | `task_id`, `score`, `threshold`, `passed`, `weakest` | task history `gate` entry |
| `task_created` | `task_id`, `text`, `sources` | the earliest of task history, LFM's delegate call and the task service |
| `task_status` | `task_id`, `status`, optional `reason` and `result_summary` | task history and the task service |
| `handoff` | `final_transcript`, `unreviewed_speech`, `segments`, `snapshot` | end of utterance: the snapshot LFM sees |
| `lfm_action` | `calls` (name and arguments), `text`, `snapshot_after` | LFM's tool turn |
| `tool_call` | `name`, `arguments`, `result` | tool result |
| `reply_start` | `reply` (`snapshot` or `update`), `reply_id`, `text`, `audio` pointer | where the reply's WAV sounds |
| `reply_end` | `reply`, `reply_id`, `played_s`, `interrupted`, `cut` | derived |
| `barge_in` | | client log |
| `backlog_reprompt` | `task_ids` | reserved: `voice/main` does not log backlog re-prompts yet |

An `audio` pointer is `{"clip", "clip_offset_s", "stem", "at", "probe_s"}`. It says the clip, from `clip_offset_s`, sounds in `stem` at replay time `at`.

## What the validator checks

- The manifest names schema `voice.loop.archive/1`. Every file it lists exists and matches its SHA-256. Every stem's WAV length matches the manifest.
- Every event has a known type, a valid `src`, its payload fields, and a `seq` equal to its line number. `t` never decreases and never passes the archive's end.
- Within each turn, speech starts before it ends and ends before `turn_end`, the hand-off follows the final transcript, and LFM acts after the hand-off. A task is created before its first status, and each reply starts before it ends.
- For every `audio` pointer, it cross-correlates up to 1 s of the clip against the stem within 250 ms of `at`. A measured lag over 20 ms, or a normalized correlation under 0.5, is an error.

## Results

| Archive | Events | Audio checks | Max lag | Errors |
| --- | --- | --- | --- | --- |
| `voice/archive/example` (synthetic) | 57 | 7 | 0.1 ms | 0 |
| Desktop loopback, three clips from `$VOICE_HOME/audio/real` through `client.py --wav` | 117 | 9 | 0.4 ms | 0 |
| Mason's live demo, server run `20261116-080326` with the laptop's client recording, 6 turns, 91.7 s | 120 | 14 | 0.3 ms | 0 |

All three rows are measured. The loopback ran on 2026-11-16 at this branch's head, in a `voice-loopviz` GPU lease, with `02_two_requests.wav`, `11_tools.wav` and `04_correction.wav`. Two of its replies were cut by barge-ins.

Two independent checks agree with the placement (measured on the loopback). The client's `reply_start` log time, plus its measured playback delay, lands within 0.5 ms of where the reply's audio sounds, for the four replies that did not wait in the queue. The client's `turn_end` log time lands 11 to 26 ms before the end of the input audio. That is about one 20 ms frame, because the server ends a turn when the frame arrives and the exporter counts to the frame's end (inferred).

The demo export has two warnings (measured). In turns 1 and 5 the client logged a second reply that the server record does not hold, because a barge-in cancelled the turn and the record kept only its first reply. The client heard 0.23 s and 0.04 s of them. The archive leaves both out.

## Limits

- The alignment check uses the same correlation method as the exporter. It catches a wrong offset, a wrong clip, drift and a broken stem, but not a mistake shared by both. The two client log checks above are the independent evidence.
- A server event inside a turn is only as accurate as the server's own clock between that event and the turn's audio end. Every turn on that clock comes from one process, so the error is the server's scheduling jitter, not network time (inferred).
- A turn with no `turn_end` in the client log, or no `audio_end` in its record, cannot be placed. The exporter skips its events and records a warning.
- `task_split` and `task_merged` come from segment counts. They do not say which task a new segment became.

## Live hooks, for later

The exporter reads records after the fact because `voice/live/server.py` is changing on `voice/live-memory` and `voice/live-requests-only`. Once those land, two hooks would remove the correlation search. `MicTurn` in `server.py` can carry the turn's start on the client's clock, which the client already stamps on each frame. `Player._callback` in `client.py` can log the replay time of each reply's first played chunk.
