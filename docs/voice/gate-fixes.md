# Gate fixes: use the tasks already listed, and show the ones the utterance took back

`gated` lost 8 expected tasks to `decider` on the 14 clips of PR #12 (measured there, 18 of 26 tasks and 7 of 14 clips). The three fixes below target the recorded causes in `voice/segmentation`. None of them has run against LFM3 yet, so every effect claimed here is inferred. The code is frozen in this branch before Mason's clips run, and the validation commands are at the end.

The design is fixed now, not tuned to Mason's recordings. The only bar in the code is the existing 60% content-word overlap, unchanged. No prompt wording names a clip. The recorded failures below set the mechanism each fix must address; the size of the gain is unknown until the runs.

## What changed

### 1. A delegate uses the task ids already listed

`Orchestrator.delegate_from_voice` now links a voice delegate to every live task whose content words it covers at 60%, when the side-effect claim does not contradict that task. A delegate that covers no task still becomes one new task, as before.

The recorded cause it targets is a whole-utterance re-delegation. LFM3-Audio-2B often repeats the whole utterance instead of running the tasks in the snapshot (PR #12, recorded on `05_topic_change`, `09_run_on`, and `long`). The old rule linked a delegate to at most one task, and any tail word the task lacked forced a new combined task. On `05_topic_change` that new task stood in for both requests, and the milk task stayed pending. On `long` the combined task became the only task the delegate touched, while the five tasks in the snapshot stayed as they were.

Two rules are unchanged. "delete the report" still never links to "find the report", because it shares one of the task's two content words. A read-only claim about a side-effect task still links and keeps the hold; it no longer runs the task even when `run_linked` is set, which the old code allowed.

A linked task runs on a delegate only when it has not run, waits on no dependency, and the delegate's claim does not contradict it. A task the service left for clarification is not run by a restatement, because the restatement adds no answer.

Words the delegate brings from the unreviewed tail that no linked task covers become one new task made from the tail, so a correction's replacement words are not dropped. The cost is that the tail task repeats any tail words a linked task already shows (inferred from the code, visible in the tests).

### 2. A task the utterance took back is visible

The tools snapshot now lists a task the decider withdrew or superseded with status `cancelled`, and with a note when the snapshot carries notes. It used to leave such tasks out. `amend_task` accepts a withdrawn task and revives it as a new task with `revises` set, and it resolves a superseded task to its newest successor.

This targets `04_correction` and `correction`. On both, the decider withdrew the first reminder or timer during speech, and the replacement words came in the tail. The snapshot left the withdrawn task out, so LFM3 delegated a new task with no `revises` (recorded in PR #12). Expected task T2 requires `revises` on both clips.

### 3. The wrong cancel on clip 08 follows from the same visibility

On `08_cancel`, the gate ran the calendar search during speech, and the decider withdrew the text to Sam. The snapshot held only the running calendar task, so LFM3 cancelled that one when the person took back the text (recorded in PR #12). The snapshot now shows the text task as `cancelled`, which gives LFM3 the task the take-back was about. No new rule refuses a cancel: the diagnosis is that the right target was invisible, and the fix is to make it visible (inferred).

## Tests

The offline suites pass with no GPU, key, or network:

```
python -m unittest discover -s voice/segmentation/tests -t .
python -m unittest discover -s voice/prototype/tests -t .
```

96 tests and 44 tests. The new cases cover a delegate that links to two tasks and runs both, a delegate that covers one task and makes the tail task, a delegate that covers nothing and makes one task, a read-only claim that leaves a side-effect task held, a task left for clarification and a task waiting on its dependency, the delete-versus-find refusal, amend of a withdrawn task, amend of a superseded task through its successor, and both snapshot cases (a corrected task and a task taken back, each listed as `cancelled` beside the live one). Mutations that drop the tail task, link only one task, lower the overlap bar, or drop the side-effect guard each fail the suite (checked).

## What the runs must show

For each of the 14 clips, the same table as PR #12: first reply audio, expected tasks correct, clips passed, side-effect requests that ran, inventions, the share the gate ran, and decider calls per utterance. The mechanism each fix must change, and what a pass would need to show:

- `05_topic_change`: the milk task links and runs, and the drive words become their own task, so T1 has a dispatched task. The stale partial task `task_2` stays proposed and may still fail the clip as an extra, which this branch does not touch (measured in the record).
- `09_run_on`: the Robo Rock tail words become their own task, so T3 has a dispatched task. T1 and T2 stay on the proposed lights task while the delegate claims read-only, so those two stay failed (inferred).
- `long`: the coffee tail words become their own task, which T5 already matched through the combined delegate in the old run (measured). T4, the dentist reminder, stays proposed on the read-only claim (inferred). A task left for clarification is not run by a restatement, so `task_1` stays `needs_clarification` (a unit test pins this).
- `04_correction` and `correction`: the cancelled task is visible, so LFM3 can amend it. Whether it does is the model's decision and cannot be known before the run.
- `08_cancel`: the taken-back text task is visible, so LFM3 has a target and no reason to cancel the calendar search. Again, the model's next call decides.
- `03_dependent`: unchanged. The delegate and the stub task share no content word, so the dependency still comes from the decider's partial-word answer (measured from the record).

An offline replay of the recorded delegate calls against the recorded snapshot states (not a model run) confirms the code's side of this: `05_topic_change` links the milk task and makes `drive to Boulder?`, `09_run_on` makes `start the Robo Rock.` and leaves the lights task, `long` makes `more coffee for the office.` and leaves the clarification, and `03_dependent` creates the same combined task as before. The replay rebuilds statuses from the snapshot, so it cannot score a clip. Whether the count moves is unknown until the GPU runs.

## Validation commands

Synthetic clips first, then Mason's. Both under the GPU lease and `quiet_run.py`, with `VOICE_ENV_FILE` set to the env file that holds the key. The synthetic set is `single`, `two_request`, `correction`, and `long` from `$VOICE_HOME/evidence/stage1/audio/`.

```
python -m voice.prototype.run SYNTHETIC... --eou gated --fresh --out "$VOICE_HOME/evidence/eou/fix-tune"
python -m voice.prototype.eou_report before="$VOICE_HOME/evidence/eou/r2-gated" fix="$VOICE_HOME/evidence/eou/fix-tune"
```

Only if the synthetic pass shows no regression on its four clips, run all 14:

```
python -m voice.prototype.run CLIPS... --eou gated --fresh --out "$VOICE_HOME/evidence/eou/fix-r1"
python -m voice.prototype.eou_report before="$VOICE_HOME/evidence/eou/r2-gated" fix="$VOICE_HOME/evidence/eou/fix-r1" decider="$VOICE_HOME/evidence/eou/r2-decider"
```

CLIPS is `$VOICE_HOME/audio/real/01_single.wav` through `10_noisy.wav`, then the four synthetic clips above. The budget is the existing `DECIDER_BUDGET`, which holds 390 attempts and 323 used after the backlog round; the backlog round's 14 clips cost 33 decider attempts in the gated arm (measured).

## Limits

- Nothing here is measured against LFM3. The unit tests fix the code's behavior, not the model's.
- The tail task can repeat words a linked task already covers. If LFM3's delegate text and the tail overlap, the score may count an extra task.
- Every cancelled task stays in the snapshot for the rest of the conversation, so the tools session sees a growing list. The alternative is a window over the last utterance, which is a product decision.
- The 60% overlap bar was picked on the tuning data in PR #6 and is not refit here.
