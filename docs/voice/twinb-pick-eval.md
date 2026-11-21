# Twin B: does a fast model pick the right thread?

**Bottom line.** No arm meets the design's bar, measured on the thread list the seam will actually
build. A case is one labeled request, and a run is one of its three repeats. A run is wrong when it
names a wrong thread or names one thread where a question was right.

The deterministic prefilter answers 112 of 190 cases and is wrong in 3 cases (9 runs). Two of them
are a project name that appears in one title. The third, L2-037, is a tokenization limit. The request
spells a compound word as one word, one of the two matching titles spells it as two, and only the
other title matches.
`particle-ai/particle-1:free` with reasoning off answers 169 of 190, reaches 98.6 percent correct on
the unambiguous slices, asks unnecessarily on 3.2 percent, and is wrong in 4 cases (12 runs). Three
of those four are prefilter hits that never reach the model. The model itself was wrong in one case,
L2-045, in all 3 of its runs, out of the 96 cases (288 calls) it answered. In all four wrong cases
the pick named one thread where the gold was a question, and none is a `wrong_thread` outcome. The
chain still fails the zero-wrong bar, and its 1610 ms single-request p95 is more than twice the
700 ms bar. Letting the same model break prefilter ties made every number worse. A local
Qwen2.5-1.5B in Q4 was wrong in 16 cases (47 runs), 9 of them a wrong thread, at 1399 ms.

