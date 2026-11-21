# Stage 2 boundary variants: variant E against the baseline

Measured on 2026-11-16 (UTC) against `perplexity/pplx-decider-flash-v1.3:free`. Every number below comes from committed files through `python -m voice.research.segmentation.variants_report`, which writes `voice/research/segmentation/results/variants-report.json`.

Variant E lost to the round 2 baseline on both scenario sets. On the original 11 streams E passed 7 and the baseline passed 9. On the 18 held-out streams E passed 6 and the baseline passed 8 (measured). E placed more of the hand-labeled breaks exactly, 16 of 18 held-out breaks against the baseline's 12 (measured). Its losses came after the break, from false corrections and from dispatching a request before the speaker corrected it.

## Recommendation

- Keep the baseline for Stage 3. That is the clause splitter plus one bundled decisions call per boundary change, from `docs/voice/stage2-segmentation.md`. It passed more scenarios on both sets, made fewer calls per utterance (2.56 against 4.00 on the held-out set), and had fewer premature dispatches, 4 against 8 on the held-out set (measured).
- Give the baseline splitter E's marker rules as the next experiment. These are commas, "then", "also" and "I mean", plus the fallback to word gaps in a long stretch with no marker. The baseline's 6 missed held-out breaks all sit at a comma, a bare "then", an "I mean", or between two unpunctuated imperatives (measured, `boundaries.stream-a-heldout.per_scenario`). E's markers covered all 18 held-out breaks on punctuated text (measured). Whether the baseline's per-segment questions then classify the new pieces as well as before is untested (guess). Commas may create pieces that the decider has to label as fragments.
- Do not ship E's `corrects` question as written. It marked a correction in 4 scenarios that had none, including `near_duplicates` with margin 0.86 (measured). The baseline asks for the segment's kind first and applies a correction only when the kind is `correction`. That gate is the likely reason the baseline made fewer false corrections (inferred).
- Drop E's dependency near-tie hold. In `post_and_read` the decider answered `depends` with `none` at margin 0.13, and the hold turned that into a wait on the first request, which failed the scenario (measured).
- Do not let a pause alone commit a request that can still be corrected. In `pause_correction`, a repeated partial committed and dispatched "search for violin lessons in Bath" one revision before "actually, Bristol" arrived (measured).

## What was compared

Two systems ran over the same revision streams. They share the task state machine, the dispatch policy, and the scorer.

|                       | Baseline (round 2 option a)                                                          | Variant E                                                                                          |
| --------------------- | ------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------- |
| Who finds the break   | A rule-based splitter at sentence punctuation and spoken markers                     | The decider picks a numbered marker after a cursor                                                 |
| Calls                 | One per change in the segment list, plus delegate points and the final transcript    | One per revision, plus delegate points and one per extra request on the final transcript           |
| Questions per call    | Kind, readiness, side effect, dependency and correction for every segment (median 8) | Finished, marker, side effect, dependency and correction for the first finished request (median 3) |
| Input tokens per call | Median 953                                                                           | Median 418                                                                                         |
| Code                  | `voice/segmentation/spans.py`, `contract.py`                                         | `voice/segmentation/cursor.py`                                                                     |

