# Task updates during a reply: the backlog and the re-prompt

A task can finish while the voice model is still answering. Before this change the runner held every result until no task was still running, then sent one update. A result could wait seconds behind an unrelated task. This change records every status change in an ordered backlog, and when a reply ends it sends the results the person has not heard as one merged update. It repeats nothing the voice has already been given, and it changes nothing about task decisions. On the same 14 clips it delivered updates sooner without losing or repeating one (measured).

The numbers come from one run per arm over the same 14 clips: Mason's 10 recordings and Stage 1's 4 synthetic clips. Both arms ran with `--eou gated --fresh`, inside the GPU lease and `quiet_run.py`, with LFM3 at `-c 32768`. The before arm is PR #12's `gated` run, `$VOICE_HOME/evidence/eou/r2-gated`. The after arm is `$VOICE_HOME/evidence/backlog/after`. Each claim below says whether it is measured, inferred, or a guess.

## Result

| Measurement | before | after |
| --- | --- | --- |
| Finished tasks | 17 | 17 |
| Results that arrived during the first reply | 2 | 2 |
| Results that arrived during an update reply | 0 | 1 |
| Results that arrived in the tools turn | 2 | 2 |
| Results that arrived before the snapshot | 2 | 2 |
| Results that arrived while idle | 11 | 10 |
| Results never said (run-record model) | 0 | 0 |
| Results said twice or more (run-record model) | 0 | 0 |
| Arrival to first word, median / p95 / max (s) | 0.31 / 2.01 / 2.01 (n=13) | 0.24 / 1.07 / 1.07 (n=13) |
| Update turns | 10 over 10 clips | 12 over 10 clips |
| Expected tasks correct, of 26 | 18 | 18 |
| Clips passed, of 14 | 7 | 7 |
| First reply audio, median / p95 / max (s) | 0.62 / 0.86 / 0.86 | 0.60 / 0.78 / 0.78 |

All rows are measured, by `python -m voice.prototype.backlog_report` and `python -m voice.prototype.eou_report`. The task outcomes and the first reply did not move. The brief's scope predicts that, because the backlog changes only how a result is delivered.

The latency gain is in the tail. The before arm's worst wait was 2.01 s, on `two_request` task_1, which waited for task_2 to finish because the runner drained updates only when every task was done (measured, from the run record). The after arm's worst wait is 1.07 s, a result that landed during an update reply and went out when that reply ended (measured). Across the 13 deliveries the median falls from 0.31 s to 0.24 s (measured).

The cost is two more update turns, 12 against 10, because results that land while nothing is replying now go out at once instead of batching (measured). Each update turn is one LFM3 reply and one WAV.

PR #13 measured 1.63 s from the end of the user's speech to the update's first word on its three lookup clips (measured, in `docs/voice/real-tools.md`). This round cannot be compared with that number. It measures arrival to first word, and its audio-end figure is 4.75 s before and 4.74 s after, dominated by the longer first replies on these clips (measured). #13's finding that the wait is the first reply, not the lookup, still holds and is why the backlog helps only results that land late.

## How it works

`voice/prototype/backlog.py` holds the rule, and `voice/prototype/run.py` wires it in.

1. While the first reply generates, a collector task polls the mock task service every 50 ms, and also when a lookup returns. Each terminal or cancelled state it sees becomes an `Arrival` with the task id, the old and new status, the summary, the time, and whether it landed during the first reply, an update reply, or idle.
2. When the reply's `response.done` arrives, the runner drains the backlog and asks the rule what to tell.
3. The rule tells every `completed` or `failed` result whose task the voice has not already been given. The task service marks a task reported when a snapshot carries it, so a result the snapshot already showed is never repeated. A cancellation is never told, because the person or the voice model asked for it.
4. Everything to tell goes out as one `task_update` message, so several results merge into one re-prompt. The update reply's own arrivals go into the next backlog, and the loop repeats until nothing is left to tell and no task is still running.
5. A result that lands while nothing is replying is told at once, as its own update turn, where the old runner waited for every running task first.

The reply ends at `response.done`, as everywhere else in the prototype, not when its audio would finish playing. A listener hears the update after the first reply's speech ends (inferred, because the prototype does not play audio).

## What the rule cannot see

