# Stage 2 segmentation findings

Measured on 2026-11-15 (US Eastern) against `perplexity/pplx-decider-flash-v1.3:free`. Every number below is regenerated from committed files by `python -m voice.research.segmentation.report`, which writes `voice/research/segmentation/results/report.json`.

The free decider can drive segmentation when a deterministic clause splitter proposes the segments and the decider classifies all of them in one call. On the streamed scenarios, that design passed 9 of 11 with 0 duplicate dispatches, 0 missed corrections, and 1 premature dispatch (measured). The median decision latency was 93 ms, and network time to OpenRouter was about 11 ms of that (measured). Both failures trace to answers whose top two options were within 0.2 of each other (measured).

## Recommendation

- Use option (a) for Stage 3. A deterministic clause splitter proposes segments, and one bundled decisions call per boundary change classifies them. It passed 10 of 11 final transcripts, against 4 of 11 for decider boundary confirmation and 5 of 11 for decider extraction (measured).
- Keep the application-side dispatch policy as built. Hold the trailing segment, hold side effects until the final transcript, and supersede or cancel-request on correction. In the streamed runs, no side-effect task dispatched on a partial, and no superseded version ever dispatched (measured).
- Add a margin gate before Stage 3 relies on dependency and readiness answers. 7 of 97 dependency answers and 6 of 215 readiness answers had a margin under 0.2 (measured). The one premature dispatch came from a dependency answer with margin 0.15 (measured). A gate that holds such answers would have kept that task pending (inferred, not run). Evaluate the gate on a new scenario set, because tuning it on these 11 would fit the test.
- Before trusting early dispatch, add a scenario that dispatches a non-side-effect request early and then corrects it. No live run reached `cancel_requested`, because the decider marked "find flights" as a side effect and the policy held it (measured). Only a unit test covers that path.

## The decider's API

Round 1 found the endpoint. This round mapped the question types from the docs and confirmed the free id's behavior with probes. The probe responses are `voice/research/segmentation/cache/decisions-87a8e98a28a5090984c7eea7.json` and `voice/research/segmentation/cache/extractions-441877dd3ce17ccf21d59b39.json`.

