# Echo cancellation for the live client

## Why

The live assistant plays replies through speakers unless headphones are plugged in. The server decides turns from
the microphone, so it heard its own reply, sent `barge_in`, and the reply stopped itself. The goal is that speaker
playback never starts or interrupts a turn, while a person who talks over the reply still gets through.

This branch does that in the client, before the microphone audio leaves the laptop. It needs no server change and
no new dependency. The server's turn, barge-in and transcript paths stay exactly as they are.

## What ships

| File | State | What it is |
| --- | --- | --- |
| `voice/live/aec.py` | New | The canceller. `EchoCanceller.playback` takes the played reply, `process` cleans one microphone frame, `stats` reports counts. |
| `voice/live/client.py` | Changed | Feeds the player's audio to the canceller and sends the cleaned microphone. Three flags. |
| `docs/voice/echo-cancellation-client.patch` | New | The `client.py` diff against reference head `1845b42978`. It applies cleanly with `git apply` to that head (measured). |
| `docs/voice/echo-cancellation.md` | New | This file. |
| `docs/voice/licenses.md` | New | The one dependency this branch touches, numpy, with no new package. |
| `voice/live/tests/synthetic.py` | New | Generated scenes. No recorded audio. |
| `voice/live/tests/test_aec.py` | New | 18 tests: echo-only, interruptions, rate and frame handling. |
| `voice/live/tests/test_client_echo.py` | New | 10 tests: flag selection, player feed, send path, failure guard. |
| `voice/live/tests/echo_bench.py` | New | Rerunnable check that writes WAVs and a JSON summary. |
| `voice/live/recording.py`, `endpoint.py`, `requirements-client.txt`, `__init__.py` | Copied | Unmodified copies of reference head `1845b42978`, added so the tests can run in this checkout. See "Parent checkout" below. |

## How it works

The reference is the PCM the player's output callback handed to the audio device, stamped with that callback's
`time.perf_counter()`. The canceller stores the last four seconds of it indexed by time, so each 20 ms microphone
frame can read the reference window that could have produced its echo. Both streams are on one clock, so the
output buffer, the acoustic path and the input buffer appear as one delay inside the filter.

A block-normalized least-mean-squares filter learns that echo path from the reference window and subtracts its
estimate from the microphone. The filter is 256 ms long by default. That is the `--echo-tail-ms` knob.

A linear filter cannot subtract the person's voice, and the filter's residual can still clear the server's
endpointer. The canceller therefore decides per frame whether the frame is mostly echo. The decision uses the echo
return loss, which is the microphone level minus the reference level on the same frame. That ratio is a property of
the room, the speaker and the microphone, so it stays steady while the reply's own loudness moves. A person
talking raises it. The canceller learns the ratio from the first reply, then flags a frame as the person's speech
when the ratio rises more than `erl_margin_db` (3 dB) above the learned value. A flagged frame is sent with only
the learned echo subtracted, so an interruption keeps its level. An unflagged frame is attenuated toward -60 dBFS,
which keeps the endpointer quiet.

The first reply does not have a learned ratio yet. Its opening is suppressed for `warmup_ms` (250 ms) at least,
until the ratio has stopped rising for `warmup_settle_ms` (160 ms), and never longer than `tail_ms` plus 400 ms,
with a 600 ms floor. During that opening only a frame more than `warmup_boost_db` (6 dB) louder than the playback
passes, because that cannot be echo. Later replies use the learned ratio from their first frame. When playback
stops, the last gain is held for 150 ms to cover the room's echo tail.

## What was measured

All numbers below come from generated scenes in this checkout on 2026-11-16. The scenes use seeded speech-shaped
noise as the reply and as the person, a random decaying echo path, and -65 dBFS room noise. Playback is fed at 24 kHz
because that is the live client's output rate; the microphone is 16 kHz, as on the real link.

**Echo only.** Six seconds of reply with no person. The last 3.5 s are scored. The endpointer in `endpoint.py` runs
on what the client would send.

