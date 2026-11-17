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

## Next round: an in-sample prefilter rule change

The independent re-review proposed the smallest prefilter change that clears the four wrong cases
above. It is not in this PR. The rules were written after reading these failures and scored on the
same 190 labels, so every number in this section is in-sample.

1. Add `happening`, `happened` and `news` to `STOPWORDS`. "happening" appears in 24 of the 190
   requests (measured). In L2-045 it was a content word that no title carries, so the prefilter
   missed and sent the request to the model, which made its only wrong pick there. Without it, the
   two matching titles tie and the prefilter asks.
2. When the request has no state word, every topic word is a token of a listed project's name, and
   that project has two or more listed threads, ask instead of picking. This covers the two
   project-name hits.
3. Count the joined form of each adjacent pair of title words as a title token, so a title that
   spells a compound word as two words also matches the one-word spelling. This covers L2-037.

The re-review recomputed both picks on the saved data with no model call (measured by the re-review,
not rerun here). The prefilter alone goes from 112 to 135 correct cases and from 9 wrong runs to 0,
and loses 0 correct answers. The chain goes from 169 to 174 correct cases and from 4 wrong cases to 0.
Confirm that on a fresh labeled set, about 80 new requests on new snapshots, before trusting "0 wrong".

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
for the morning read. Every run file is beside them under `runs/`.