- Decisions is `POST https://openrouter.ai/api/alpha/decisions` with `{model, state, questions}`. `state` can be a string, an object, or an array. The probe sent an object (measured).
- The [Decisions API reference](https://openrouter.ai/docs/api/api-reference/alphadecisions/submit-a-decisions-request) defines four question types. `noul` is a true or false proposition and returns a probability in `noul`. `choice` picks one key of `criteria` and returns `choice`, `probabilities`, and `confidence`. `score` picks a point on an ordered scale. `box` returns an image region. The [multimodal decisions guide](https://openrouter.ai/docs/guides/community/multimodal-decisions) lists `noul`, `choice`, and `score` for Perplexity endpoints and not `box`. An answer can also be `{"type": "refusal"}`.
- The docs state no limit on questions per call or choices per question. Calls in this round carried 1 to 13 questions, and the largest choice set had 5 options (measured).
- `confidence` is the top probability minus the second, not the top probability. In round 1's probe, recorded in `voice/research/segmentation/round1-probe.json`, 0.9528 - 0.0472 = 0.9056 (measured).
- Extractions is a separate route, `POST https://openrouter.ai/api/alpha/extractions`, with `fields` in place of `questions`. The [extractions guide](https://openrouter.ai/docs/guides/community/extractions) lists the Perplexity deciders with `text` fields and `list` fields of `text`, up to 128 fields. A list answer carries `spans` as `[start, end)` code-point offsets into `state`. The free id serves it and returned spans (measured).
- The same request with a different `user` tag returned identical answers on all 415 questions compared (measured). The free decider looks deterministic for identical input (inferred), so answers that flip between revisions come from the changed input, not from sampling.
- Every response reported `usage.cost` 0, all 157 of them (measured). The key endpoint reported a free-model daily limit of 1,000 requests and `is_free_tier` false. It also reported a `limit` of 3 with `usage` 0 (measured). That `limit` is probably a $3 spending cap (guess). The client treats any nonzero cost as a billing event and stops.

## The decision contract

The decider can only pick among options the caller offers, so it cannot invent spans. Each decision point therefore has three parts.

1. Input. The orchestrator receives a transcript update, `{revision, text, is_final}`, where `text` replaces the whole utterance.
2. Candidate segments. A proposer turns `text` into segments `{sid, start, end, text, open}`. Each segment is a `[start, end)` span of `text`, and only the last segment of a partial transcript is `open`.
3. One decisions call. `state` holds the transcript, `transcript_is_final`, the segments by id, the open segment's id, the current task records, and any pending delegate submissions. The call asks these questions for each segment.

| Question            | Type                             | Options                                                                                  |
| ------------------- | -------------------------------- | ---------------------------------------------------------------------------------------- |
| `<sid>_kind`        | choice                           | `request`, `description`, `chitchat`, `correction`, `topic_change`                       |
| `<sid>_ready`       | choice                           | `dispatch_now`, `wait`, `clarify`                                                        |
| `<sid>_side_effect` | noul                             | true when carrying it out sends, contacts, buys, books, posts, or changes someone's data |
| `<sid>_depends`     | choice, segments after the first | `none` or one earlier segment id                                                         |
| `<sid>_corrects`    | choice, segments after the first | `none`, `replace_<earlier sid>`, or `cancel_<earlier sid>`                               |
| `<did>_same_as`     | choice, per delegate submission  | `new` or one segment id                                                                  |

`voice/segmentation/contract.py` builds the request and parses the answers. Parsing is the trust boundary. A choice outside the offered set, a refusal, or a missing answer becomes a problem string. Any problem on a segment drops that segment's decision, and its task stays unchanged until the next decision point. No live response was malformed for option (a) (measured). Option (c) had 3 extraction answers with `"value": null` (measured).

## Candidate spans: three options compared

Each option ran once on the final transcript of all 11 scenarios. Per scenario, the requests count both live calls and cache hits.

| Option                          | How it proposes segments                                                                                                                                                     | Passed   | Requests | Decider calls per decision point | Failure pattern                                                                                                |
| ------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------- | -------- | -------------------------------- | -------------------------------------------------------------------------------------------------------------- |
| (a) clause splitter             | Cuts at sentence punctuation and at spoken markers: "and", "and then", "and also", "oh and", "but", "actually", "no wait", "never mind", "anyway". Drops filler-only pieces. | 10 of 11 | 11       | 1                                | 09 only, a readiness answer with margin 0.33                                                                   |
| (b) decider confirms boundaries | Cuts at (a)'s points plus every comma, then asks one `noul` per boundary and merges the pieces where the answer is false                                                     | 4 of 11  | 22       | 2                                | Merged real boundaries ("and then summarize it", "now roll it again") and split "Actually" from its correction |
| (c) decider extraction          | One `list` field of `text`, using the returned `spans`                                                                                                                       | 5 of 11  | 19       | 2                                | Returned no list on 3 transcripts, and dropped corrections and second requests                                 |

The results files are in `voice/research/segmentation/results/final-a/`, `voice/research/segmentation/results/final-b/`, and `voice/research/segmentation/results/final-c/`. Options (b) and (c) failed at segmentation itself, and each needs two sequential calls per decision point. Streaming them would have cost about 130 more calls to confirm the same loss, so only option (a) ran on the full streams.

Bundling all segments into one call changed 1 of 115 answers compared with one segment per call (measured, `final-a` against `final-a-separate`). The changed answer was 09's readiness, which had a margin of 0.32 or 0.33 in the two runs. One segment per call passed 11 of 11 but needed 25 requests instead of 11. Bundling keeps one round trip per decision point, and the one difference is a near tie, so the recommendation is to bundle.

## Task records and dispatch policy

`voice/segmentation/tasks.py` owns the task records. Each record has `task_id`, `status`, `segment_text`, `instruction`, `sources`, `depends_on`, `revises`, `side_effect`, and an event `history`. A transition table defines the legal status changes. An illegal change raises `IllegalTransition`.

| From                                       | To                                                                             |
| ------------------------------------------ | ------------------------------------------------------------------------------ |
| `proposed`, `ready`, `needs_clarification` | each other, `dispatched`, `superseded`, `withdrawn`                            |
| `withdrawn`                                | `proposed`, `ready`, `needs_clarification` (a reclassified segment comes back) |
| `dispatched`                               | `cancel_requested` only                                                        |
| `superseded`, `cancel_requested`           | nothing                                                                        |

At each decision point, the orchestrator applies these rules. The decider never changes a status itself.

- Binding. A segment keeps its task across revisions if its start offset is unchanged. Otherwise it keeps the task that shares at least 60% of the shorter text's words. An undispatched orchestrator task with no matching segment becomes `withdrawn`.
- Requests. A `request` segment creates or updates a task. A segment reclassified as another kind withdraws its undispatched task.
- Readiness. `clarify`, or `wait` on the final transcript, gives `needs_clarification`. `wait` on a partial gives `proposed`. A task with a dependency stays `ready`, because the mock worker never completes. The open trailing segment stays `proposed`. A side-effect task stays `ready` until the final transcript. Every other case dispatches.
- Corrections. `replace_<sid>` supersedes the target if it has not dispatched, or requests its cancellation if it has. It then creates a task with `revises` set to the target and the instruction "<old instruction> (corrected: <new text>)". `cancel_<sid>` withdraws the target or requests its cancellation, and creates no task.
- Delegates. A delegate submission that matches a segment links to that segment's task and adds the `voice_delegate` source. It dispatches the task only if the task has not already dispatched. An unmatched submission becomes its own dispatched task.

The end-of-utterance snapshot follows the plan's section 5 shape. It maps `dispatched` to `running`, `proposed` and `ready` to `pending`, and keeps `needs_clarification` and `cancel_requested`. It leaves out `superseded` and `withdrawn` tasks, because they never ran. Here is the snapshot from `voice/research/segmentation/results/stream-a/05_correction_after_dispatch.json`:

```json
{
  "type": "utterance_snapshot",
  "conversation_id": "conv_stage2",
  "utterance_id": "utt_05_correction_after_dispatch",
  "final_transcript": "Find flights to Austin on Friday and check hotel prices downtown. Actually, make the flights Saturday.",
  "tasks": [
    { "task_id": "task_2", "description": "and check hotel prices downtown", "status": "running" },
    {
      "task_id": "task_3",
      "description": "Find flights to Austin on Friday (corrected: Actually, make the flights Saturday.)",
      "status": "running"
    }
  ],
  "clarification_needed": null
}
```

## Call policy

The runner calls the decider when the segment count changes, when a closed segment's text changes, when a delegate submission arrives, or on the final transcript. Growth of the open trailing segment alone triggers no call, because the policy never dispatches that segment on a partial. The streamed scenarios had 65 revisions and 36 decision points. Those points made 35 calls, because one point had no segments (measured). Calling on every revision would have made 65 calls.

## Scenario results

The streamed run is `stream-a`, option (a) over every revision. Its repeat, `stream-a-repeat1`, gave the same answers and the same scores. "Early" counts dispatches on partial revisions. The last column gives premature, duplicate, and missed corrections. Each row's task records, event history, decisions, and snapshot are in `voice/research/segmentation/results/stream-a/<scenario>.json`.

| Scenario                     | Expected (hand-written)                                                     | Actual                                                                              | Result | Revisions / points / calls | Early | Prem / dup / missed |
| ---------------------------- | --------------------------------------------------------------------------- | ----------------------------------------------------------------------------------- | ------ | -------------------------- | ----- | ------------------- |
| 01_two_independent           | T1 dispatched; T2 dispatched                                                | task_1 dispatched; task_2 dispatched                                                | pass   | 6 / 3 / 3                  | 1     | 0 / 0 / 0           |
| 02_two_dependent             | T1 dispatched; T2 ready on T1                                               | task_1 dispatched; task_2 ready on task_1                                           | pass   | 6 / 3 / 3                  | 0     | 0 / 0 / 0           |
| 03_topic_change              | T1 dispatched; T2 dispatched                                                | task_1 dispatched; task_2 dispatched                                                | pass   | 6 / 3 / 3                  | 1     | 0 / 0 / 0           |
| 04_mid_sentence_correction   | T1 superseded or withdrawn; T2 dispatched on final, revises T1              | task_1 superseded; task_2 dispatched on final, revises task_1                       | pass   | 7 / 3 / 3                  | 0     | 0 / 0 / 0           |
| 05_correction_after_dispatch | T1 cancel_requested or superseded; T2 dispatched; T3 dispatched, revises T1 | task_1 superseded; task_2 dispatched; task_3 dispatched, revises task_1             | pass   | 7 / 4 / 4                  | 1     | 0 / 0 / 0           |
| 06_description_not_request   | T1 dispatched; no email task                                                | task_1 dispatched                                                                   | pass   | 6 / 3 / 3                  | 0     | 0 / 0 / 0           |
| 07_intentional_repeat        | T1 dispatched; T2 dispatched or ready                                       | task_1 dispatched; task_2 ready on task_1                                           | pass   | 4 / 2 / 2                  | 0     | 0 / 0 / 0           |
| 08_delegate_after_dispatch   | T1 dispatched once, both sources; T2 ready on T1                            | task_1 dispatched once, both sources; task_2 dispatched                             | FAIL   | 5 / 4 / 4                  | 1     | 1 / 0 / 0           |
| 09_delegate_before_segment   | T1 dispatched once, both sources; T2 dispatched                             | task_1 dispatched once, both sources; task_2 needs_clarification                    | FAIL   | 6 / 4 / 4                  | 1     | 0 / 0 / 0           |
| 10_side_effect_email         | T1 dispatched on final with "Thursday"; T2 dispatched or ready              | task_1 held from revision 4, dispatched on final with "Thursday"; task_2 dispatched | pass   | 6 / 4 / 4                  | 0     | 0 / 0 / 0           |
| 11_filler_false_starts       | T1 dispatched                                                               | task_1 dispatched                                                                   | pass   | 6 / 3 / 2                  | 0     | 0 / 0 / 0           |

Totals: 9 of 11 pass, 5 early dispatches, 1 premature dispatch, 0 duplicate dispatches, and 0 missed corrections (measured).

- 08 failed on a dependency. At revisions 4 and 5, the decider answered `s2_depends` with `s1` and then `none`. On the final transcript, `none` got 0.575 and `s1` got 0.425, so "and tell me the total travel spend" dispatched without waiting for the spreadsheet. The scorer counts that dispatch as premature. In the final-only run the same scenario passed (measured). That run's state held no prior tasks and carried the delegate submission in the same call. Which of those differences moved the answer is unknown.
- 09 failed on readiness. On the final transcript, the decider answered "and, um, what time does the ferry leave?" with `wait` 0.47, `clarify` 0.27, and `dispatch_now` 0.26, and the policy turns `wait` on a final transcript into `needs_clarification` (measured). The question does not say which ferry, so asking is defensible (guess). The expectation stays as it was written before the runs.
- The side-effect answers on final transcripts flagged all 3 real side-effect segments: both booking segments and the email. They also flagged 2 of the 18 other request and correction segments, "Find flights to Austin on Friday" and its correction (measured). Both are lookups, so the error held a task too long rather than sending something early.

## Latency and failures

Every model call ran inside `quiet_run.py --holder voice-stage2`, one request at a time, on one persistent HTTPS connection. The timer starts when the request is sent and stops when the whole response body has been read. Cache hits carry no timing, and each timed record comes from exactly one live attempt. The runs took place between 00:21:42 and 00:29:05 UTC on 2026-11-16.

| Measurement                                              | n   | Median ms | p95 ms | Min ms | Max ms |
| -------------------------------------------------------- | --- | --------- | ------ | ------ | ------ |
| Decisions call                                           | 145 | 92.7      | 221.0  | 79.2   | 899.9  |
| Extractions call                                         | 12  | 109.5     | 208.6  | 96.8   | 302.8  |
| `GET /api/v1/key` on the warm connection (network floor) | 80  | 11.4      | 20.2   | 7.5    | 1020.3 |
| TCP and TLS connect                                      | 11  | 52.5      | 301.5  | 28.1   | 301.5  |
| Decisions call under 1,000 input tokens                  | 109 | 90.9      | 212.6  | 79.2   | 899.9  |
| Decisions call at 1,500 input tokens or more             | 8   | 111.8     | 163.9  | 86.8   | 163.9  |

- The network is not the dominant term (measured). A round trip to the OpenRouter edge with authentication costs about 11 ms. The other 81 ms of the median decision sits behind the edge, in routing to Perplexity and in inference. This client cannot split those two without direct access to the provider (inferred).
- Doubling the input from about 700 to about 1,500 tokens moved the median by about 21 ms, on only 8 large calls (measured). The 305 ms and 900 ms outliers have no link to tokens or question count. They look like upstream jitter (inferred).
- Medians were stable from run to run, between 90.8 and 98.8 ms per run. Single calls were not stable. Between `stream-a` and its repeat, the median paired difference was -1.5 ms over 35 matched requests, but single pairs differed by -121 to +218 ms (measured). The per-run figures include the 3 cache hits, which reuse the original call's timing.
- Under option (a), the dispatch decision lands about 93 ms after a boundary appears in the transcript (median, measured). Options (b) and (c) need two sequential calls, about 190 ms (inferred from the medians).
- Failures. The ledger, `voice/research/segmentation/calls.jsonl`, logged 157 attempts, all HTTP 200, with no 429s, timeouts, retries, or billing signals (measured). For option (a), no answer was malformed or refused (measured).

## Call budget

This round used 157 of its 400-call budget, counted from `voice/research/segmentation/calls.jsonl`. Each attempt was a distinct request, and each has one cache file in `voice/research/segmentation/cache/`.

| Purpose                                                            | Calls        |
| ------------------------------------------------------------------ | ------------ |
| API probes (extraction spans, object state with several questions) | 2            |
| Option comparison on final transcripts: (a), (b), (c)              | 11 + 20 + 19 |
| Option (a) over all revisions                                      | 35           |
| One segment per call, for the bundling comparison                  | 24           |
| Repeat of the final and streamed runs, for determinism             | 11 + 35      |

Round 1 made 1 more call, recorded in `voice/research/segmentation/round1-probe.json`. The script that made it is gone, and `voice/research/segmentation/probes.py` makes the same kind of call through the shared client. The key endpoint and network-floor GETs are not model calls, so the budget does not count them. Re-running any command replays from the cache and makes no calls.

## Limits of this evaluation

- One author wrote all 11 scenarios and their expectations. The ASR partials are synthetic, with no audio timing, and they follow a simple growth pattern. Real partials revise more words and arrive at uneven intervals.
- The splitter's marker list is English only. It will cut noun phrases such as "salt and pepper" in two (guess, untested). The decider would then have to classify "pepper" as a fragment.
- Matching expected tasks by words in the task's segment text is coarse. A scenario with two similar requests would need a stricter key.
- Each request ran once, apart from the repeat check. The decider answered identically on repeats, so these scores are stable for these inputs but come from a small sample.

## Reproduce

Run from the repository root. The offline tests need no key and no network:

```
python -m unittest discover -s voice/segmentation/tests -t .
```

```
----------------------------------------------------------------------
Ran 28 tests in 0.102s

OK
```

`test_replay.py` re-runs all seven recorded runs from `voice/research/segmentation/cache/` and checks that each one reproduces its committed task records, snapshot, and score. `test_orchestrator.py` and `test_contract.py` cover the dispatch policy, corrections, delegates, revisions, the transition table, the splitter, and answer parsing.

To repeat a live run, set `VOICE_ENV_FILE` to the env file that holds `OPENROUTER_API_KEY`, or set the variable itself. Then wrap the command in `quiet_run.py`:

```
python quiet_run.py --holder voice-stage2 -- python -m voice.segmentation.run --option a --mode stream
python -m voice.research.segmentation.report
```

`voice.segmentation.run` takes `--option a|b|c`, `--mode stream|final`, `--separate`, `--repeat N`, and `--offline`. `--offline` fails on any request that has no recorded response.