| Scene | Echo at mic | Sent | Start? |
| --- | --- | --- | --- |
| Echo -40 dBFS | -39.9 | -60.0 | none |
| Echo -32 dBFS | -31.9 | -60.0 | none |
| Echo -26 dBFS | -25.9 | -60.0 | none |
| Echo -20 dBFS | -19.9 | -60.0 | none |
| Echo -14 dBFS | -13.9 | -60.0 | none |
| 120 ms echo delay | -25.9 | -60.0 | none |
| 220 ms echo delay | -25.8 | -54.0 | none |
| 100 ms room tail | -25.9 | -60.0 | none |
| Another random path (3 seeds) | -25.9 | -60.0 | none |
| Reply with playback gaps | -26.0 | -58.6 | none |
| Room path changes at 5 s | -25.9 | -44.5 | none |

The filter alone reaches 11.5 to 12.6 dB of echo return loss enhancement on these scenes (measured, `erle_db` in
the stats). The suppressor takes the rest.

**Interruptions.** The person starts at 2.00 s and speaks for one second.

| Echo / person | Start | Onset held | Sent minus raw during speech (2.2 to 2.9 s) |
| --- | --- | --- | --- |
| -32 / -24 dBFS | 2.14 s | 40 ms | -0.8 dB |
| -26 / -18 dBFS | 2.14 s | 40 ms | -0.7 dB |
| -20 / -12 dBFS | 2.14 s | 40 ms | -0.7 dB |
| -14 / -8 dBFS | 2.14 s | 40 ms | -1.2 dB |

The 140 ms start is mostly the endpointer's own 100 ms `start_ms` window, plus the 40 ms the level ratio takes to
move (measured).

**Other cases.**

| Case | Result |
| --- | --- |
| Headphones, person at 2 s | Start at 2.10 s, sent level within 0.1 dB of the raw microphone |
| Person talks inside a playback gap | Start at 3.10 s |
| Person at the same level as the echo, or 2 dB above it | No start (limitation below) |
| Person starts 50 ms into the first reply | No start (limitation below) |

## Verification

`python -m unittest discover -s voice/live/tests -t .` runs 28 tests and passes in 3.7 s on this machine (measured).
The reference client's own 9 recording tests also pass unchanged against this client (measured; the two missing
constructor arguments are read with `getattr`). `python -m voice.live.tests.echo_bench --out <dir>` writes the raw
and sent microphone and the played reply for five scenes, plus `summary.json`, and exits 1 if any expectation
fails. Both run on the CPU, open no audio device, use no network, and read no recording.

Evidence from the final run is in `$VOICE_HOME/evidence/echo/20261116-synthetic-final` (4.8 MB). It holds 25 WAVs,
`summary.json` and `measurements.json`. Listen to `interruption-conversation-raw.wav` against
`interruption-conversation-sent.wav` for the barge-in case, and `echo_only_loud-conversation-sent.wav` for what the
call sounds like when the server would hear the reply only once.

## Limits

- **No real speakers or microphone were used.** Everything above is synthetic. How it behaves on this laptop's
  speakers, at Mason's volume, is a guess until one run is made with the real client. The confidence is in the
  mechanism and the synthetic margins, not in an acoustic measurement.
- **A person quieter than the echo plus 3 dB can be suppressed.** At the same level as the echo the frame is not
  distinguishable by level, and the tests show no barge-in. A person has to be a few dB above the echo. Measured on
  the synthetic scenes.
- **The first reply's opening is protected at the cost of an early interruption.** A person who starts talking in
  the first 250 ms of the very first reply, or before its echo loss settles, is held down until the ratio settles,
  and the turn is late or absent. This is measured in the synthetic scenes and is the price of not knowing the room
  yet.
- **About the first 40 ms of any interruption is attenuated** while the level ratio moves. The turn still starts,
  but the first syllable is quieter in the server's audio (measured).
- **An echo path longer than `--echo-tail-ms` is not cancelled.** 220 ms was covered with the default 256 ms. A
  Bluetooth speaker with a large delay would need a larger `--echo-tail-ms`; the warmup cap follows it
  automatically. Inferred from the delay tests.