Recommendation: keep the prefilter as the floor, send only misses to the free model, and fix the
prefilter rules next. [Next round](#next-round-an-in-sample-prefilter-rule-change) gives a rule
change that clears all four wrong cases on this data, in-sample. No arm here can hold a 700 ms spoken
turn at $0.

Every number below carries its label in its own sentence. The tables are run output.

## What ran

**Snapshots.** Four frozen lists, built from a read-only copy of a real T3 `statev2.sqlite` with
SQLite's backup API. Each one applies the seam's listing rule, listed lineage and not delegated, so a
subagent child, a side chat and a delegated thread never appear, and older settled threads are never
trimmed (measured in the builder's test and in the snapshots themselves). The copy held 1,137 active
threads, of which 973 were subagent children and 164 were listed (measured). The window and project
choices live in a local spec file, so no project name is baked into the code.

| snapshot | listed threads | ceiling total | outside the ceiling | settled beyond the 25 newest | window |
| --- | --- | --- | --- | --- | --- |
| A | 60 | 84 | 6 | 30 | five days, one project excluded |
| B | 57 | 83 | 6 | 32 | three projects, three weeks |
| C | 37 | 37 | 6 | 10 | two projects, every age |
| D | 54 | 85 | 6 | 26 | two weeks, two projects excluded |

The windowed snapshots set `truncated: true` because they hold fewer threads than their ceiling allows
at any age, which is what makes "not among your most recent threads" a true sentence (measured). Every
snapshot holds at least ten threads that settled after the 25 newest settled ones, and the older slice
asks about them.

**Labels.** 190 spoken requests. Seven were written by hand from the design's failure list,
including one for the state-word rule. The other 183 came from one labeler that
saw the four snapshots and the slice table and no picker output. Claude Opus 5.5 was not reachable in
this session, T3's delegate tool was not available and the two Opus routes refused on plan and billing,
so the labeler ran as Claude Sonnet 5.5 at high effort, the nearest model available.

The labels were not all fixed before any picker ran. This is the timeline in ET on 2026-11-17, from
file times (measured) and, for the aborted attempt, from the independent re-review:

1. 06:32. The labeler's file was written.
2. 06:34:38. The first manifest hashed the snapshots, the labels and the spec.
3. 06:34:52 to 06:35:22. An unscored model attempt ran and was aborted.
4. 06:38:02. One hand-written case, `kf-pr-thread`, moved to snapshot D after the data showed its
   gold was wrong on its first snapshot.
5. 06:38:07 (11:38:07 UTC). The manifest was rewritten as `labels/frozen-manifest.json`.
6. 06:38:12. The first reported run started.

Every run verifies that manifest before it starts (measured, `voice/t3/pick/manifest.py`). The first
manifest is kept at `$VOICE_HOME/evals/twinb-pick/scratch/round1/frozen-manifest-v1.json`.

**Arms.** Each ran 190 cases three times and a case passes only when all three runs pass (measured).

| arm | configuration |
| --- | --- |
| prefilter | no model. One allowed title containing every content word is the pick, a state word matches activity, a tie asks |
| model | prefilter first; a hit wins; a tie asks; a miss sends the whole allowed list to `particle-ai/particle-1:free`, temperature 0, 200 output tokens, reasoning off, provider sort latency, one request in flight paced 3.2 seconds apart, every reply cached |
| model_tie | the same client, plus the model may break a prefilter tie and sees only the tied titles |
| local | the same chain as `model` over `qwen2.5-1.5b-instruct-q4_k_m` on llama.cpp b12428 under the GPU lease, `-ngl 99`, 8192 context, JSON-schema constrained output, temperature 0 |

The local model had no copy on disk. One file was downloaded, 1.12 GB, Apache-2.0, at
`$VOICE_HOME/gguf/qwen2.5-1.5b-instruct-q4_k_m.gguf`, and its row is in
[licenses.md](licenses.md). The server took the schema shape on all 288 requests (measured: attempts
equal requests, so the first shape never fell back).

## Results

Cases that pass, over three repeats, strict view. A case passes only when all three runs are correct.

| slice | cases | prefilter | model | model_tie | local |
| --- | --- | --- | --- | --- | --- |
| exact | 30 | 21 | 30 | 30 | 22 |
| paraphrase | 40 | 16 | 39 | 39 | 17 |
| project | 15 | 0 | 9 | 9 | 4 |
| state | 20 | 15 | 17 | 15 | 15 |
| back reference | 15 | 15 | 15 | 15 | 15 |
| ambiguous | 25 | 16 | 22 | 22 | 16 |
| nonexistent | 15 | 15 | 14 | 14 | 11 |
| older | 10 | 4 | 10 | 10 | 4 |
| no subject | 10 | 0 | 3 | 3 | 5 |
| outside | 10 | 10 | 10 | 10 | 6 |
| total | 190 | 112 | 169 | 167 | 115 |

The lenient view, where a question is never a wrong thread and an unparseable reply reads as a
question, scores 113, 172, 171 and 122 (measured). The two views separate only where a reply could not
be parsed or a question missed a gold thread, so the strict column is the one to read.

The pass bar, with the latency metric named. It counts a run as wrong when it named the wrong thread or
picked one thread where a question was right.

| criterion | bar | prefilter | model | model_tie | local |
| --- | --- | --- | --- | --- | --- |
| wrong in ambiguous, nonexistent, outside | 0 | 3, fail | 6, fail | 6, fail | 9, fail |
| wrong overall | at most 2 | 9, fail | 12, fail | 15, fail | 47, fail |
| correct on the unambiguous slices, strict | at least 90 percent | 52.9, fail | 98.6, pass | 98.6, pass | 55.7, fail |
| unnecessary questions | at most 10 percent | 0.5, pass | 3.2, pass | 3.2, pass | 17.0, fail |
| `single_request_p95_ms` | at most 700 | no request, pass | 1610, fail | 1605, fail | 1399, fail |

`single_request_p95_ms` is the pooled p95 over every model request in the arm. The worst single slice
reads higher on small samples, 2193 ms for the model arm and 1763 ms for the local arm (measured), and
that number is in each arm's `summary.json` as `p95_single_request_worst_slice_ms`.

Latency, three ways (measured). The single-request column is the client's own timer around one HTTP
call. The wall column is the harness timer around the whole pick, so it includes the prefilter, the
prompt build and any backoff. Both p95 columns pool every run in the arm.

| arm | requests | p50 single request | p95 single request | p95 wall under eval load |
| --- | --- | --- | --- | --- |
| prefilter | 0 | none | none | 0.85 ms |
| model | 288 | 506 ms | 1610 ms | not recomputable |
| model_tie | 366 | 548 ms | 1605 ms | 3302 ms |
| local | 288 | 785 ms | 1399 ms | 1269 ms |

The model arm's pooled wall p95 cannot be recomputed. Its run files were re-scored from the disk
cache after the uncached run to prove the committed parser produced the same picks, and that replay
overwrote each run's `wall_ms` with a cache read. The only under-load figure left is 5070 ms, the
worst slice's wall p95 from the uncached run, in `runs/model/wall-under-load.txt` (measured).

Both latency metrics changed definition after the results were in. The arm summaries were written at
06:59 ET with the worst slice's wall p95. The commit that pooled the single-request p95 landed at
07:02, and the wall p95 was pooled in a later commit. Under both definitions every model arm fails
the 700 ms bar (measured).

The free tier allows 20 requests a minute. The first attempt at the model arm died after 41 calls when
the client gave up inside that window, so the arm was re-run with a paced client (measured: 41 calls,
then 255). That pacing is part of the configuration and is why the wall column is seconds, not
milliseconds.

**Tokens.** Reasoning off held. Every one of the 288 model calls and 288 local calls reported zero
reasoning tokens (measured). Content was 42 tokens per call for the model arm and 98 for the local arm
(measured: 12,098 and 28,185 tokens over the same call counts).

**Unparseable replies.** The model arm produced 6 unparseable replies in 570 runs, one case repeated
three times, where it listed every id and hit the 200-token cap (measured). The tie arm produced 9 and
the local arm 10, all from the same cause. Round 1 had 122 unparseable replies in 399, and 75 of those
had empty content because reasoning had used the whole token budget (measured on round 1's saved
replies, which is the correction below).

## The known failures

| case | what it checks | prefilter | model | local |
| --- | --- | --- | --- | --- |
| shared title prefix | two titles and eight seconds apart, so ask naming both | ask with both, correct | ask with both, correct | ask with both, correct |
| project-only mention | 45 threads in that project, so ask | named a thread, fail | named a thread, fail (prefilter hit) | named a thread, fail |
| "the PR thread" with no PR in the list | say none, never a thread | none, correct | asked, unnecessary question | none, correct |
| recency, many just finished | ask | ask, correct | ask, correct | ask, correct |
| deleted thread, absent | none | none, correct | none, correct | asked, unnecessary question |
| back reference after a launch | resolve from the task record | the record's thread, correct | the record's thread, correct | the record's thread, correct |
| "the one that failed" | match activity, not the title that says failed | asked with both failed threads, correct | asked with both, correct | asked with both, correct |

The state-word case is the one the prefilter was rewritten for. Its old rule matched the word failed
inside a title and spoke that thread; the new rule matches activity, and all three arms now ask with
the two threads that actually failed (measured). The project-only case still fails everywhere, because
one allowed title contains the project's name and the prefilter takes it as a single match before the
model is asked (measured).

After the runs, the prefilter's activity filter was changed to keep a title that carries the state
word. A request for "the broken login" now ties "Fix broken login" with a failed login thread instead
of naming the failed one. The change alters none of the 175 saved prefilter picks (measured by
re-running the prefilter over the saved cases, with no model call).

## What this round corrects from round 1

- Round 1 built its snapshots from every active thread, so 973 subagent children were in the lists the
  picker saw. The voice never sees those threads. Its numbers described a list the design does not
  produce, which is why they could not gate the design (measured: the 1,137 active rows and the 973
  subagent-lineage rows in the same copy).
- Round 1 said the model named a wrong thread in 5 of 180 cases. The saved runs hold 6 cases with a
  wrong answer and 18 runs, one case a `wrong_thread` and five `picked_when_should_ask` (measured).
- Round 1 said the prefilter made 3 wrong picks. Those were three identical runs of one deterministic
  case (measured).
- Round 1 blamed truncation on the 300-token cap. In 75 of the 122 unparseable replies the content was
  empty, so the token budget went to reasoning before any content was written (measured). Reasoning
  off removed that class; the 6 that remain come from a model that lists every id.
- Round 1 counted an unparseable reply as a correct question whenever the gold was a question. This
  round scores it as its own outcome and reports the lenient view beside the strict one, so the effect
  is visible rather than folded into the pass count (measured: the two views differ by 0 to 7 cases).

## What this metric cannot see

- It scores the pick only. A correct pick can still be spoken as a false sentence, which is the
  failure the end-of-utterance handoff already measured.
- Text only. No ASR error, no misheard "PR", no pause or repair.
- 190 cases, seven of them my own labels and 183 from one model labeler. A label a careful reader would
  argue flips a case. One ambiguous label is defensible either way and was kept as an ask. Its words
  all sit in one title, and they sit in a second title once a compound word is split.
- The candidate-set rule only bites where the gold names a set. A project-only mention and a request
  with no subject accept any question, so those slices measure "did it ask", not "did it ask well".
- The back-reference slice is decided by the task record before either arm runs, so it cannot separate
  the arms (measured, 15 of 15 everywhere).
- The ceiling is static. The snapshots bake in an allowlist, which is not the seam enforcing a ceiling
  on a live list.
- Three repeats at temperature 0 measure transport variance, not sampling variance.
- The model arm was paced to the free tier's 20 requests a minute. A deployed pick would not be paced
  and would meet 429s instead, which this run does not price.
- Nothing measures whether the spoken summary of the chosen thread is true, or whether the read is
  fresh. A thread can change between the pick and the read.

## Privacy: what was sent where

Thread titles and project names went as plain text to OpenRouter and nowhere else, in 929 calls
(measured). This round made 375 of them: 1 configuration probe, 41 in the aborted first attempt, 255
in the model arm and 78 in the tie arm. Round 1, in the same PR, made the other 554 over lists that
still included subagent-thread titles (measured, `scratch/round1/model-run.log`). Every call used a
model id ending in `:free` asserted in code, at cost 0, and neither round's log holds a 402 or billing
language (measured). In this round, the aborted attempt is the only set of calls that did not reach a
scored run. The cache replays made no calls. The local arm
sent nothing anywhere; the local server ran on this machine under the GPU lease.

Snapshots, labels, run files, raw replies and the ten scored outputs stay under
`$VOICE_HOME/evals/twinb-pick` and are not in the repository. That directory also holds the snapshot
spec, which names projects. Nothing from this eval is in the repository except this page, the picker
code, its tests on synthetic fixtures, and a license row.

## Recommendation

1. Keep the prefilter as the floor. It costs under a millisecond and answers 59 percent of the set
   (measured). It is not safe alone. It is wrong in 3 cases (9 runs), and one of them, L2-037, is in
   the ambiguous slice and wrong in all 3 runs (measured).
2. Send only misses to `particle-ai/particle-1:free` with reasoning off. It is the first configuration
   in this eval to reach 98.6 percent on the unambiguous slices with 3.2 percent unnecessary questions.
   The model itself was wrong in one case (3 runs) out of the 96 cases (288 calls) it answered
   (measured). The chain as it stands still fails the zero-wrong bar because three of its four wrong
   cases are prefilter hits.
3. Fix the three prefilter hits before adding a confidence gate. Two are a project name that appears
   in one title, so the prefilter names that thread for a request that names a project with dozens of
   threads. The third, L2-037, is the compound-word tokenization limit. The next section
   gives one rule for each.
4. Do not let the model break ties. It turned one correct case wrong in all 3 runs, a state-word
   question where 49 finished threads tied and the model picked one of them. It lost one more correct
   case to 3 unparseable replies (measured).
5. Drop the local 1.5B for this job. It mispicks exact titles, is wrong in 16 cases (47 runs), 9 of
   them a wrong thread, and takes 1399 ms (measured).
6. Treat the 700 ms bar as unmet by every arm at $0. The free tier's own queueing is the limiter, not the
   prompt size (inferred from a 506 ms p50 next to a 1610 ms p95). Either the pick runs ahead of the
   utterance, or the bar needs the PM's revision.

## Round 3: the fixed prefilter on a fresh held-out set

The three rule changes from the round 2 review are in this round (`voice/t3/pick/prefilter.py`,
each with a synthetic failing test first): `happening`, `happened` and `news` join `STOPWORDS`;
with no state word, when every topic word is a token of a listed project's name and that project
has two or more listed threads, ask instead of picking; and each adjacent pair of title words counts
in its joined form as a title token. The rules were written after reading round 2's failures, so the
recompute on round 2's own 190 cases is in-sample and the fresh 80 cases below are the test that
counts. Every number in this section carries its label in its own sentence.

**Step 1 recompute, deterministic, no model call.** Re-running the fixed prefilter over round 2's
saved 190 cases reproduces the committed `recompute-r2/summary.json` byte for byte (measured by
rerun). The prefilter goes from 112 to 142 correct cases, not the predicted 135, and from 9 wrong
runs to 0 (measured). The chain goes from 169 to 176, not 174, with 0 wrong runs (measured). The
extra 7 gains over the prediction are a finding: the re-review's hand recompute undercounted the
same rules, and the committed recompute is the number to trust because it runs the shipped code.
One changed case is neutral (a miss stays a miss in a different way); nothing that was correct
regressed (measured from the changed-case list).

**Fresh set.** Two new snapshots, E and F, built from a fresh read-only copy of the same database
with time windows and project subsets that do not overlap round 2's four (measured in the builder).
E lists 20 to 60 threads older than three weeks across every project; F lists three projects at every
age. Each holds settled threads beyond the 25 newest, so the older slice still asks about them
(measured). 80 new utterances: 13 hand-written known-failure cases first, then 67 from one labeler
that saw only the new snapshots and the slice table, never round 2's labels or any picker output
(measured). The manifest froze the snapshots, labels and spec at the builder commit before any picker
ran and every run verifies it, and the database copy was deleted after freezing (measured).

**Arms.** (1) fixed prefilter alone; (2) fixed prefilter plus `particle-ai/particle-1:free` with
reasoning off on misses only, ties ask, 3 repeats per case. The model's single pick is scored twice:
as a direct pick and as a confirming question, which is the design's default delivery (measured in
`harness.py:confirm_outcome`; a prefilter hit is still spoken directly). A case passes only when all
three runs pass (measured).

Cases that pass, over three repeats. Strict first, then lenient, then the confirming-question view,
then confirm-lenient.

| slice | cases | prefilter strict / lenient / confirm / confirm-lenient | model strict / lenient / confirm / confirm-lenient |
| --- | --- | --- | --- |
| exact | 13 | 9 / 9 / 9 / 9 | 12 / 12 / 12 / 12 |
| paraphrase | 17 | 3 / 3 / 3 / 3 | 17 / 17 / 17 / 17 |
| project | 6 | 1 / 2 / 1 / 2 | 4 / 5 / 4 / 6 |
| state | 8 | 3 / 3 / 3 / 3 | 7 / 8 / 7 / 8 |
| back reference | 6 | 6 / 6 / 6 / 6 | 6 / 6 / 6 / 6 |
| ambiguous | 10 | 6 / 6 / 6 / 6 | 8 / 9 / 8 / 9 |
| nonexistent | 6 | 6 / 6 / 6 / 6 | 6 / 6 / 6 / 6 |
| older | 5 | 0 / 0 / 0 / 0 | 5 / 5 / 5 / 5 |
| no subject | 4 | 0 / 0 / 0 / 0 | 0 / 0 / 0 / 0 |
| outside | 5 | 5 / 5 / 5 / 5 | 5 / 5 / 5 / 5 |
| total | 80 | 39 / 40 / 39 / 40 | 70 / 73 / 70 / 74 |

Cases and runs separately (measured). Prefilter strict: 39 of 80 cases pass, wrong in 1 case over
3 runs, all three the same ambiguous request where one title matched and the gold was a question.
Model as a direct pick: 70 of 80 cases pass, wrong in 2 cases over 6 runs. One is the prefilter hit
carried through. The other is the model's own miss in all 3 runs on a project request where two
threads share a name and the gold asks. Model as a confirming question: 70 of 80 cases pass, wrong
in 1 case over 3 runs, only the carried prefilter hit. Across 79 confirmation runs the model
confirmed a wrong thread zero times (measured). The lenient views add only question-quality cases,
never a wrong thread (measured).

The pass bar (measured). The prefilter fails wrong-overall at 3 runs and unambiguous-correct at
40.0 percent. The model chain passes unambiguous-correct at 97.8 percent and unnecessary-questions
at 0.8 percent in every view, and fails wrong-overall with 6 runs as a direct pick and 3 as a
confirming question, against a bar of at most 2. Nothing here holds a zero-wrong bar while a
single-match prefilter hit is spoken directly.

Latency, tokens, transport (measured). The model arm made 150 model calls over 240 runs plus 1
configuration probe, 151 API calls, every id ending in `:free` asserted in code, no 402 or billing
language in either log. Reasoning held at 0 tokens; content averaged 43 tokens a call. Single
request p50 570 ms, p95 1620 ms, worst-slice p95 2790 ms, wall p95 3901 ms against the 700 ms bar,
so the bar is still unmet at the free tier. 6 replies were unparseable, 2 cases in all 3 attempts
each, both listing ids past the parser. Nothing failed to parse on the prefilter arm, which makes
no calls.

The known failures on the fresh set, behavior only (measured). Ten of the 13 pass the prefilter
with no wrong answer; the model resolves one more, the older one, by direct pick. Two miss
everywhere by saying none where a thread exists. One asks but names a set that misses a gold
thread, in both arms, by the project-name rule. The shared-prefix, project-only, no-PR-in-list,
recency, deleted, back-reference, failed-word and compound-joined cases all behave as designed.

What this round cannot see. The 67 labeler cases come from one labeler and one reading of each
snapshot. Temperature-0 repeats measure transport variance, not sampling variance. The free tier
was paced to one request in flight, so a deployed pick would meet rate limits this run never
priced. The no-subject slice has no designed behavior yet: any question passes, and both arms say
none on all 4. A correct pick can still be spoken as a false sentence, which this eval does not
score.

**Recommendation: what Twin B ships.** Ship the prefilter as the floor with misses sent to the free
model, and deliver every single-thread result as a confirming question, prefilter hits included.
That delivery was wrong zero times in 79 model confirmations on data nobody tuned on, and the only
wrong runs left are the one direct prefilter hit it would have softened into a question (measured).
Do not ship direct picks: the model named a wrong thread outright once in 80 cases. Next, in order:
decide the no-subject behavior (always ask is the cheap answer, currently 0 of 4), retest the one
ambiguous single-hit class that the project-name rule does not cover, and either run the pick ahead
of the utterance or revise the 700 ms bar, which no $0 arm meets. The ten scored real outputs for
this round stay local at `$VOICE_HOME/evals/twinb-pick/round3/sample-10-scored.md`, alongside every
run file.

## Round A, name-only evidence (invalid comparison)

The measured d1.2 request p95 was 194 ms,
below the 700 ms bar, but d1.2 confirmed the wrong thread on one case in all three repeats.
Its strict confirmation score was 38 of 80 cases, compared with 70 of 80 for particle-1.
This round omitted evidence present in the recorded particle-1 input. Round B supersedes
it for the replacement decision.

In this configuration, `narrow+decider` ranked titles and project names by word overlap and
kept the top eight. Ties kept snapshot order, and zero-overlap rows could fill the list.
Every case went to d1.2, including back references. `full+decider` was eligible only for
lists of at most 12 items. Measured visible list sizes were 30 and 60, so no Twin B case
qualified. Both frozen manifests verified before the applicable runs.

Liquid received one typed question with the offered titles, project names, opaque option keys,
and words from listed names that also occurred in the request. The raw request, activity,
timestamps, listing IDs, and audio stayed local. The two extra choices were `none` and
`ask`. With no designated tuning split, the confidence floor was fixed at 0.20 before all
runs. A below-floor item choice or an explicit ask became a generic clarification without
named options. A strict question that requires named options can therefore fail.

The table reports measured case passes. A case passes only when all its repeats pass.
The four scores are strict, lenient, confirm, and confirm-lenient. The latency columns use
milliseconds. Request timing includes a connection setup when observed and excludes pacing.
Wall timing includes each arm's pacing and local work.

| Arm | Cases | Runs | Cases passed in the four views | Request p50 / p95 | Wall p50 / p95 | d1.2 calls | Liquid spend |
| --- | ---: | ---: | --- | --- | --- | ---: | ---: |
| Prefilter | 80 | 240 | 39 / 40 / 39 / 40 | None | 0.34 / 0.63 | 0 | $0 |
| Prefilter + particle-1 | 80 | 240 | 70 / 73 / 70 / 74 | 570 / 1620 | 2882 / 3901 | 0 | $0 |
| `narrow+decider` | 80 | 240 | 38 / 56 / 38 / 57 | 94 / 194 | 116 / 217 | 240 | $0.00128682 |
| `full+decider` | 0 | 0 | Ineligible | None | None | 0 | $0 |

Measured wrong counts by slice. Each cell gives distinct cases with a wrong result and then
wrong runs, in strict, lenient, and confirm order. Wrong includes a wrong direct pick, a direct
pick where gold requires asking, or a confirmation naming the wrong target.

| Slice | Cases / runs per eligible arm | Prefilter | Prefilter + particle-1 | `narrow+decider` |
| --- | ---: | --- | --- | --- |
| Exact | 13 / 39 | 0/0 · 0/0 · 0/0 | 0/0 · 0/0 · 0/0 | 1/3 · 1/3 · 1/3 |
| Paraphrase | 17 / 51 | 0/0 · 0/0 · 0/0 | 0/0 · 0/0 · 0/0 | 0/0 · 0/0 · 0/0 |
| Project | 6 / 18 | 0/0 · 0/0 · 0/0 | 1/3 · 1/3 · 0/0 | 0/0 · 0/0 · 0/0 |
| State | 8 / 24 | 0/0 · 0/0 · 0/0 | 0/0 · 0/0 · 0/0 | 0/0 · 0/0 · 0/0 |
| Back reference | 6 / 18 | 0/0 · 0/0 · 0/0 | 0/0 · 0/0 · 0/0 | 0/0 · 0/0 · 0/0 |
| Ambiguous | 10 / 30 | 1/3 · 1/3 · 1/3 | 1/3 · 1/3 · 1/3 | 1/3 · 1/3 · 0/0 |
| Nonexistent | 6 / 18 | 0/0 · 0/0 · 0/0 | 0/0 · 0/0 · 0/0 | 0/0 · 0/0 · 0/0 |
| Older | 5 / 15 | 0/0 · 0/0 · 0/0 | 0/0 · 0/0 · 0/0 | 0/0 · 0/0 · 0/0 |
| No subject | 4 / 12 | 0/0 · 0/0 · 0/0 | 0/0 · 0/0 · 0/0 | 0/0 · 0/0 · 0/0 |
| Outside | 5 / 15 | 0/0 · 0/0 · 0/0 | 0/0 · 0/0 · 0/0 | 0/0 · 0/0 · 0/0 |
| Total | 80 / 240 | 1/3 · 1/3 · 1/3 | 2/6 · 2/6 · 1/3 | 2/6 · 2/6 · 1/3 |

**Retrieval.** Measured once per case, the top eight included the gold thread in 39 of 46
single-target cases. Recall was 13/13 exact, 17/17 paraphrase, 2/2 project, 0/3 state,
2/6 back reference, and 5/5 older. Across all 65 cases with named gold IDs, 54 had at
least one gold ID in the shortlist and 51 had every gold ID. The wrong exact confirmation
occurred with the gold thread in the shortlist, so retrieval alone cannot account for it.

**Repeat and transport checks.** Measured all 240 Liquid attempts returned 200 and produced
a valid typed answer, with zero retries or billing errors. Strict confirmation passed 38
runs out of 80 in each repeat. Liquid request p95 ranged from 169 to 207 ms across the
three repeats, compared with 1413 to 1698 ms in the recorded particle-1 runs. Both
measurements cover their own hosted request paths, including different connections and
provider pacing policies. Inferred from that design, the latency difference describes
these configurations and cannot isolate model compute or predict live voice latency.

Measured d1.2 chose ask 132 times and none zero times. It asked on all 11 nonexistent
or outside cases. Those asks are not wrong picks under the rubric, but they each fail
the frozen gold. The name-only evidence omits the words that could distinguish an absent
name, an activity request, or a back reference. The eval cannot show how d1.2 would
perform with that context. The original run records remain in `runs/narrow+decider`.

## Round B, matched request evidence

**Keep particle-1 for thread picks.** Measured on the frozen set, d1.2 passed the latency
bar at 194 ms request p95 and got all 90 exact and paraphrase runs right. It still passed
49 of 80 cases with strict confirmation, compared with 70 for particle-1. It confirmed
one wrong thread in the state slice in all three repeats and asked unnecessarily on
47 of 240 runs, above the 10% gate.

Round B reused the particle-1 user message for the offered rows. It preserved the raw
request, snapshot date, title, project, activity, and last activity time in the original
listing order. Six back-reference cases also supplied the recorded thread ID that the
baseline used to bypass the model. Only text went to Liquid. The run kept the top eight,
three repeats, and the fixed 0.20 floor. When d1.2 asked, the local question named the
offered threads and scoring checked those same IDs. The visible lists held 30 or 60
threads, so there were no eligible full-list cases.

Measured scores give case passes in strict, lenient, confirm, and confirm-lenient order.
A case passes when all of its repeats pass. Request timing includes connection setup when
observed and excludes pacing. Wall timing includes pacing and local work.

| Arm | Cases | Runs | Cases passed in the four views | Request p50 / p95 (ms) | Wall p50 / p95 (ms) | Liquid calls | Liquid spend |
| --- | ---: | ---: | --- | --- | --- | ---: | ---: |
| Prefilter | 80 | 240 | 39 / 40 / 39 / 40 | None | 0.34 / 0.63 | 0 | $0 |
| Prefilter + particle-1 | 80 | 240 | 70 / 73 / 70 / 74 | 570 / 1620 | 2882 / 3901 | 0 | $0 |
| `narrow+decider` | 80 | 240 | 49 / 55 / 49 / 62 | 106 / 194 | 132 / 220 | 240 | $0.00436950 |
| `full+decider` | 0 | 0 | Ineligible | None | None | 0 | $0 |

Measured wrong counts give cases with at least one wrong result and wrong runs, in
strict, lenient, and confirm order. The recorded baseline breakouts are in Round A above.

| Slice | Cases / runs | `narrow+decider` wrong cases/runs in the three views |
| --- | ---: | --- |
| Exact | 13 / 39 | 0/0 · 0/0 · 0/0 |
| Paraphrase | 17 / 51 | 0/0 · 0/0 · 0/0 |
| Project | 6 / 18 | 1/3 · 1/3 · 0/0 |
| State | 8 / 24 | 2/6 · 2/6 · 1/3 |
| Back reference | 6 / 18 | 0/0 · 0/0 · 0/0 |
| Ambiguous | 10 / 30 | 5/11 · 5/11 · 0/0 |
| Nonexistent | 6 / 18 | 0/0 · 0/0 · 0/0 |
| Older | 5 / 15 | 0/0 · 0/0 · 0/0 |
| No subject | 4 / 12 | 0/0 · 0/0 · 0/0 |
| Outside | 5 / 15 | 0/0 · 0/0 · 0/0 |
| Total | 80 / 240 | 8/20 · 8/20 · 1/3 |

The measured retrieval limit remained 39 of 46 single targets in the top eight.
Only 2 of 6 recorded back-reference targets were offered, and those were the two
back-reference cases that passed. No single-target state case had its target offered.
Measured strict confirmation passed 51, 51, and 49 runs across repeats. Request p95
ranged from 163 to 204 ms. All 240 calls returned valid choices with zero retries.
The latency comparison covers the two hosted request paths, so it does not isolate
model compute. The baseline's larger candidate list and deterministic bypass also
limit what this measures about the models alone.

Round B raw and scored records are local in `runs/round-b-narrow+decider`. The PM's ten
current scores and reasons across both sets are in `$VOICE_HOME/evals/pick-decider/sample-10-scored.md`.

## Reproduce

```text
python -m unittest discover -s voice/t3/pick/tests -t .
python -m voice.t3.pick.manifest verify $VOICE_HOME/evals/twinb-pick
python -m voice.t3.pick.harness --arm prefilter --repeats 3
VOICE_ENV_FILE=<path to the env file with OPENROUTER_API_KEY> python -m voice.t3.pick.harness --arm model --repeats 3
VOICE_ENV_FILE=<path to the env file with OPENROUTER_API_KEY> python -m voice.t3.pick.harness --arm model_tie --repeats 3
python -m voice.t3.pick.harness --arm local --repeats 3      # against a llama-server on VOICE_LOCAL_URL
```

`harness.py` reads `$VOICE_HOME/evals/twinb-pick/{snapshots,labels}` and writes `runs/`. Every arm
verifies the frozen manifest first, so a changed label fails the run instead of being scored. The
snapshots come from `voice/t3/pick/snapshot.py` run against a copy of a `statev2.sqlite`.

Ten scored real outputs, with titles and reasons, are at `$VOICE_HOME/evals/twinb-pick/sample-10-scored.md`
for the morning read. Every run file is beside them under `runs/`. Round 3 reruns against
`$VOICE_HOME/evals/twinb-pick/round3` the same way, with `--root` pointing at that directory and no
`model_tie` or `local` arm; its ten are at `$VOICE_HOME/evals/twinb-pick/round3/sample-10-scored.md`.
The round 2 recompute reruns with no model call as `python round3/tools/recompute_r2.py`, and must
reproduce `round3/recompute-r2/summary.json` exactly.