The rule is code, and it sees task state, not the reply. It never re-prompts for a result the snapshot carried. That is the conservative side of "never for a status the reply already stated", and it has a blind spot. A result in the snapshot that the reply never mentions is never re-prompted either. On `long`, task_2 (the calendar) and task_3 (the slides) had finished before the snapshot, and the first reply narrated the plan without stating either result (measured, one reader's read of the reply text). The slides result was never spoken in either turn, and the calendar result was spoken only when a later task returned the same summary. `backlog_report` counts both as said, because its model assumes a terminal task in the snapshot reaches the first reply. So the run-record count of 0 never-said results is too kind, and the text read is the honest one.

A second blind spot is what the update says. The message carries each task's description and summary, and LFM3 paraphrases them. On `09_run_on` the update reply says the combined task completed, while the summary is "Mock worker finished. No real work was done" (measured, one reader's read). The mock did nothing real for any task, so this is the mock's wording, not a backlog duplicate. The mock also gives two tasks the same summary on `02_two_requests` and `two_request`, so the second update repeats a sentence the person heard for the first task (measured). The rule told two different tasks, so it is not a duplicate by task id.

Nothing here decides whether a result was worth saying. That judgement is the reader's. The sample in [the 12 re-prompts](#the-12-re-prompts) is the one to read.

## The 12 re-prompts

The after arm made 12 re-prompts over 10 clips, and 3 results arrived during a reply (measured, from the run records). `03_dependent` merged two results, the fare and the mock worker's completion, into one update (measured). The other 11 carried one result each.

One reader read all 12 update replies against their backlog:

| Question | Answer |
| --- | --- |
| Re-prompts whose result the first reply had not stated | 12 of 12 |
| Merged re-prompts | 1, on `03_dependent` |
| Results the person needed that were never spoken | 1, `long` task_3's slides |
| Results spoken twice | 0 |
| Update replies that overstate what ran | 2, `03_dependent` and `09_run_on` |

The sample with every decision, the backlog that drove it, and the reply that followed is `$VOICE_HOME/evidence/backlog/decisions.md`, printed by `python -m voice.prototype.backlog_decisions`. It quotes the replies, so it stays local. Per COMMON.md, a reader's grade is not an acceptance: the PM reads this sample, and Mason reads it before any run over an hour.

## Calls and cost

The round made 33 decider attempts against its cap of 100, and every one returned HTTP 200 with no 429 (measured, from the ledger). No billing stop file exists. The prototype's ledger now holds 323 attempts against `DECIDER_BUDGET` 390. The client asserts the `:free` model id and stops on a nonzero cost. The cache and ledger stay under `$VOICE_HOME/evidence/prototype/decider-cache/`, because their keys hold transcripts.

## Reproduce

Run from the repository root. The offline tests need no GPU, no key, and no network:

```
python -m unittest discover -s voice/prototype/tests -t .
```

`voice/prototype/tests/test_backlog.py` covers the rule: two unseen results merge into one update in arrival order, a failed result is told, a result the snapshot carried is skipped while a newer one is told, a cancellation is never told, a result that lands during the update reply waits for the next decision, and an empty backlog records a decision without a re-prompt. A mutation of `TOLD` and a mutation that ignores `reported` each fail the suite (checked).

The after arm ran inside the GPU lease with `quiet_run.py --holder voice-backlog`:

```
python -m voice.prototype.run CLIPS... --eou gated --fresh --out "$VOICE_HOME/evidence/backlog/after"
python -m voice.prototype.backlog_report before="$VOICE_HOME/evidence/eou/r2-gated" after="$VOICE_HOME/evidence/backlog/after"
python -m voice.prototype.eou_report before="$VOICE_HOME/evidence/eou/r2-gated" after="$VOICE_HOME/evidence/backlog/after"
python -m voice.prototype.backlog_decisions "$VOICE_HOME/evidence/backlog/after"
```

CLIPS is `$VOICE_HOME/audio/real/01_single.wav` through `10_noisy.wav`, then `single`, `two_request`, `correction`, and `long` from `$VOICE_HOME/evidence/stage1/audio/`.

## Files outside the repo

| Path | Contents | Size |
| --- | --- | --- |
| `$VOICE_HOME/evidence/backlog/` | The after arm's 14 run records, reply and update WAVs, the server log, and `decisions.md` | 15.2 MB |
| `$VOICE_HOME/evidence/prototype/decider-cache/` | The ledger lines and cached responses | part of the existing cache |

No model, package, or venv was added.

## Open decisions

- Should the rule check the reply text, not only the snapshot? That would catch `long`'s unspoken slides result. The cheapest form is a decider question over the backlog and the reply, and the brief lists it as an option. It costs one call per clip with a result, against today's zero.
- Should idle results batch for a short window, to trade the two extra update turns back for a little latency? This run cannot answer it, because no two idle results landed within a second of each other (measured, the closest were 1.81 s apart on `two_request`). The two extra turns came from `02_two_requests` and `two_request`, each of which had a second result finish after the first update turn.
- Should an update reply that promises an action, as `03_dependent` does with "I'll proceed to book it", be allowed to make a tool call? That is the gate's and LFM3's scope, not the backlog's, and PR #12 lists it as an open decision.
