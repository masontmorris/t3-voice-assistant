# End-of-utterance handoff: no decider call after the speech ends

Mason's decision of 2026-11-16 reads "we shouldn't run the decider at the end of utterances; authority should be handed off to lfm at that point." This round builds that decision into the prototype and measures it against the current prototype on the same audio. Each claim says whether it is measured, inferred, or a guess. Measured numbers come from one run per arm over 14 clips: Mason's 10 recordings and Stage 1's 4 synthetic clips. Both arms ran inside the GPU lease and `quiet_run.py`, with LFM3 at `-c 32768` and every decider request sent live.

## Result

The handoff now runs by default. As built, it made the assistant slower to answer and worse at tasks on these clips.

- **The first reply came later.** Audio started 0.60 s after the speech ended against 0.45 s before (medians, measured). The max was 0.92 s against 0.52 s. LFM3's tool turn took 0.42 s (median, measured), and it replaced a decider call that took 0.11 s (median wall time, measured).
- **Task outcomes got worse.** 16 of 26 expected tasks came out right against 23, and 4 of 14 clips passed against 10 (measured). No task dispatched early in either arm. The handoff made 2 duplicate dispatches and missed 6 tasks, against 0 and 3.
- **Side effects ran.** Four expected side-effect requests started running in the handoff run, in two tasks, against one request before (measured; the mock service ran nothing real). One of the two came from a close rule that this branch has since changed. The other came from LFM3's own side-effect flag.
- **Replies stayed about as truthful.** 2 of 14 handoff replies invented something against 3 of 14 before, and no reply in either arm called a held task done (measured, one reader's grade).
- **Calls fell by one per utterance,** from 3.36 to 2.36, which is the final call (measured). Four clips made no decider call at all.
- **Every utterance has a tail that only LFM3 sees.** The words of the last request reach the transcript about when the audio ends, so no decision point during speech reads them. A median of 5 words per clip, and 71 of 200 words overall, were never read by the decider (measured).

My recommendation is not to make the handoff plan A yet. With LFM3-Audio-2B as the tail's owner, the handoff removed a 0.11 s call and added a 0.42 s tool turn and a weaker task record. Before Mason decides, three changes are worth one run each. They are listed under [Next experiments](#next-experiments).

## How the handoff works

### During speech

Nothing changes while the person speaks. The hybrid from PR #6 calls the decider at its usual decision points on partial transcripts, and it applies their answers as before. Each task now also keeps its last decision and the words that decision was made on (`Orchestrator.decided` in `voice/segmentation/tasks.py`).

### At the end of the utterance, in code

`HybridSegmenter.close` takes the final transcript instead of `update`, and it makes no call. It rebuilds the pieces from the committed segments and the soft cuts that the last decision point chose. Then `Orchestrator.close` settles each task.

| A task's words since its last decision | What the close does |
| --- | --- |
| Unchanged | Settles the task with its last decision, as on a final transcript. A side effect dispatches, and the task service holds it for confirmation, as before. A `wait` becomes `needs_clarification`. |
| Changed | Holds the task (`held`, shown as `held_for_confirmation`). No decider read the new words, so code cannot tell whether they made the request a side effect. |
| No task (the words were never read) | Nothing. The words go to LFM3 as `unreviewed_speech`. |

The measured run used an earlier version of the second row. That version held a changed side effect but dispatched a changed read-only request with its final words. On clip 03 the decider had last read "and if any are under three hundred" and called it read-only. "book the cheapest one" then arrived in the tail, and the close dispatched a booking. This branch now holds every changed task. [After the measured run](#after-the-measured-run) gives that rule's effect on the recorded runs.

### At the end of the utterance, in LFM3

A third, text-only LFM3 session on the same `llama-server` (`-np 3`) gets the snapshot as prefixed user text. The snapshot lists every task with its `task_id` and carries `unreviewed_speech`. That session may call three tools:

| Tool | What the task service does with the call |
| --- | --- |
| `delegate(instruction, side_effect)` | Reconciles it with the live tasks by the rule below. A new read-only task runs. A new side-effect task, or one with no flag, is held. |
| `cancel_task(task_id)` | Withdraws an undispatched task or cancels a dispatched one. It refuses an unknown id, a task that is already superseded, withdrawn, or cancelled, and a task whose run has completed. |
| `amend_task(task_id, new_request)` | Supersedes or cancels the task and makes a new one that revises it, with the instruction "old (corrected: new)". A completed task keeps its result. It refuses the same cases as `cancel_task`, except completion. |

A tool call is a request. `MockTaskService.call_tool` checks it and returns what happened. The voice session then hears the updated snapshot as prefixed user text, which is injection method (c) from PR #5, and it replies. The voice session never makes a tool call of its own.

The dedup rule is `Orchestrator.delegate_from_voice`. A delegate duplicates a live task when all three of these hold:

1. The two share at least 60% of the shorter one's content words.
2. The delegate brings no content word from `unreviewed_speech` that the task lacks.
3. The delegate does not claim a side effect the task lacks.

A duplicate links to the task and changes nothing else. A read-only claim about a side-effect task therefore links and leaves the hold in place, and "delete the report" never links to "find the report".

## Tuning on the synthetic clips

The four synthetic clips were the tuning set, as in PR #5. Mason's ten clips ran only after the design froze at commit `e9fd50c19`. Each pass below ran once on the synthetic clips. After a request's first live call, its decider answer came from the cache.

| Pass | Design | Clips where the tool calls handled the tail | Replies with a false done claim | First reply audio (s): two_request, correction, single, long |
| --- | --- | --- | --- | --- |
| Smoke | Tools offered in the voice session, one response with text and audio | 1 of 2 | 1 of 2 | 0.27, 0.25 |
| 1 | Text-only tool turn in the voice session, then the spoken reply | 3 of 4 | 2 of 4 | 0.41, 0.51, 0.46, 1.14 |
| 2 | Pass 1, plus the snapshot as user text after the tools and the asymmetric side-effect rule | 4 of 4 | 2 of 4 | 0.54, 0.59, 0.45, 1.21 |
| 3 | Pass 2, plus a switch to the plain method (c) persona before the reply | 4 of 4 | 2 of 4 | 0.45, 0.58, 0.49, 1.11 |
| 4, frozen | A separate text-only tools session; the voice session sees only snapshots | 3 of 4 | 0 of 4 | 0.68, 0.58, 0.69, 0.88 |
| 5 | Pass 4, plus a warm-up turn and a line saying a question is a request | 0 of 4 | 0 of 4 | 0.49, 0.63, 0.59, 0.90 |
| 6 | Pass 4, plus the question line only | 3 of 4 | 1 of 4 | 0.66, 0.73, 0.62, 0.96 |

All results are measured, one run per pass. The grades are mine.

- **Tools in the voice session made replies untruthful.** In passes 1 to 3, the `long` reply said every task was done while four were pending or held, and the `correction` reply said a running timer "has been set" (measured). That is method (b)'s failure from PR #5: the model's own `delegate` call reads to it like the action. Neither the user-text snapshot nor the persona switch fixed it. llama.cpp offers no `conversation.item.delete` to take the calls back out. Its server library names only `conversation.item.create`, `added`, `done`, and `input_audio_transcription` (measured).
- **A separate session kept the replies truthful but cost time.** The frozen design made no false done claim on the tuning set. Against pass 3, it was 0.23 s slower on `two_request`, 0.20 s slower on `single`, and the same on `correction`. On `long` it was 0.23 s faster, because it made one call instead of four (measured). I infer that the extra cost is the first prefill of the tools session's instructions and tools, because the voice session has already processed its own prompt by then. The warm-up in pass 5 did not help, and the model took its "call no tool" text as a standing order.
- **The prompt changes traded one miss for another.** Pass 6 delegated `single` and missed `correction`. Pass 4 did the opposite (measured). Four clips cannot tell these prompts apart, so I froze pass 4, the one with no false done claim. That follows PR #5's rule that a wrong status is the failure the plan rules out.

## Results on the 14 clips

### Timings

Each cell gives the median, the p95, and the max, in seconds after the audio ends, over 14 clips. With 14 samples, the nearest-rank p95 is the max.

| Measurement | Before: decider call at the end | After: handoff |
| --- | --- | --- |
| Final transcript | 0.10 / 0.21 / 0.21 | 0.11 / 0.15 / 0.15 |
| Snapshot ready | 0.24 / 0.31 / 0.31 | 0.11 / 0.15 / 0.15 |
| First reply audio | 0.45 / 0.52 / 0.52 | 0.60 / 0.92 / 0.92 |

All numbers are measured. PR #6 reported a 0.41 s median first reply over Mason's 10 clips. This round's 0.45 s covers 14 clips at `-c 32768` with every decider call live.

What limits each arm (measured from the run timelines):

- **Before.** The final decider call took 0.11 s of wall time (median, 0.10 to 0.24). Over 47 calls, the call itself took a median of 96 ms. The voice then started speaking 0.19 s after the snapshot (median, 0.09 to 0.25).
- **After.** The snapshot was ready 0.01 s after the final transcript, because the close makes no call. The tool turn then took 0.42 s (median, max 0.66) before the voice session got its snapshot. The voice started 0.08 s after that (median, max 0.14). I infer that the voice is faster to start here because it prefills the committed audio while the tool session runs. If so, the tool turn hides part of the reply's own cost.
- **`long` was the slowest handoff clip.** It took 0.92 s, because the tool turn wrote a delegate that restated the whole 48-word utterance (measured).

So the handoff did take the decider off the reply's path. LFM3's tool turn costs more than the call it replaced.

### Decider lag behind speech

The lag runs from a word's first partial to the end of the first decision point that read it. These are seconds over words, as median, p95, and max.

| Decision points counted | Before | After |
| --- | --- | --- |
| Every decision point | 1.01 / 2.94 / 3.94 (200 words) | 1.08 / 3.01 / 3.94 (129 words) |
| Decision points on partials only | 1.07 / 3.01 / 3.94 (125 words) | 1.08 / 3.01 / 3.94 (129 words) |

The call policy sets this lag, not the decider's speed. A call takes about 0.1 s (measured). The hybrid calls when a marker appears or moves after the cursor, or for a second look. So a word waits until the next such event, often the next "and" or sentence end (inferred from the policy in `HybridSegmenter._needs_call`). A clip with no marker, such as 01, gets no decision point during speech at all.

### Uncovered tail words

71 of the 200 words were never read by a decision point on a partial (measured). Per clip, the median was 5 and the range was 2 to 9. The tail is never empty. In 25 of this round's 28 clip runs, the partial that held the last word arrived 0.02 to 0.18 s after the audio ended (measured). No decision on that partial could finish before the final transcript. Four clips, 01, 07, 10, and `single`, were all tail, because their partials held no marker to trigger a call.

### Task outcomes

The scorer is `voice.segmentation.score` against `expected.json`, over every task from every source. A held side effect scores as a final dispatch, because it ends as `held_for_confirmation` either way. A held read-only task scores as undispatched.

| Measurement | Before | After |
| --- | --- | --- |
| Clips passed | 10 of 14 | 4 of 14 |
| Expected tasks correct | 23 of 26 | 16 of 26 |
| Premature dispatches | 0 | 0 |
| Duplicate dispatches | 0 | 2 |
| Missed tasks and corrections | 3 | 6 |
| Unexpected tasks | 1 | 3 |
| Expected side-effect requests that ran | 1 | 4 |

All numbers are measured. Both arms failed 07 and 10, the implicit requests, as in PR #6. Both also failed `long`, where "I need help planning tomorrow" became a clarification, and 09. The handoff also failed six clips that the before arm passed: 03, 04, 05, 08, `correction`, and `single`. These are the reasons, with 09's new failures (measured, from the task records):

- **The close's `wait` rule (03, 09).** The last decision on a partial said `wait` for words that never changed. The close turned that into `needs_clarification`, as a `wait` on a final transcript does. The before arm's final call said `dispatch_now` for both.
- **LFM3's tool calls (`single`, 08, 09, 03, 05).**
  - It made no call on `single`, so the request was lost.
  - On 08 it cancelled `task_1`, the calendar search, when the speaker had taken back the text to Sam. The decider had already withdrawn the text.
  - On 09 it delegated the whole run-on as one read-only task, so the message and both device requests ran.
  - On 03 and 05 it restated the decider's requests together with the tail as new side-effect tasks. Those became held duplicates.
- **Hidden withdrawn tasks (04, `correction`).** The decider withdrew the first reminder or timer on a partial. The replacement words came in the tail. A withdrawn task is not in the snapshot, so LFM3 could not amend it. It delegated the corrected request as a new task. The user-visible outcome is right, but the task does not record what it revises.

### Tail requests

Nine expected requests had no decider task after the close (measured). LFM3 delegated 6 of them, and 4 of those carried the expected side-effect flag. It missed 3: 07, 10, and `single`. The decider had also missed 07 and 10 in the before arm. LFM3 made 10 tool calls. Their instructions also restated 10 expected requests that the decider already had a task for. Dedup linked one call, on `long`. The other calls carried tail words, so the rule made them new tasks.

### Replies

One reader graded every spoken reply with PR #5's rubric. A reply invents something when it makes a done claim the snapshot does not support, promises a step the snapshot does not show, or gives a result or reason the snapshot does not hold. Leaving a status out is not invention.

| Measurement | Before | After |
| --- | --- | --- |
| Replies that invent something | 3 of 14 | 2 of 14 |
| Replies that state every task's status | 8 of 14 | 10 of 14 |
| Replies that call a held task done | 0 of 14 | 0 of 14 |

All grades are measured. 06 and 10 invented in both arms. 06 said a task was complete when there was no task, and 10 promised to check the forecast. The before arm's third invention was 09, which said a running task would start after confirmation. The grades and the reader's notes stay under `$VOICE_HOME/evidence/eou/grades.json`, because the notes quote the replies. Most replies in both arms still narrate in the third person ("The user wants..."), as PR #5 found.

### Calls per utterance

| Measurement | Before | After |
| --- | --- | --- |
| Decider calls per utterance, mean | 3.36 | 2.36 |
| Decider calls, total | 47 | 33 |
| LFM3 tool calls, total | none | 10 |

All numbers are measured. Every clip lost exactly its final call. In the before arm, every final decision point made one call, with no drain call.

## After the measured run

Reading the failures above led to one code change on this branch. The rule now holds a task whose words changed after its last decision, whatever its kind. A request whose new words no decider read is unclassified, and the prototype already holds unclassified requests (`needs_hold` in PR #5). I made the change for safety, after reading the test-set results, so it has no clean measurement. `python -m voice.prototype.eou_report` replays the recorded handoff run under the new rule, in code only, with LFM3's calls unchanged.

| Close rule, on the recorded handoff run | Clips passed | Expected tasks correct | Expected side-effect requests that ran |
| --- | --- | --- | --- |
| Measured: a changed read-only request dispatches | 4 | 16 | 4 |
| Shipped: every changed task is held | 4 | 15 | 3 |
| Shipped, and a `wait` on unchanged words dispatches | 4 | 17 | 3 |

The shipped rule stops 03's booking, and it also holds 05's read-only drive time for an OK. The three side-effect requests that still ran all come from LFM3's one read-only delegate on 09. The `wait` rule in the last row is an open decision, because it changes the score on the test set it came from.

## Next experiments

- **Speak first, then act.** The voice could reply from the close's snapshot right away while the tools session runs. Its tail actions would then arrive as a `task_update` at the next boundary. That takes the 0.42 s tool turn off the first reply (inferred). The cost is a second utterance whenever the tail held a request.
- **Show tasks that a correction withdrew.** Listing them in the snapshot as `cancelled`, and letting `amend_task` revive one, would let LFM3 link a last-moment correction to the task it fixes. This would address 04 and `correction` (inferred).
- **Ask the decider about the changed pieces only.** A call with only the held and tail pieces would bring the decider back after the speech ends, but for fewer questions than the full final call (guess). It costs a call, which is what the handoff removed.

## Limits

- Each arm ran once per clip. The two arms sent their partial decision points live and separately, so their answers could differ. On 13 clips, the two arms made their partial decision points at the same revisions, with the same categorical answers (measured). On 05, coalescing kept a later revision in the handoff arm, and the decider classified the second piece as a request instead of chitchat.
- Four tuning clips could not separate the tools prompts (passes 4 and 6). The frozen prompt's misses on Mason's clips may differ on a rerun.
- The side-effect flags in `expected.json` come from `dispatch_only_on_final`, which was written for the dispatch policy. It is a proxy for "this request is a side effect".
- One reader graded the replies, as in PR #5.
- The tools session reads text only. It sees the transcript, not the audio that the voice session heard.
- The handoff arm used three server slots (11,008 tokens of context each) against two (16,384 each) before (measured, from the server log). VRAM was not measured this round.

## Open decisions for Mason

- Whether the handoff stays the default. On this evidence it costs 0.15 s at the median and 7 of 26 tasks. The decider-at-the-end turn is one flag away (`--eou decider`).
- What a `wait` from a partial means at the close: `needs_clarification`, as measured, or dispatch, as in the counterfactual.
- Whether to try "speak first, then act" before anything else, since the tool turn is the reply's new limit.

## Reproduce

Run from the repository root. The offline tests need no GPU, no key, and no network:

```
python -m unittest discover -s voice/segmentation/tests -t .
python -m unittest discover -s voice/prototype/tests -t .
```

`voice/segmentation/tests/test_handoff.py` covers the close with no call, the release of an unchanged side effect, the hold on changed words, and the tail. It also covers dedup links, a new task from tail words, the side-effect asymmetry, cancel, and amend. `voice/prototype/tests/test_tasks.py` covers `call_tool` and the snapshot's `held_for_confirmation` and `unreviewed_speech`.

The two arms ran inside the GPU lease with `quiet_run.py --holder voice-eou`, with `VOICE_ENV_FILE` set to the env file that holds `OPENROUTER_API_KEY`:

```
python -m voice.prototype.run CLIPS... --eou decider --fresh --out "$VOICE_HOME/evidence/eou/before"
python -m voice.prototype.run CLIPS... --fresh --out "$VOICE_HOME/evidence/eou/after"
python -m voice.prototype.eou_report "$VOICE_HOME/evidence/eou/before" "$VOICE_HOME/evidence/eou/after" --grades "$VOICE_HOME/evidence/eou/grades.json"
```

CLIPS is `$VOICE_HOME/audio/real/01_single.wav` through `10_noisy.wav`, then `single`, `two_request`, `correction`, and `long` from `$VOICE_HOME/evidence/stage1/audio/`. `--fresh` resends every decider request even when it is cached, so the timings include each call, and it never overwrites a cached entry. `--lfm-ctx` defaults to 32768.

## Calls and cost

This round made 97 decider attempts against its cap of 150, all in the prototype's ledger under `$VOICE_HOME`. The smoke and tuning runs made 17, the before arm 47, and the handoff arm 33. Every attempt returned HTTP 200, and none drew a 429 (measured). The 22 responses this round added to the cache all report `usage.cost` 0. The other 75 were `--fresh` resends of cached requests, which leave the cache unchanged. The client checks every response for a nonzero cost and stops on one, and it never stopped (measured, no `BILLING_STOP` file). The ledger is not committed, because its cache keys hold Mason's transcripts.

## Files outside the repo

| Path | Contents | Size |
| --- | --- | --- |
| `$VOICE_HOME/evidence/eou/` | Both arms' run records, reply WAVs and server logs, the six tuning passes and the smoke run, the grades, and the report output | 53 MB |
| `$VOICE_HOME/evidence/prototype/decider-cache/` | 97 new ledger lines and 22 new cached responses | part of the existing cache |

No model, package, or venv was added.
