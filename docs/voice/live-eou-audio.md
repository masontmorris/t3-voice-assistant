# End of turn by ear: the audio decider in the live server

**Short answer.** `--eou audio` asks the local d1-Omni-2B whether Mason has finished at 150, 300 and 500 ms of
silence and ends the turn at the first P(done) of 0.5 or more, falling back to today's 700 ms. On a replay of his real
call it cut the silence he waits through from 0.70 s to 0.22 s at the median and brought the first reply audio 0.38 s
earlier, and it cut three of his 37 turns mid-thought. `--eou silence`, the default, is today's behaviour and gave 37
turns and no cuts on the same replay.

Every number below is **measured** on this machine unless it is marked **inferred** or **guess**. The replay harness
and its scores are under `$VOICE_HOME/evidence/eou-audio/` and are not committed.

## Goal and smallest change

Mason wants the assistant to answer as soon as he has finished, without cutting him off mid-thought. Today a turn
ends after `--silence-ms`, 700 ms, of silence. That wait exists because the live transcript's last word lands a median
0.69 s after speech ends (`docs/voice/audio-decider.md`). The smallest change that could meet the goal is one extra
question at the end of speech, asked of the audio itself, replacing part of that fixed wait and leaving the 700 ms as
the ceiling. Nothing else in the pipeline changes.

## What is shipped

- `voice/live/eou_audio.py` asks one question, worded as the study worded it, of d1-Omni-2B Q8_0 on its own local
  llama-server, with the study's flags (`-np 1 -b 8192 -ub 2048 -fa on -c 8192`). The audio is the turn's last 3 s.
  No transcript is sent: adding the stale one dropped the study's AUC from 0.955 to 0.690. The audio never leaves
  this machine, and `--eou audio` starts and stops that server with the call's LFM3.
- `voice/live/endpoint.py` gained `check_ms`, the points of silence at which it emits a `Check` (150, 300 and 500 ms
  by default), and `finish`, which ends the turn there and stops counting silence. The 700 ms `End` is unchanged.
- `voice/live/server.py` asks the model at each `Check`, one question in flight per turn, and ends the turn when the
  answer is at or above theta. An answer that arrives after the person spoke again ends nothing, and neither does one
  that arrives after the turn ended. Each check's deadline is the silence left before 700 ms, capped at 500 ms, so a
  slow or broken model costs no turn: the silence rule ends it as today.
- `--eou silence` is the default and adds no check points, so no request is ever made.

## The policy replay on real audio

Method. Mason's 37 recorded turn inputs from the 2026-11-16 15:26 call are replayed into one live server session
through the live client, in order, each clip once the previous reply has played, with `--decider liquid --mock-tools`
and the recorded call's speech. Two runs, one per `--eou` mode, on the same worktree, clips, decider and mock tools
(`$VOICE_HOME/evidence/eou-audio/run_replay.py`). Each server turn is then placed in the recorded clips by
exact sample match of its own input audio, so a turn that ends early or covers two clips is placed by content, not by
order (`score.py`). "Silence" is measured on the turn's own audio: the samples after its last speech frame. It is not
the server's nominal number, which understates the wait whenever an answer came back late.

| | `--eou silence` | `--eou audio` |
|---|---|---|
| Turns produced (37 expected) | 37 | 39 |
| Clips with exactly one turn | 35 of 37 | 34 of 37 |
| Turns that ended with speech still to come in the clip (mid-thought cuts) | 0 | 3: clips 4, 6 and 10 |
| Silence from speech end to turn end, median / p90 | 0.70 s / 0.70 s | 0.22 s / 0.70 s |
| Time to first reply audio, median / p90 | 2.04 s / 2.36 s (n 37) | 1.66 s / 2.12 s (n 37) |
| VRAM during the call | 3.13 to 3.18 GB | 5.80 to 5.87 GB |

- **The saving is real and close to the study's inference.** Median first audio came 0.38 s earlier. The study
  inferred about 0.35 s from silence alone. Measured here, 33 of 39 turns ended early: 30 at the 150 ms check, 2 at
  300 ms, 1 at 500 ms. Six fell back to 700 ms, which is why p90 does not move.
- **The model is fast enough.** 114 checks in the run: 63 ms median, 207 ms p90, 357 ms worst, no errors and no
  timeout. The first turns of a call pay more, 163 to 336 ms, while LFM3 is still warming up; those answers still land
  before 700 ms, so they cost silence saved and nothing else.
- **It cuts him about once in twelve turns.** Three of 37 clips ended while he was still talking, each at a pause the
  model called done at the 150 ms check (P(done) 0.81, 0.58, 0.68). Two of the three were then carried into the next
  turn, which the server logs as a continued turn. This is much worse than the study's replay suggested, and the
  reason is in the study's own scoring: it counted a pause as cut only when a check fired early, and compared that
  against pauses of 700 ms or more, which are two different things. Its "turns 8 and 9 would not be cut" needs the
  longer hold it did not test, not this policy.
- **Turns 8 and 9 as recorded are still cut.** At their 700 ms boundaries the model's highest answers were 0.41 on
  clip 8 and 0.07 on clip 9, all under theta, so the 700 ms fallback ended those turns exactly as today. The audio
  signal to hold is there; the policy does not use it.

## Cost

- **VRAM.** d1-Omni-2B Q8_0 adds 2,655 MiB over idle at load and 2,689 MiB after a run (measured alone, matching the
  study's 2,659). LFM3 with this branch's three slots used 2,361 to 2,407 MiB during a call, so both together ran at
  5.80 to 5.87 GB of the card's 12 GB.
- **Latency.** The question is asked while silence is running, so it costs the turn nothing unless it is slower than
  the silence left before the fallback. The deadlines are 500, 400 and 200 ms at the three checks.
- **Startup.** The second llama-server adds 2.0 s to a call's startup.

## What still fails

- **Mid-thought cuts.** Three of 37 on the real replay, all from P(done) just over 0.5 at 150 ms. Raising theta would
  trade cuts for fewer early ends; theta is not tuned here beyond the study's 0.5.
- **Theta is a single fixed value.** The study measured hosted answers varying by 0.027 standard deviation on repeat;
  the local model was not measured for spread here, so a margin around theta is still unmeasured.
- **The hold lives behind `--eou smart`.** `--eou audio` can only end a turn early or at 700 ms, so a pause of 700 ms or
  more is still a turn boundary under it. `Endpointer.hold` and Smart Turn on the CPU keep such a turn open to 2 s, and
  the measurement of what that costs and saves is in `docs/voice/audio-decider.md`.
- **Two of the recorded clips (29 and 34) cannot be placed** by the scorer in either run, because they are short and
  mostly silence. Their turns are counted in the totals and not in the per-clip lines.
- **One replay, one afternoon, one speaker.** The replay is also not the microphone path: no echo, no room noise and
  no AEC. Real calls will see the checks fire on noise the replay does not have.

## Reproduce

```
python $VOICE_HOME\evidence\eou-audio\run_replay.py silence   # under the GPU lease
python $VOICE_HOME\evidence\eou-audio\run_replay.py audio
python $VOICE_HOME\venv-lfm3\Scripts\python.exe $VOICE_HOME\evidence\eou-audio\score.py silence
python $VOICE_HOME\venv-lfm3\Scripts\python.exe $VOICE_HOME\evidence\eou-audio\score.py audio
python -m unittest discover -s voice/live/tests -t .
```