- **Speaker distortion is not linear.** Loud playback and cheap speakers add harmonics the filter cannot subtract.
  The onset detector still works on levels, so barge-in protection should hold, but how much echo reaches the
  server's ASR in that case is a guess.
- **No timing claim is made.** The shared Desktop was running the live demo and holding the GPU lease, so no
  `quiet_run` measurement was taken. The per-frame work is one 4,096-tap correlation plus one weight update, and
  the CPU cost is not reported here.
- **The client records only what it sends.** `recording.py` still says the microphone track is the PCM sent to the
  server, which is now the cleaned frame. The client log's `reply_end` and `summary` entries carry the canceller's
  counts and levels but no audio. Hearing the raw and sent tracks needs `echo_bench.py`.

## Integration

Reference is the separate `voice/live-corrections` checkout at head `1845b42978`. It was read only. Nothing in it
was changed, and its demo was not touched.

**Parent checkout.** This branch's worktree started from the old local repository at `2985806`, which has only
`docs/plan.md` and `README.md`. It has no `voice/` directory at all, so the live client and server could not be
imported here. The reference's `client.py`, `recording.py`, `endpoint.py` and `requirements-client.txt` were copied
at the reference head so the client wiring and the endpointer could be tested in this checkout. Of those copies,
only `client.py` is changed. Port `aec.py`, the `client.py` change and the tests; leave the other copies to the
fork, which already has them.

**Steps.**
1. Copy `voice/live/aec.py` into the fork.
2. Apply `docs/voice/echo-cancellation-client.patch` with `git apply` in the fork, or port the hunks by hand. It
   is measured to apply to `voice/live/client.py` at `1845b42978` without conflict.
3. Copy the four test files into `voice/live/tests/`.
4. Run `python -m unittest discover -s voice/live/tests -t .` from the repository root.
5. Add `aec.py` to the laptop zip and to the list in `voice/live/SETUP-LAPTOP.md`.
6. Replace the "Use headphones" warning in `SETUP-LAPTOP.md` and the "There is no echo cancellation" line under
   "Limitations" in `docs/voice/live-demo.md`. Suggested text for both: "The client cancels speaker echo before it
   sends the microphone, so speakers work. Headphones still give the cleanest signal. `--no-echo-cancel` sends the
   raw microphone."

**Behavior on the laptop.**
- `--echo-cancel` is on by default when the microphone is the input. It is off with `--wav` unless passed.
- `--no-echo-cancel` restores the earlier raw-microphone path.
- `--echo-tail-ms` raises the modeled echo path from 256 ms.
- The client prints `echo cancellation on: 256 ms echo path` at start.
- Each `client.jsonl` `reply_end` and the final `summary` carry an `echo` object with `frames`, `playing_frames`,
  `tail_frames`, `double_talk_frames`, `suppressed_frames`, `erle_db`, `echo_loss_db` and `last_gain_db`.
- If the canceller raises, the client prints one line, logs `echo_failed`, sends the raw microphone and continues.
  A failure in the player feed disables the feed without raising on the audio callback.

**Server.** No change. The server still ends turns on silence and still mutes a reply on speech. The only
difference is that its microphone no longer contains the reply.

**Tuning.** On the real hardware, if replies still barge in, raise `--echo-tail-ms` first. If a person's
interruptions are missed, raise the constructor's `erl_margin_db` above 3 or lower `suppression_target_dbfs`; these
are not CLI flags yet.

## Cost and licenses

$0. No paid API, no new account, no GPU lease, no model weights. Only numpy, which the client already pins, plus
the Python standard library. No real user audio was read or written; every test signal is generated from seeded
noise. See `docs/voice/licenses.md`.

## Decisions for Mason

1. **On by default for the microphone.** A laptop with speakers works out of the box. `--no-echo-cancel` keeps the
   old raw path, and `--wav` runs are unchanged. The cost is that every microphone run now goes through the
   canceller.
2. **Client side, not server side.** The client owns the played samples and the playback clock. The server sends
   the reply faster than it plays, so its own timeline cannot align with the echo.
3. **No server flag.** The server cannot see playback, and adding a playback gate would raise the latency of real
   interruptions. The client's cleaner audio keeps the server's 100 ms turn start.
