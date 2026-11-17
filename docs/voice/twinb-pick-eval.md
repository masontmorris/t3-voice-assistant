# Twin B: does a fast model pick the right thread?

**Bottom line.** Neither arm passes the design's bar. The deterministic prefilter alone never names a
wrong thread in the ambiguous, nonexistent, or outside-project slices, and answers 49 percent of the 180
cases. Adding `particle-ai/particle-1:free` raises correct answers to 83 percent and names a wrong thread
in 5 of 180 cases, including the design's "the PR thread" known failure, at a p95 of 3 to 18 seconds per
answered pick under eval load (measured). Recommendation: keep the prefilter as the floor, do not give
the pick to this free model yet, and spend the next run on prefilter recall plus a confidence gate
instead of a second model.

Every claim below carries a label in its own sentence. The tables are run output.

## What ran

Four frozen snapshots were built from a read-only copy of a real T3 `statev2.sqlite` (measured: copied
with SQLite's backup API from a `mode=ro` connection, never opened from the live path by any runner). The
window and project choices live in a local spec file, so no project name is baked into the builder.
Each snapshot carries the seam design's thread summary shape: `threadId`, `projectId`, `title`,
`activity`, `lastActivityAt`, `pinned`, plus the snapshot's `allowedProjects`, which stands in for the
seam's project ceiling. Visible thread counts are 52, 52, 43 and 50, and each snapshot also holds 6
threads from a project outside its ceiling (measured).

The snapshots were chosen to carry the nasty cases the slices need (measured). Every snapshot has a
two-word title prefix shared by 3 to 7 threads, the largest being 7. Visible projects hold 4 to 26
threads each. One snapshot has two threads that finished within a second of each other. The activity
values across the snapshots cover working, finished, failed, cancelled, interrupted and idle.

Labels: 180 spoken requests. Six known-failure cases were written by hand from the design's list before
anything else, then an Opus 5.5 labeler wrote 174 more from the snapshots alone, with no picker output
anywhere on disk at that point (measured: the labeler was a single delegated task and the whole eval had
not run). The label set and snapshots were hashed and frozen at 2026-11-17T10:32:47Z, before the first
run (measured, `labels/frozen-manifest.json`). One hundred eighty cases also match the design's slice
counts after the six hand-written cases fill their slices (measured with the label validator).

| arm | configuration | model calls |
| --- | --- | --- |
| prefilter | task record first, then one listed title containing every content word is the pick, two or more ask, no match says none | 0 |
| model | prefilter first, then a tie or a miss goes to `particle-ai/particle-1:free`, temperature 0, 300 output tokens, provider sort latency, one request in flight, every reply cached | 399 |
| local | skipped, see below | 0 |

The model was chosen by a speed screen over the 14 free OpenRouter ids (measured, 26 requests with a
synthetic 20-thread list): particle-1 returned clean JSON in 620 and 881 ms, gemma-4-26b in 885 and 903
ms, north-mini-code in 542 and 1744 ms with its first reply truncated, flint in 1471 and 1913 ms,
nemotron-nano in 2739 and 3800 ms, and six ids returned 429 or 403. particle-1 was the fastest clean
responder, so it is the only model in the arm.

The local arm is skipped with a measured reason. The two local models small enough for a spoken turn are
decision models, and `llama-server` answered both the 2B and the 600M omni model with HTTP 501, "This
model is a decision model without text generation, use /v1/systemone" (measured, under a GPU lease). A
`systemone` picker that scores thread titles as a choice set is a different design from the text picker
under test, and this eval does not cover it (inferred).

Every case ran 3 times at those settings and passes only if all 3 runs pass (measured). The prefilter arm
is deterministic, so its 3 repeats are identical by construction (measured: 0 cases with a differing
outcome). The model arm produced 3 distinct HTTP calls per case, and 19 of 180 cases disagreed across
repeats (measured).

## Results

Pass counts over 3 repeats, per slice. A case passes when all 3 runs are correct.

| slice | cases | prefilter pass | model pass |
| --- | --- | --- | --- |
| exact | 30 | 20 | 30 |
| paraphrase | 40 | 11 | 36 |
| project | 15 | 0 | 11 |
| state | 20 | 0 | 13 |
| back-reference | 15 | 15 | 15 |
| ambiguous | 25 | 17 | 25 |
| nonexistent | 15 | 15 | 9 |
| no subject | 10 | 0 | 5 |
| outside project | 10 | 10 | 5 |
| total | 180 | 88 | 149 |

Outcomes over 540 runs per arm. "Wrong thread" counts a pick the owner did not mean, which includes
picking one thread when the right answer was a question.

| slice | arm | correct | wrong thread | asked when one was right | unnecessary question | said none |
| --- | --- | --- | --- | --- | --- | --- |
| exact | prefilter | 60 | 0 | 0 | 0 | 30 |
| exact | model | 90 | 0 | 0 | 0 | 0 |
| paraphrase | prefilter | 33 | 0 | 0 | 0 | 87 |
| paraphrase | model | 114 | 0 | 6 | 0 | 0 |
| project | prefilter | 0 | 0 | 0 | 0 | 45 |
| project | model | 36 | 7 | 2 | 0 | 0 |
| state | prefilter | 0 | 3 | 0 | 0 | 57 |
| state | model | 45 | 3 | 9 | 0 | 3 |
| back-reference | prefilter | 45 | 0 | 0 | 0 | 0 |
| back-reference | model | 45 | 0 | 0 | 0 | 0 |
| ambiguous | prefilter | 51 | 0 | 0 | 0 | 24 |
| ambiguous | model | 75 | 0 | 0 | 0 | 0 |
| nonexistent | prefilter | 45 | 0 | 0 | 0 | 0 |
| nonexistent | model | 35 | 3 | 0 | 7 | 0 |
| no subject | prefilter | 0 | 0 | 0 | 0 | 30 |
| no subject | model | 18 | 3 | 0 | 0 | 9 |
| outside project | prefilter | 30 | 0 | 0 | 0 | 0 |
| outside project | model | 18 | 0 | 0 | 12 | 0 |

The pass bar from the design, per arm (measured):

| criterion | bar | prefilter | model |
| --- | --- | --- | --- |
| wrong thread in ambiguous, nonexistent, outside | 0 | 0, pass | 3, fail |
| wrong thread overall | at most 2 | 3, fail | 16, fail |
| correct on the unambiguous slices | at least 90 percent | 44.3 percent, fail | 97.1 percent, pass |
| unnecessary questions | at most 10 percent | 0 percent, pass | 6.7 percent, pass |
| p95 pick latency | at most 700 ms | 0.43 ms, pass | 7.2 s, fail |

Latency, over the 540 runs per arm (measured). The model arm's p95 by slice is in the table below. The
single request p95, read from the saved per-request timings, is 2523 ms with a median of 1124 ms. The
limiter is the free tier, not the prompt. 126 of 399 model runs took over 2.5 s (measured), which the
spacing points to 429 backoff at 2 s, 4 s, 8 s and so on (inferred), while the two-request speed screen
on a quiet path measured 620 and 881 ms for the same model and prompt shape (measured). So the 700 ms bar
fails on both readings. 7.2 s is an eval-load number, and the 2.5 s single-request p95 is the closer
estimate of one deployed pick (inferred).

| slice | prefilter p95 ms | model p95 ms |
| --- | --- | --- |
| exact | 0.31 | 3064 |
| paraphrase | 0.35 | 7157 |
| project | 0.29 | 7381 |
| state | 0.29 | 6829 |
| back-reference | 0.01 | 0.01 |
| ambiguous | 0.31 | 7080 |
| nonexistent | 0.29 | 4368 |
| no subject | 0.34 | 17960 |
| outside project | 0.43 | 4412 |

One quality number needs its cause named. 122 of the 399 model replies were not parseable JSON, and each
became a question (measured). Most are a JSON object cut off at the 300 token cap (inferred from the
saved replies). Of those, 17 were cases whose right answer was one thread, so truncation cost real
picks, and 86 were cases whose right answer was a question, so truncation scored as correct by accident
(measured). The model's 149 passes and its 6.7 percent unnecessary question rate both lean on that
accident. A retry on malformed JSON, or a larger token cap, is a named change, not a free fix, and no
number here includes it.

## The known failures

| case | what it checks | prefilter | model |
| --- | --- | --- | --- |
| shared title prefix | seven threads share a prefix, so ask | ask, correct | ask with 3 of 7, correct |
| project-only mention | 26 threads in the project, so ask | none, fail | one thread, fail |
| "the PR thread" with no PR anywhere | say none, never the newest | none, correct | named a thread, fail |
| recency, two just finished | ask | none, fail | ask, correct |
| deleted thread, absent | none | none, correct | none, correct |
| back-reference after a reconnect | resolve from the task record | the record's thread, correct | the record's thread, correct |

The prefilter's three wrong picks all come from the state slice, where a state word is also a word in a
title, so containment matched one thread and named it for a question about state (measured). The model's
worst result is the design's own example: "how's the PR thread going?" picked a thread in a list with no
PR in any title or state (measured, 3 of 3 repeats).

When the model asked, its shortlist often missed the thread the owner meant. 63 of 75 ambiguous runs
contained a gold thread, but only 12 of 29 project runs and 8 of 27 state runs did (measured). A question
whose candidate set has the wrong threads is not a usable clarification.

## What this metric cannot see

- It scores the pick only. A correct pick can still be spoken as a false sentence, which is the failure
  the end-of-utterance handoff already measured.
- Text only. No ASR error, no misheard "PR", no pause or repair.
- 180 cases is small, and six of the labels are mine. A label one careful reader would argue flips a case.
- No visible project in any snapshot has exactly one thread, so a bare project mention is decided among
  several threads. Three project cases still expect one thread because the utterance names a run label
  inside that project (measured). The "one thread in the project" shape is untested.
- A state word with zero matching threads is untested, because the label validator only allows a "none"
  answer in the nonexistent and outside slices.
- The back-reference slice is decided by the task record before either arm runs, so it cannot separate
  the arms (measured, 15 of 15 for both).
- The ceiling is static. The snapshots bake in `allowedProjects`, which is not the seam enforcing a
  ceiling on a live list.
- Three repeats at temperature 0 measure transport variance, not creative variance.
- Nothing here measures whether the spoken summary of the chosen thread is true, or whether the read is
  fresh. A thread can change between the pick and the read.

## Privacy: what was sent where

Thread titles and project names, as plain text, went to OpenRouter and nowhere else: 26 requests to
screen 13 free model ids, then 554 completion attempts against `particle-ai/particle-1:free` (399 first
attempts and 155 retries, mostly 429), every one with a model id ending in `:free`, asserted in code
(measured). Each response cost 0, and no 402 or billing language appeared. The live path already sends
text to a free decider (inferred from the lane's decider default), so this matches its exposure.
Snapshots, labels, run files and raw replies stay under `$VOICE_HOME/evals/twinb-pick` and are not in
the repository. That directory also holds the snapshot spec, which names projects, and the ten scored
outputs. Nothing from this eval is in the repository except this page, the picker code and its
synthetic-fixture tests.

## Recommendation

1. Ship the prefilter as the deterministic floor. It names no wrong thread in the dangerous slices and
   costs under a millisecond (measured). It resolves 49 percent of these cases, so it cannot be the whole
   answer.
2. Do not ship this free model for the pick. It fails the zero-wrong-thread bar and its latency cannot
   hold a spoken turn, and a third of its replies needed a parser rescue (measured).
3. The next configuration worth one run is prefilter plus a small synonym and project map, with the
   model kept out until recall is up. That targets the two real prefilter misses, paraphrase and project
   mentions, without adding the failure mode the model brought. If a model comes back, it needs a
   confidence gate that asks instead of guessing, a retry on malformed JSON at a higher token cap, and a
   latency budget measured on a quiet path.
4. The optional Opus ceiling arm was skipped, as the brief allows.

Ten scored real outputs, with the titles, are at `$VOICE_HOME/evals/twinb-pick/sample-10-scored.md` for
the morning read. Every run file is beside them under `runs/`.

## Reproduce

```text
python -m unittest discover -s voice/t3/pick/tests -t .
python -m voice.t3.pick.harness --arm prefilter --repeats 3
VOICE_ENV_FILE=<path to the env file with OPENROUTER_API_KEY> python -m voice.t3.pick.harness --arm model --repeats 3
```

`harness.py` reads `$VOICE_HOME/evals/twinb-pick/{snapshots,labels}` and writes `runs/`. The snapshots
come from `voice/t3/pick/snapshot.py` run against a copy of a `statev2.sqlite`.