Variants A, B, C and D did not run, for the reasons in [Variants not run](#variants-not-run).

## How variant E works

`voice/segmentation/cursor.py` holds all of E. The parts that decide are pure functions, and `CursorRunner` wires them to the client and the orchestrator.

1. The cursor is a word index into the utterance. Committed segments are word ranges `[w0, w1)` before it. Each revision re-derives their text from the new transcript, so an ASR fix before the cursor updates an undispatched task's instruction.
2. `markers` numbers the candidate breaks after the cursor. A gap is a marker if clause punctuation precedes it or a connective starts there. The connectives are "and", "and then", "and also", "then", "also", "oh and", "actually", "no wait", "I mean", "but", "never mind" and "anyway". A stretch of more than 12 words with no marker falls back to every word gap, and the end of the speech is always the last marker.
3. One decisions call asks these questions. `state` holds `new_speech` (the words after the cursor, with ` [k]` at each marker), `earlier_requests` (committed segments by id), and `speech_is_final`.

| Question                 | Type                          | Options                                                                                                     |
| ------------------------ | ----------------------------- | ----------------------------------------------------------------------------------------------------------- |
| `finished`               | noul                          | Has the speaker finished explicitly asking for a task, or for a change or withdrawal of an earlier request? |
| `at`                     | choice                        | `m1` to `mN`, each naming the request it would close, plus `none`                                           |
| `side_effect`            | noul                          | Would carrying out the first finished request change something outside the conversation?                    |
| `depends`                | choice, once a segment exists | `none` or an earlier segment id                                                                             |
| `corrects`               | choice, once a segment exists | `none`, `replace_<sid>`, `cancel_<sid>`                                                                     |
| `implicit`, `preemptive` | noul, only with `--extra`     | Measured separately, never used in decisions                                                                |

4. `judge` applies the condition that the API cannot express.

| `finished` at 0.5 or more | `at`     | `at` margin | On a partial   | On the final transcript                          |
| ------------------------- | -------- | ----------- | -------------- | ------------------------------------------------ |
| yes                       | a marker | 0.2 or more | candidate      | candidate                                        |
| any                       | any      | under 0.2   | near-tie, wait | the earlier marker of the top two, if `finished` |
| yes                       | `none`   | 0.2 or more | disagree, wait | the runner-up marker                             |
| no                        | a marker | 0.2 or more | disagree, wait | disagree, nothing commits                        |
| no                        | `none`   | 0.2 or more | nothing        | nothing                                          |

5. `commits` applies the stability rule. A candidate is the words from the cursor to its marker. It commits when the same words were the candidate on the previous revision, or when this revision is a pause. A pause is a revision whose words equal the previous revision's words. A side-effect request needs both. The final transcript commits, because no revision follows it. After a commit on the final transcript, the runner asks again about the words left, up to 8 times.
6. `decision_for` turns the answer into the baseline's `SegmentDecision`. A near-tie between `none` and a dependency takes the dependency, so the task waits. `CursorOrchestrator` then reuses the baseline's `Orchestrator` for task creation, corrections, delegate links and the dispatch policy. The policy still holds a side-effect task until the final transcript.

Four choices go beyond the brief's description of E.

- `state` also holds the committed segments, not only the words after the cursor. Without them, `depends` and `corrects` have nothing to point at.
- The dependency and correction questions ride in the same call and ask about "the first finished request". This keeps one call per revision.
- A delegate submission gets its own call with only `<did>_same_as`, as in the baseline. A `new_speech` answer dispatches the delegate's task at once and links it to the next committed segment whose words match at least 60%.
- The final-transcript column of the table above is new. Without it, a near-tie on the final transcript left the rest of the speech uncommitted.

## Tuning on the original 11

All tuning used only the original 11 scenarios. Then the code, thresholds and break labels were committed (`34bdcd75f`, `6a0c24bec`) before the first held-out run. The thresholds are `finished` 0.5, choice margin 0.2 (the round 2 near-tie bound), and `side_effect` 0.5.

| Version  | Passed  | What changed                                                                                                                                                                                                                                                                 | Evidence                                    |
| -------- | ------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------- |
| v1       | 3 of 11 | Marker options described by the words around the gap. A near-tie also covered `finished` within 0.1 of 0.5.                                                                                                                                                                  | `results/stream-e-v1/`, code at `af980a971` |
| Frozen E | 7 of 11 | Each marker option names the request it closes. The questions define a request as one action. The `finished` band was dropped, because the brief asks only for a threshold. The final-transcript rule was added. A marker bug that split "and then" at the cursor was fixed. | `results/stream-e/`                         |

v1 lost 5 scenarios because a near-tie or a disagreement on the final transcript ended the run with speech left over (measured). One more prompt revision would have cost about 90 calls, which the held-out runs needed. So E froze at 7 of 11.

## Results on the original 11

The baseline rows replay round 2's `stream-a`. "Calls" counts every request a run sent, and none were cache hits. "Early" counts dispatches on partial revisions. Break error is the distance in words from the nearest cut to each labeled break, with "miss" when no cut lands within 3 words.

| Scenario                     | Baseline | E    | Revisions | Calls, baseline / E | Early, baseline / E | Break error, baseline / E |
| ---------------------------- | -------- | ---- | --------- | ------------------- | ------------------- | ------------------------- |
| 01_two_independent           | pass     | pass | 6         | 3 / 6               | 1 / 1               | 0 / 0                     |
| 02_two_dependent             | pass     | pass | 6         | 3 / 6               | 0 / 1               | 0 / 0                     |
| 03_topic_change              | pass     | FAIL | 6         | 3 / 7               | 1 / 1               | 0 / 0                     |
| 04_mid_sentence_correction   | pass     | FAIL | 7         | 3 / 7               | 0 / 0               | 0 / miss                  |
| 05_correction_after_dispatch | pass     | pass | 7         | 4 / 7               | 1 / 2               | 0, 0 / 0, 0               |
| 06_description_not_request   | pass     | pass | 6         | 3 / 6               | 0 / 0               | none labeled              |
| 07_intentional_repeat        | pass     | FAIL | 4         | 2 / 5               | 0 / 0               | 0 / 0                     |
| 08_delegate_after_dispatch   | FAIL     | FAIL | 5         | 4 / 6               | 1 / 1               | 0 / miss                  |
| 09_delegate_before_segment   | FAIL     | pass | 6         | 4 / 7               | 1 / 1               | 0 / 0                     |
| 10_side_effect_email         | pass     | pass | 6         | 4 / 7               | 0 / 0               | 0 / 0                     |
| 11_filler_false_starts       | pass     | pass | 6         | 2 / 6               | 0 / 0               | none labeled              |

Totals, baseline then E: 9 and 7 pass, 1 and 2 premature dispatches, 0 and 0 duplicates, 0 and 1 missed corrections, 35 and 70 calls (measured). E's failures:

- 03 and 07 failed on false corrections. "Okay, now roll it again" got `replace_s1` with margin 0.40, and the banana bread question got `cancel_s1` with margin 0.25 (measured). In 03, the final-transcript rule also chose "Anyway," as its own request over "Anyway, totally different thing", which were within 0.12 of each other (measured).
- 04 and 08 failed because the decider kept one request across the break. It chose the end of "Book a table for four, no wait, for six, at Luigi's tonight at seven" with margin 0.76, and the end of "Find the Q3 budget spreadsheet and tell me the total travel spend" with margin 0.32 (measured). Each reading is defensible as one request, but the hand-written expectations call for two.
- E passed 09, which the baseline failed. E has no readiness question, so the ferry question was never sent to `needs_clarification` (measured).

## Results on the 18 held-out scenarios

Each system ran once on the held-out set, after the freeze.

| Scenario           | Covers                                | Baseline | E    | Calls, baseline / E | Early, baseline / E | Break error, baseline / E |
| ------------------ | ------------------------------------- | -------- | ---- | ------------------- | ------------------- | ------------------------- |
| actually           | correction with "actually"            | FAIL     | FAIL | 3 / 3               | 0 / 0               | 0 / miss                  |
| assistant_question | no task                               | pass     | pass | 2 / 3               | 0 / 0               | none labeled              |
| buy_and_read       | buy plus read                         | pass     | FAIL | 3 / 4               | 1 / 0               | 0 / 0                     |
| cancel_second      | cancellation                          | pass     | FAIL | 3 / 4               | 1 / 1               | 0, 0 / 0, 0               |
| delegate_overlap   | delegate and orchestrator overlap     | pass     | pass | 3 / 4               | 1 / 1               | none labeled              |
| delete_and_read    | delete plus read                      | pass     | pass | 3 / 3               | 1 / 1               | 0 / 0                     |
| email_extension    | apparent completion then continuation | pass     | pass | 2 / 4               | 0 / 0               | none labeled              |
| i_mean             | correction with "I mean"              | FAIL     | FAIL | 2 / 3               | 0 / 0               | miss / miss               |
| market_three       | three independent requests            | FAIL     | FAIL | 3 / 6               | 1 / 0               | miss, 0 / 0, 0            |
| mixed_chain        | independent request and chain         | FAIL     | FAIL | 2 / 5               | 0 / 1               | miss, 0 / 0, 0            |
| museum_chain       | three chained requests                | FAIL     | pass | 3 / 5               | 1 / 1               | miss, 0 / 0, 0            |
| near_duplicates    | near duplicates are distinct          | pass     | FAIL | 2 / 4               | 0 / 0               | 0 / 0                     |
| no_wait            | correction with "no wait"             | FAIL     | FAIL | 3 / 4               | 0 / 1               | 0 / 0                     |
| pause_correction   | correction after a pause              | FAIL     | FAIL | 3 / 4               | 0 / 1               | 0 / 0                     |
| post_and_read      | post plus read                        | pass     | FAIL | 3 / 3               | 1 / 1               | 0 / 0                     |
| rain_wonder        | implicit request                      | FAIL     | FAIL | 2 / 3               | 0 / 0               | none labeled              |
| run_on             | unpunctuated run-on                   | FAIL     | pass | 2 / 6               | 0 / 0               | miss, miss / 0, 0         |
| true_repeat        | true repeat                           | FAIL     | FAIL | 2 / 4               | 0 / 0               | none labeled              |

Totals, baseline then E: 8 and 6 pass, 4 and 8 premature dispatches, 1 and 1 duplicates, 1 and 2 missed corrections, 46 and 72 calls (measured). Per-scenario records are in `voice/research/segmentation/results/stream-a-heldout/` and `voice/research/segmentation/results/stream-e-heldout/`.

E failed for these reasons, read from its decision points.

- False corrections cancelled real tasks in `market_three` (margin 0.11) and `near_duplicates` (margin 0.86) (measured). A near-tie hold on `corrects` would have saved `market_three` only (inferred). That rule was not tuned or run, because it would come from the held-out set.
- A request dispatched before its correction arrived in `no_wait` and `pause_correction`. The task ended as `cancel_requested`, but the expectation says it must never dispatch (measured). In `pause_correction`, the commit came from the repeated partial, which the stability rule treats as a pause.
- The decider kept the correction inside one request in `actually` and `i_mean`. It chose the end of "Look up the height of Mount Elbert, actually Mount Massive." with margin 0.62 (measured).
- In `buy_and_read`, the decider said the purchase needs the review, with `depends` at `s1` and margin 0.57, so the task waited in `ready` (measured). `post_and_read` failed the same way, but there the decider said `none` at margin 0.13 and E's dependency near-tie hold chose the wait (measured). `mixed_chain` failed the other way, with `none` at margin 0.55.
- In `cancel_second`, "and find the ferry timetable" committed and dispatched on the final transcript before the drain reached "Never mind the second one." The task ended as `cancel_requested`, but the expectation says `withdrawn` (measured). The final transcript dispatches each committed segment at once instead of reading every request first. Settling the dispatches after the drain would fix this case (inferred, not run).
- `rain_wonder` is an implicit request. E asks only about explicit requests, and `finished` stayed at 0.05 or below (measured).
- `true_repeat` expects one task for an exact repeat, and the original `07_intentional_repeat` expects two. The two sets disagree, and both systems dispatched twice (measured).

The scorer matches an expected task by the words of the task's own segment. Several held-out expectations name words from the corrected request, such as "height" for "actually Mount Massive". Rescored against the dispatched instruction instead, the baseline passes 11 of 18 and E still passes 6 (measured, `pass_if_matched_on_instruction`). This is a scorer sensitivity, not a tuning result. The primary numbers above use the scorer as written.

## Breaks, flips and extra questions

| Measurement                      | Baseline, original | E, original | Baseline, held-out | E, held-out |
| -------------------------------- | ------------------ | ----------- | ------------------ | ----------- |
| Labeled breaks found exactly     | 10 of 10           | 8 of 10     | 12 of 18           | 16 of 18    |
| Labeled breaks missed            | 0                  | 2           | 6                  | 2           |
| Cuts away from any labeled break | 1                  | 0           | 3                  | 1           |
| Calls per utterance              | 3.18               | 6.36        | 2.56               | 4.00        |

No found break was off by 1 to 3 words. Each break was either exact or missed (measured). E missed only where the decider kept two requests together. The baseline missed where its splitter has no cut (measured).

E's markers held a labeled break in 10 of 10 original breaks and 18 of 18 held-out breaks on punctuated final transcripts (measured). With punctuation stripped to mimic ASR partials, they held 9 of 10 and 16 of 18 (measured). The 3 misses on stripped text are breaks with no connective and no punctuation, in `07_intentional_repeat` ("for me okay now roll"), `market_three` ("oats find") and `near_duplicates` ("monday check") (measured).

A `finished` answer of 0.5 or more was followed by one below 0.5 at the same cursor on the next revision in 1 of 34 pairs on the original set and 1 of 31 on the held-out set (measured). Most instability sat in `at`. 8 of 68 original answers and 5 of 71 held-out answers had a margin under 0.2 (measured).

Adding the `implicit` and `preemptive` questions changed no other answer (measured). `voice.research.segmentation.extra` resent 15 of E's recorded requests with the two questions added. None of the 51 shared answers changed its choice or crossed 0.5, and noul values moved by a median of 0.0 and at most 0.2. The paired latency difference had a median of -1.9 ms, with single pairs from -92.6 to +232.7 ms (measured, n = 15). Two more questions add no cost large enough to show over that spread, and an effect of a few milliseconds would not show in 15 pairs (inferred).

## Latency and failures

All live calls ran inside `quiet_run.py --holder voice-stage2`, one request in flight, between 01:03:36 and 01:28:28 UTC. The timer is round 2's, from send to the end of the response body. Each distinct request counts once.

| Run                          | n   | Median ms | p95 ms | Max ms |
| ---------------------------- | --- | --------- | ------ | ------ |
| Baseline, original (round 2) | 35  | 93.0      | 179.6  | 221.0  |
| E, original                  | 70  | 94.6      | 185.2  | 655.5  |
| Baseline, held-out           | 46  | 94.9      | 212.1  | 340.1  |
| E, held-out                  | 71  | 94.3      | 128.2  | 236.1  |

- E's calls cost the same as the baseline's, although E sends less than half the input tokens (median 418 against 953, measured). Round 2 put the network round trip at about 11 ms. So most of the 94 ms is a fixed cost per request behind the OpenRouter edge, and the size of the request does not drive it (inferred). The extra-question pairs point the same way.
- E makes about twice the calls per utterance. Each dispatch decision still waits on one call, so a committed break dispatches about 94 ms after the revision that confirms it (inferred from the medians). E confirms a break on the second agreeing revision, and the baseline dispatches a segment when the next boundary appears. In 01 both dispatched the weather request at revision 3 (measured).
- No answer was malformed or refused in any E run (measured).
- The free tier returned 92 HTTP 429s among this round's 363 attempts (measured). Round 2 saw none. Spacing calls 1.2 s apart, then 3.1 s apart, did not stop them. During the held-out E run they arrived one or two at a time, with no burst pattern (measured), so they look like limits on shared free capacity, not on this client's rate (guess). Each 429 counts against the budget, and the client backs off 2, 4, then 8 s.
- Every cached response reported `usage.cost` 0, all 428 of them (measured).

## Call budget

This round used 363 of its 400 attempts, counted from `voice/research/segmentation/calls.jsonl` after round 2's 157. The client's cap is now `157 + 400`.

| Purpose                                              | Attempts | Of which 429 |
| ---------------------------------------------------- | -------- | ------------ |
| E v1, original 11                                    | 90       | 21           |
| E frozen, original 11 (incl. 2 after the marker fix) | 94       | 24           |
| Baseline, held-out 18                                | 60       | 14           |
| E, held-out 18                                       | 103      | 32           |
| Extra-question pairs                                 | 16       | 1            |

## Variants not run

The brief makes A and C conditional. They run if E loses on the original 11, or if budget is left after E. E lost, but after the held-out runs only 53 attempts remained. The extra-question pairs, which the brief asks of E, took 16, which left 37, about 28 successful calls at this round's 429 rate. A needs at least two calls per decision point. C needs a boundary call and a classification call. Neither would cover both scenario sets within that budget, so neither ran.

B and D are close to what ran. E's `at` question is B's single choice over the gaps after a cursor. E's markers are D's candidate set, and their recall is in [Breaks, flips and extra questions](#breaks-flips-and-extra-questions).

## Limits of this evaluation

- E was tuned on the original 11 and the baseline was not retuned, so the original-set comparison favors E (inferred). The held-out set is the fairer comparison.
- The held-out set has 18 scenarios with 3 or 4 revisions each, written by another author. Its README and two of its files (`actually`, `delegate_overlap`) were read before E was frozen. The other 16 were read after the freeze, while the break labels were written.
- The scorer and the expectations come from round 2 and were not changed. The two sets disagree about repeats.
- Each request ran once. Round 2 found the decider deterministic on identical input, so these answers should repeat, but the sample is small.
- E's cursor maps committed segments by word index. An ASR revision that changes the word count before the cursor would shift every later segment. "itll" becoming "it will" in `rain_wonder` is one, but nothing had committed there.

## Reproduce

Run from the repository root. The offline tests need no key and no network, and they replay all ten recorded runs, including `stream-e`, `stream-a-heldout` and `stream-e-heldout`.

```
python -m unittest discover -s voice/segmentation/tests -t .
python -m voice.research.segmentation.variants_report
```

`tests/test_cursor.py` covers the markers, the condition table, the stability rule, the near-tie hold, the dependency hold, delegate links, and the final-transcript drain.

To repeat a live run, set `VOICE_ENV_FILE` to the env file that holds `OPENROUTER_API_KEY`, and wrap the command in `quiet_run.py`. Cached requests replay without a call.

```
python quiet_run.py --holder voice-stage2 -- python -m voice.segmentation.run --option e --mode stream
python quiet_run.py --holder voice-stage2 -- python -m voice.segmentation.run --option e --mode stream --set heldout
python quiet_run.py --holder voice-stage2 -- python -m voice.segmentation.run --option a --mode stream --set heldout
python quiet_run.py --holder voice-stage2 -- python -m voice.research.segmentation.extra
```

`results/stream-e-v1/` came from the code at commit `af980a971`. The current code does not replay it.
