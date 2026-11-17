# Twin B: does a fast model pick the right thread?

**Bottom line.** No arm meets the design's bar, measured on the thread list the seam will actually
build. The deterministic prefilter answers 112 of 190 requests and names a wrong thread in 3 cases,
all of them a project name that happens to appear in one title. `particle-ai/particle-1:free` with
reasoning off answers 169 of 190, reaches 98.6 percent correct on the unambiguous slices, asks
unnecessarily on 3.2 percent, and names a wrong thread in 4 cases; three of those four are prefilter
hits that never reach the model, so the model itself made one wrong pick in 288 calls. It still fails
the zero-wrong-thread bar, and its 1610 ms single-request p95 is more than twice the 700 ms bar.
Letting the same model break prefilter ties made every number worse, and a local Qwen2.5-1.5B in Q4
named 16 wrong threads at 1399 ms. Recommendation: keep the prefilter as the floor, send only misses
to the free model, and put the next run into the three prefilter hits and the one miss that the model
answered from a full list. No arm here can hold a 700 ms spoken turn at $0.

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

**Labels.** 190 spoken requests, written before any picker ran. Seven were written by hand from the
design's failure list, including one for the state-word rule. The other 183 came from one labeler that
saw the four snapshots and the slice table and no picker output. Claude Opus 5.5 was not reachable in
this session, T3's delegate tool was not available and the two Opus routes refused on plan and billing,
so the labeler ran as Claude Sonnet 5.5 at high effort, the nearest model available. The snapshots, the
labels and the spec were hashed into `labels/frozen-manifest.json` at 2026-11-17T11:38:07Z, before the
first scored run, and every run verifies that manifest before it starts (measured, `voice/t3/pick/manifest.py`).
A later fix moved one hand-written case to another snapshot after the data showed its gold was wrong;
the manifest was rewritten once at that point and the earlier version was kept (measured).

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
prompt build and any backoff.

| arm | requests | p50 single request | p95 single request | p95 wall under eval load |
| --- | --- | --- | --- | --- |
| prefilter | 0 | none | none | 1.1 ms |
| model | 288 | 506 ms | 1610 ms | 5070 ms |
| model_tie | 366 | 548 ms | 1605 ms | 3805 ms |
| local | 288 | 785 ms | 1399 ms | 1763 ms |

The model arm's wall column comes from its uncached run. The run files beside it were re-scored from
the disk cache after that run to prove the committed parser produced the same picks, which made their
wall times a cache read; the measured under-load figure is kept in `runs/model/wall-under-load.txt`
(measured).

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
  argue flips a case. One ambiguous label, the weights-lab request that names words from two titles, is
  defensible either way and was kept as an ask.
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

Thread titles and project names went as plain text to OpenRouter and nowhere else: 1 configuration probe,
41 calls in the aborted first attempt, 255 in the model arm and 78 in the tie arm, every one with a model
id ending in `:free` asserted in code, at cost 0, with no 402 and no billing language (measured). The
aborted attempt and the cache replays are the only calls that did not reach a scored run. The local arm
sent nothing anywhere; the local server ran on this machine under the GPU lease.

Snapshots, labels, run files, raw replies and the ten scored outputs stay under
`$VOICE_HOME/evals/twinb-pick` and are not in the repository. That directory also holds the snapshot
spec, which names projects. Nothing from this eval is in the repository except this page, the picker
code, its tests on synthetic fixtures, and a license row.

## Recommendation

1. Keep the prefilter as the floor. It costs under a millisecond, never names a wrong thread in the
   ambiguous or nonexistent slices, and answers 59 percent of the set (measured).
2. Send only misses to `particle-ai/particle-1:free` with reasoning off. It is the first configuration
   in this eval to reach 98.6 percent on the unambiguous slices with 3.2 percent unnecessary questions,
   and it made one wrong pick in 288 calls (measured). The chain as it stands still fails the
   zero-wrong-thread bar because three wrong picks come from prefilter hits.
3. Fix the three prefilter hits before adding a confidence gate. Each is a project name that appears in
   one title, so the prefilter names that thread for a request that names a project with dozens of
   threads. A pre-check that asks when a spoken word is a project name with more than one listed thread
   covers two; an ask when a named word is shared by two titles covers the third.
4. Do not let the model break ties. It cost 3 wrong picks and 2 correct ones, and the extra wrong picks
   were state-word questions where a "tie" of dozens of finished threads should stay a question
   (measured).
5. Drop the local 1.5B for this job. It mispicks exact titles, names 16 wrong threads and takes 1399 ms
   (measured).
6. Treat the 700 ms bar as unmet by every arm at $0. The free tier's own queueing is the limiter, not the
   prompt size (inferred from a 506 ms p50 next to a 1610 ms p95). Either the pick runs ahead of the
   utterance, or the bar needs the PM's revision.

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
