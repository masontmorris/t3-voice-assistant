# Gate fixes: use the tasks already listed, and show the ones the utterance took back

`gated` lost 8 expected tasks to `decider` on the 14 clips of PR #12 (measured there, 18 of 26 tasks and 7 of 14 clips). The three fixes below target the recorded causes in `voice/segmentation`. None of them has run against LFM3 yet, so every effect claimed here is inferred. The code is frozen in this branch before Mason's clips run, and the validation commands are at the end.

The design is fixed now, not tuned to Mason's recordings. The only bar in the code is the existing 60% content-word overlap, unchanged. No prompt wording names a clip. The recorded failures below set the mechanism each fix must address; the size of the gain is unknown until the runs.

## What changed

### 1. A delegate uses the task ids already listed

`Orchestrator.delegate_from_voice` now links a voice delegate to every live task whose content words it covers at 60%, when the side-effect claim does not contradict that task. A delegate that covers no task still becomes one new task, as before.

The recorded cause it targets is a whole-utterance re-delegation. LFM3-Audio-2B often repeats the whole utterance instead of running the tasks in the snapshot (PR #12, recorded on `05_topic_change`, `09_run_on`, and `long`). The old rule linked a delegate to at most one task, and any tail word the task lacked forced a new combined task. On `05_topic_change` that new task stood in for both requests, and the milk task stayed pending. On `long` the combined task replaced the five tasks LFM3 could have run.

Two rules are unchanged. "delete the report" still never links to "find the report", because it shares one of the task's two content words. A read-only claim about a side-effect task still links and keeps the hold; it no longer runs the task even when `run_linked` is set, which the old code allowed.

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

94 tests and 44 tests. The new cases cover a delegate that links to two tasks and runs both, a delegate that covers one task and makes the tail task, a delegate that covers nothing and makes one task, a read-only claim that leaves a side-effect task held, the delete-versus-find refusal, amend of a withdrawn task, amend of a superseded task through its successor, and both snapshot cases (a corrected task and a task taken back, each listed as `cancelled` beside the live one). A mutation that drops the tail task and a mutation that links only one task each fail the suite (checked).

## What the runs must show

For each of the 14 clips, the same table as PR #12: first reply audio, expected tasks correct, clips passed, side-effect requests that ran, inventions, the share the gate ran, and decider calls per utterance. The specific claims to check first:

- `05_topic_change`: the milk task runs and the drive request becomes its own task, so T1 passes and T2 keeps passing (inferred from the record).
- `09_run_on`: the Robo Rock tail task runs, so T3 passes; T1 and T2 still fail if the read-only claim keeps the lights task held (inferred).
- `long`: no expected change. T4, the dentist reminder, stays held on the read-only claim, and T1, T2, T3 and T5 already pass (measured from the record).
- `04_correction` and `correction`: LFM3 amends the visible cancelled task, so T2 carries `revises` and passes (inferred).
- `08_cancel`: LFM3 does not cancel the calendar search, so T1 passes (inferred).
- `03_dependent`: unchanged. The delegate and the stub task share no content word, so the dependency still comes from the decider's partial-word answer (measured from the record).

Those five gains would take 18 tasks to 23 of 26, and 7 clips to 11 of 14: `04_correction`, `05_topic_change`, `08_cancel` and `correction` would pass, while `03_dependent`, `09_run_on` and `long` would still fail one task each. That is a prediction, not a result.

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

CLIPS is `$VOICE_HOME/audio/real/01_single.wav` through `10_noisy.wav`, then the four synthetic clips above. The budget is the existing `DECIDER_BUDGET`, which holds 390 attempts and 323 used after the backlog round; this round's 14 clips cost 33 decider attempts in the gated arm (measured).

## Limits

- Nothing here is measured against LFM3. The unit tests fix the code's behavior, not the model's.
- The tail task can repeat words a linked task already covers. If LFM3's delegate text and the tail overlap, the score may count an extra task.
- Every cancelled task stays in the snapshot for the rest of the conversation, so the tools session sees a growing list. The alternative is a window over the last utterance, which is a product decision.
- The 60% overlap bar was picked on the tuning data in PR #6 and is not refit here.
