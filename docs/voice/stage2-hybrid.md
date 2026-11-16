# Stage 2 hybrid: plan A against the baseline and variant E

Measured on 2026-11-16 (UTC) against `perplexity/pplx-decider-flash-v1.3:free`. Every scenario number below comes from committed files through `python -m voice.segmentation.hybrid_report`, which writes `voice/segmentation/results/hybrid-report.json`. The clip numbers come from the same command run where `$VOICE_HOME` holds the prototype's run records. Each claim says whether it is measured, inferred, or a guess.

The hybrid beat the baseline on both scenario sets and tied it on Mason's ten clips. It passed 11 of 11 original scenarios against the baseline's 9, and 14 of 18 held-out scenarios against the baseline's 11 (measured, current scorer). It made fewer calls per utterance than either earlier system, 2.91 and 2.00 against the baseline's 3.18 and 2.56 (measured). It is now the prototype's default segmenter. With the client's fixed 3.1 s spacing gone, the snapshot was ready 0.27 s after the audio ended (median of 9 clips whose final decision made a live call). The baseline's prototype run took 2.25 s (median of 8, measured).

## Recommendation

- Keep the hybrid as plan A and as the prototype's default. It found 17 of 18 held-out breaks exactly, against the baseline's 12 and E's 16 (measured). It made 0 premature dispatches on the original set against the baseline's 1, and 1 against 1 on the held-out set (measured).
- Next, ask the break questions without the current pieces in view. On both scenarios where the hybrid merged two requests, it was the batched call that merged them. The same break question asked alone split them, with margins of 0.94 and 0.96 (measured, one probe each, in [Batching biases the break answers](#batching-biases-the-break-answers)). That change needs a fresh scenario set with soft breaks to tune on. The held-out set is spent, and every original break sits at a hard cut.
- Keep the adaptive pacer and the coalescing. The pacer never had to back off, because none of this round's 97 attempts drew a 429 (measured). Coalescing dropped 19 revisions over 6 clips and changed no task outcome (measured).
- Leave the implicit-request tier for later. The hybrid missed clips 07 and 10, as the baseline did (measured).

## How the hybrid finds breaks and classifies segments

`voice/segmentation/hybrid.py` holds the hybrid. It joins variant E's break-finding to the baseline's classification, and it reuses the baseline's `Orchestrator` for task records, corrections, delegates, and the dispatch policy.

### Hard cuts and soft cuts

The cursor is a word index, as in E. Committed segments sit before it, and the words after it split into pieces. Breaks come in two kinds.

- Hard cuts are the baseline splitter's cuts plus "I mean". Those are sentence punctuation, "and", "and then", "and also", "oh and", "but", "actually", "no wait", "never mind", and "anyway". The decider cannot merge across a hard cut. A correction therefore always gets its own segment, and the baseline's kind gate decides whether it corrects anything.
- Soft cuts sit at E's other markers. Those are commas, a bare "then", "also", and every word gap in a stretch of more than 12 words with no marker. A soft cut exists only where E's `finished` and `at` answers put one.

This split came from the variants round. The baseline missed breaks only where it had no marker, and E missed breaks only where it merged across one of the baseline's markers (measured in `docs/voice/stage2-variants.md`). Hard cuts keep the baseline's strength, and soft cuts add E's.

### One call per decision point

Each decision point makes one decisions call, and it holds every question.

| Question | Asked for | From |
| --- | --- | --- |
| `<sid>_kind`, `<sid>_ready`, `<sid>_side_effect`, `<sid>_depends`, `<sid>_corrects` | every segment, committed or not | the baseline, unchanged |
| `<did>_same_as` | each pending voice delegate | the baseline, unchanged |
| `<sid>_finished` (noul), `<sid>_at` (choice over numbered markers) | each piece whose end is in doubt | E, asked per piece |

A piece's end is in doubt when that end is a soft cut, when it is the open end of a partial transcript, or when a soft marker inside the piece could split it. The state is the baseline's state plus `new_speech`, which is the words after the cursor with a numbered marker at each candidate break. The median call carried 10 questions in the scenario runs (measured, maximum 17).

E's correction question is gone, because it cancelled real tasks in 4 held-out scenarios. E's dependency near-tie hold is gone too, because it broke `post_and_read`. A readiness near tie still holds dispatch. On a partial transcript, a `dispatch_now` that beat the runner-up by less than 0.2 becomes `wait`.

### Commits, holds, and the final transcript

`judge` from E turns each piece's two break answers into a verdict. An answer that puts the end inside a piece adds a soft cut, and an answer that puts it past a soft cut removes that cut. A piece commits when three things hold.

1. Every piece before it has committed.
2. This point's answer confirms its end. A hard end holds unless the answer is a near tie. A soft or open end holds only when the answer chose it.
3. E's stability rule passes. The same piece appeared at the previous decision point, or this revision repeats the previous transcript exactly. A side-effect piece needs both.

Until a piece commits, its task is held. The orchestrator can create, correct, or withdraw the task, but it cannot dispatch it. So a correction heard in the same call as the request it corrects supersedes that request before it can run. On the final transcript, nothing is applied until the break answers stop moving the pieces. If an answer moves a piece, the hybrid asks again (a drain call), at most 4 times. Then it applies everything at once, so a cancellation is read before any dispatch. That fixes the way E failed `cancel_second`.

### When the hybrid calls

The hybrid calls on the final transcript, for a voice delegate, when a marker appears or moves after the cursor, or when the words before the last marker change. It also calls for a second look, which happens when a piece from the last point could now commit. A transcript with no marker yet makes no call. So an utterance whose partials hold no marker costs one call, on the final transcript (measured on 5 held-out scenarios such as `email_extension`, on `07_intentional_repeat`, and on clips 01, 07, and 10).

## The scorer now matches on the instruction

The scorer used to match an expected task by the words of the task's segment. A correction's segment holds only the new words, so an expectation such as "height" and "massive" for "actually Mount Massive" never matched it. `voice/segmentation/score.py` now matches on the instruction, which for a correction is the old instruction plus the new words. The committed runs were rescored from their stored task records, with no new calls.

| Run | Original 11, old | Original 11, new | Held-out 18, old | Held-out 18, new |
| --- | --- | --- | --- | --- |
| Baseline | 9 | 9 | 8 | 11 |
| Variant E | 7 | 7 | 6 | 6 |

The baseline gained `actually`, `no_wait`, and `pause_correction` (measured). Its premature count on the held-out set fell from 4 to 1, and E's fell from 8 to 6 (measured). The old scorer counted the dispatch of a correction it could not match as premature. Every comparison below uses the new scorer.

## Results on the original 11

The hybrid ran once on the original 11 and passed all of them, so no threshold moved. They stay at E's values (`finished` 0.5, choice margin 0.2, side effect 0.5) and round 2's readiness margin of 0.2. Commit `a01b3452c` froze the code before the held-out run. "Early" counts dispatches on partial revisions.

| Scenario | Baseline | E | Hybrid | Calls, baseline / E / hybrid | Early, baseline / E / hybrid |
| --- | --- | --- | --- | --- | --- |
| 01_two_independent | pass | pass | pass | 3 / 6 / 3 | 1 / 1 / 1 |
| 02_two_dependent | pass | pass | pass | 3 / 6 / 3 | 0 / 1 / 1 |
| 03_topic_change | pass | FAIL | pass | 3 / 7 / 3 | 1 / 1 / 1 |
| 04_mid_sentence_correction | pass | FAIL | pass | 3 / 7 / 2 | 0 / 0 / 0 |
| 05_correction_after_dispatch | pass | pass | pass | 4 / 7 / 4 | 1 / 2 / 2 |
| 06_description_not_request | pass | pass | pass | 3 / 6 / 2 | 0 / 0 / 0 |
| 07_intentional_repeat | pass | FAIL | pass | 2 / 5 / 1 | 0 / 0 / 0 |
| 08_delegate_after_dispatch | FAIL | FAIL | pass | 4 / 6 / 4 | 1 / 1 / 1 |
| 09_delegate_before_segment | FAIL | pass | pass | 4 / 7 / 4 | 1 / 1 / 1 |
| 10_side_effect_email | pass | pass | pass | 4 / 7 / 3 | 0 / 0 / 0 |
| 11_filler_false_starts | pass | pass | pass | 2 / 6 / 3 | 0 / 0 / 0 |

Totals, baseline, E, then hybrid: 9, 7, and 11 pass; 1, 2, and 0 premature dispatches; 0 duplicates each; 0, 1, and 0 missed corrections; 35, 70, and 32 calls (measured).

Do not read much into the hybrid's two extra passes. In 08, the hybrid's final `depends` answer chose `s1` by a margin of 0.06 (measured), so a near tie broke its way. In 09, the hybrid's readiness answer for the ferry question was `dispatch_now` with margin 0.59, where the baseline's was `wait` (measured). The two calls carried different state, so the answers differ, but one sample cannot say which part of the state moved them. Every labeled break in this set sits at a hard cut, so this set does not exercise soft breaks.

## Results on the 18 held-out scenarios

The hybrid ran once on the held-out set, after the freeze.

| Scenario | Baseline | E | Hybrid | Calls, baseline / E / hybrid | Break error, baseline / E / hybrid |
| --- | --- | --- | --- | --- | --- |
| actually | pass | FAIL | pass | 3 / 3 / 2 | 0 / miss / 0 |
| assistant_question | pass | pass | pass | 2 / 3 / 1 | none labeled |
| buy_and_read | pass | FAIL | pass | 3 / 4 / 2 | 0 / 0 / 0 |
| cancel_second | pass | FAIL | pass | 3 / 4 / 3 | 0, 0 / 0, 0 / 0, 0 |
| delegate_overlap | pass | pass | pass | 3 / 4 / 2 | none labeled |
| delete_and_read | pass | pass | pass | 3 / 3 / 2 | 0 / 0 / 0 |
| email_extension | pass | pass | pass | 2 / 4 / 1 | none labeled |
| i_mean | FAIL | FAIL | pass | 2 / 3 / 2 | miss / miss / 0 |
| market_three | FAIL | FAIL | FAIL | 3 / 6 / 2 | miss, 0 / 0, 0 / miss, 0 |
| mixed_chain | FAIL | FAIL | FAIL | 2 / 5 / 3 | miss, 0 / 0, 0 / 0, 0 |
| museum_chain | FAIL | pass | pass | 3 / 5 / 3 | miss, 0 / 0, 0 / 0, 0 |
| near_duplicates | pass | FAIL | pass | 2 / 4 / 1 | 0 / 0 / 0 |
| no_wait | pass | FAIL | pass | 3 / 4 / 3 | 0 / 0 / 0 |
| pause_correction | pass | FAIL | pass | 3 / 4 / 2 | 0 / 0 / 0 |
| post_and_read | pass | FAIL | pass | 3 / 3 / 2 | 0 / 0 / 0 |
| rain_wonder | FAIL | FAIL | FAIL | 2 / 3 / 1 | none labeled |
| run_on | FAIL | pass | pass | 2 / 6 / 3 | miss, miss / 0, 0 / 0, 0 |
| true_repeat | FAIL | FAIL | FAIL | 2 / 4 / 1 | none labeled |

Totals, baseline, E, then hybrid: 11, 6, and 14 pass; 1, 6, and 1 premature dispatches; 1 duplicate each; 1, 2, and 0 missed corrections; 46, 72, and 36 calls; 7, 8, and 4 early dispatches (measured). The hybrid's 3 cuts away from any labeled break are the baseline's own 3, sentence cuts in `assistant_question`, `rain_wonder`, and `true_repeat`, where the classifier made no task from them (measured).

The hybrid failed four scenarios.

- `market_three` lost the yen request. On the final transcript the decider ended the first request at "rate," and not at "oats,", with margin 0.66 (measured). The next section shows that the batched call caused this merge.
- `mixed_chain` found both breaks, but the decider answered `depends: none` for "then make a shopping list" with margin 0.66, so the task dispatched instead of waiting on the recipe (measured). That dispatch is the hybrid's one premature dispatch on this set. The baseline's one, in `i_mean`, dispatched "find the capital of Austria" before the correction. The hybrid's hard "I mean" cut prevents that kind of premature dispatch.
- `rain_wonder` is an implicit request, which neither question set asks about (measured).
- `true_repeat` expects one task for an exact repeat, and the original `07_intentional_repeat` expects two. All three systems dispatched twice (measured).

## Batching biases the break answers

On `market_three` and on clip 09, the break question had the right marker available. The decider chose the merged span anyway. In the batched call, the state lists the current pieces as segments, and the classification questions quote them. So when a piece still holds two requests, the decider sees that piece presented as one segment.

`python -m voice.segmentation.probe_breaks` tests that premise. It resends a recorded hybrid request with only its `finished` and `at` questions, and with `new_speech` and `transcript_is_final` as the whole state.

| Request | Batched answer | Break questions alone |
| --- | --- | --- |
| `market_three`, final transcript, `s1_at` | `m2`, the merged span, margin 0.67 | `m1`, the split, margin 0.94 |
| Clip 09, final transcript, `s1_at` | `m2`, the merged span, margin 0.92 | `m1`, the split, margin 0.96 |

On both, the batched call merged and the unbatched call split (measured, one probe each). Round 2's finding that batching changed 1 answer in 115 covered classification questions about fixed segments. It does not carry over to break questions about segments the same call may redraw. Two designs would remove this bias, and neither has run. One makes two calls at a point where a piece has an open soft marker, first the breaks and then the classification. The other leaves such a piece out of the segment list until its break answer settles (inferred). Either needs scenarios with soft breaks that no one has tuned on.

## Mason's ten clips

The hybrid ran in the prototype on Mason's ten recordings with injection method (c), inside the GPU lease and `quiet_run.py`. The live ASR produced the same transcript update stream for every clip as the prototype round's pass 1 (measured), so the two runs saw identical input. The baseline column is that pass 1, rescored with the current scorer. The rescore changed nothing.

| Measurement | Baseline (prototype pass 1) | Hybrid |
| --- | --- | --- |
| Clips passed | 7 of 10 | 7 of 10 |
| Failed clips | 07, 09, 10 | 07, 09, 10 |
| Premature dispatches | 0 | 0 |
| Side-effect tasks dispatched early | 0 | 0 |
| Early dispatches | 1 | 2 |
| Decider calls, cached or live | 28 | 26 |
| Revisions dropped by coalescing | none (not built) | 19, over 6 clips |

Clip 09 failed the same way under both segmenters, but for a different reason. The baseline's splitter had no cut at the bare "then" in "Tell my dad I'm home then turn the lights on". The hybrid has a soft marker there, and the batched decider still ended the first request at "on", with margin 0.92 (measured). Asked alone, it split at "then" (measured, the probe above). Clips 07 and 10 are the implicit tier, which this round leaves out. Their result is recorded only.

### Snapshot latency

Times are seconds after the audio ended, as median and range, one run per clip.

| Seconds after the audio ends | Baseline (prototype pass 1) | Hybrid |
| --- | --- | --- |
| Final transcript | 0.10 (0.03 to 0.19), n=10 | 0.08 (0.03 to 0.19), n=10 |
| Snapshot ready | 1.21 (0.04 to 6.73), n=10 | 0.26 (0.05 to 0.31), n=10 |
| Snapshot ready, final decision made a live call | 2.25 (0.19 to 6.73), n=8 | 0.27 (0.16 to 0.31), n=9 |
| Last decision point, wall time minus call time | 2.02 (0.01 to 2.98), n=8 | 0.01 (0.01 to 0.05), n=9 |
| First reply audio | 1.44 (0.22 to 7.07), n=10 | 0.41 (0.24 to 0.50), n=10 |

Clip 08 shows the change at its worst case. The baseline queued two decisions on stale partials behind the 3.1 s spacing and had the snapshot ready at 6.73 s. The hybrid had it ready at 0.29 s (measured).

The spacing was the limit, and it is gone. The wall time of the last decision point beyond its call time fell from 2.02 s to 0.01 s (medians, measured). What remains is the final transcript (0.08 s median), one decider call (0.11 s median, 0.09 to 0.18), and the voice's 0.15 s to first audio after the snapshot (measured). The next 0.1 s would have to come from the ASR flush or from the decider call itself.

### What coalescing did

When newer transcript updates queue behind a decision, the prototype now processes only the newest. Coalescing dropped 19 revisions over 6 clips (measured). To see what it changed, `python -m voice.prototype.replay` fed the same updates to a fresh hybrid twice.

- Skipping the revisions the live run dropped reproduced the live task records and dispatch logs on all 10 clips, with no live call (measured). The replay is faithful.
- Processing every revision gave identical records on 9 clips. On clip 05, one decision point fell on an earlier revision, and the dispatches, their revisions, and the score stayed the same (measured, 2 live calls). The score stayed 7 of 10.

So coalescing shortened the queue and did not change what the segmenter decided here. That is 10 clips with one run each, so a clip with denser corrections could still differ (inferred).

## Calls, failures, and cost

All live calls ran inside `quiet_run.py --holder voice-hybrid`, one request in flight, between 02:49:15 and 02:56:57 UTC.

| Run | Attempts | Median call ms |
| --- | --- | --- |
| Hybrid, original 11 | 32 | 96.7 |
| Hybrid, held-out 18 | 36 | 97.1 |
| Hybrid in the prototype, Mason's 10 clips | 25 | 103.0 |
| Replay of every revision, Mason's clips | 2 | not timed |
| Probes, one per merge | 2 | not timed |

This round used 97 of its 250 attempts. 69 went to `voice/segmentation/calls.jsonl` and 28 to the prototype's ledger under `$VOICE_HOME`, which holds Mason's transcripts and is not committed. Every attempt returned HTTP 200, and every response reported `usage.cost` 0 (measured). No answer in the scenario runs was malformed (measured, `malformed` 0 in the report). Only one held-out utterance needed a drain call, `run_on`, and no original one did (measured).

The pacer's backoff never ran live. The variants round drew 92 429s in 363 attempts, and this round drew none in 97. The pacer's tests cover its 2 s and 4 s backoff and its decay. Whether the free tier tolerates back-to-back calls over a longer session is untested (guess).

## Limits of this evaluation

- The held-out set ran once and is now spent. The probe result came after it, so no change based on the probe has run.
- The original set has no soft break, so the soft-cut logic was tuned on nothing. Its held-out record is 17 of 18 breaks exact, from one run.
- Each request ran once. The decider looked deterministic on identical input in round 2, but the hybrid's 08 pass rests on a 0.06 margin.
- A pause is a revision whose transcript repeats the previous one exactly. In the prototype, that happens when the ASR completes an item, not after a set silence. On the clips it happened only on the final transcript, and no scenario committed on a pause (measured).
- The prototype ran each clip once and only with method (c). Its timings are single samples.

## Open decisions for Mason

- Whether the next round tries the unbatched break question. The probe says it splits both merged cases. It costs either a second call at points with an open soft marker, or a lag of one decision point before such a piece is classified.
- Whether a fresh scenario set with soft breaks (commas, bare "then", run-ons) should come first, so that change has something unseen to run on.

## Reproduce

Run from the repository root. The offline tests need no key and no network:

```
python -m unittest discover -s voice/segmentation/tests -t .
python -m unittest discover -s voice/prototype/tests -t .
python -m voice.segmentation.hybrid_report
```

`tests/test_hybrid.py` drives the hybrid through a scripted decider. It covers a bare "then" split, the two-point stability rule, a pause commit, a correction after a hard marker, the side-effect hold, a cancellation read before dispatch, the one-call path, the readiness near-tie hold, the final-transcript drain, a voice delegate, and malformed answers. `tests/test_client.py` covers the pacer, and `voice/prototype/tests/test_segmenter.py` covers coalescing. `test_replay.py` replays every committed run, including `stream-h` and `stream-h-heldout`, and compares task records, snapshots, and dispatch logs.

To repeat a live run, set `VOICE_ENV_FILE` to the env file that holds `OPENROUTER_API_KEY`, and wrap the command in `quiet_run.py`. Cached requests replay with no call.

```
python quiet_run.py --holder voice-hybrid -- python -m voice.segmentation.run --option h --mode stream
python quiet_run.py --holder voice-hybrid -- python -m voice.segmentation.run --option h --mode stream --set heldout
python quiet_run.py --holder voice-hybrid -- python -m voice.segmentation.probe_breaks decisions-95075f6c2323fd2c0adf451f.json
```

The prototype run and the replays need `$VOICE_HOME` and Mason's clips in `$VOICE_HOME/audio/real/`. The prototype run also needs the GPU lease:

```
python -m voice.prototype.run "$VOICE_HOME"/audio/real/0*.wav "$VOICE_HOME"/audio/real/10_noisy.wav --inject c --out "$VOICE_HOME/evidence/hybrid/run-h-c"
python -m voice.prototype.report "$VOICE_HOME/evidence/hybrid/run-h-c"
python -m voice.prototype.replay "$VOICE_HOME/evidence/hybrid/run-h-c" --segmenter hybrid --as-run --offline --out "$VOICE_HOME/evidence/hybrid/replay-h-asrun"
python -m voice.prototype.replay "$VOICE_HOME/evidence/hybrid/run-h-c" --segmenter hybrid --out "$VOICE_HOME/evidence/hybrid/replay-h-every"
```

`--segmenter baseline` runs the prototype with round 2's segmenter.

## Files outside the repo

| Path | Contents | Size |
| --- | --- | --- |
| `$VOICE_HOME/evidence/hybrid/` | The prototype run records, reply WAVs and server log, and three replays | 9.5 MB |
| `$VOICE_HOME/evidence/prototype/decider-cache/` | 28 new cached decider responses and ledger lines for Mason's clips | part of the existing cache |

No model, package, or venv was added.
