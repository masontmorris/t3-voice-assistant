# End of the utterance: the task gate and LFM3's authority

**Live calls differ from this design.** Mason decided on 2026-11-17 (about 02:55 ET, when he chose option 3) that LFM3 takes no actions in v1: it is the voice and conversation layer only. An LFM3 tool turn at the end of an utterance would be an action. So live calls (`voice/live/server.py`, `final_decider=True`) end each utterance with the decider's own final call, and LFM3 opens no tool turn. `gated` remains in the prototype for offline evaluation.

Mason set the design on 2026-11-16. While the person speaks, the decider segments and classifies, and a task gate decides whether each task may run. When the person stops, there is no decider call, and LFM3 has authority: "lfm has authority when an utterance is done". This doc measures that design, `gated`, against the two earlier end-of-utterance modes on the same audio. It also explains where the first handoff run lost its tasks. Each claim says whether it is measured, inferred, or a guess. The measured numbers come from one run per mode over 14 clips: Mason's 10 recordings and Stage 1's 4 synthetic clips. Every run used the GPU lease and `quiet_run.py`, LFM3 at `-c 32768`, real weather and time lookups from #13, and a live call for every decider request.

## Result

`gated` is now the prototype's default, as Mason decided. On these clips it lost to `decider`, the old final call, on tasks and on latency.

| Measurement | `gated` | `decider` | `handoff` |
| --- | --- | --- | --- |
| First reply audio, median / p95 / max (s) | 0.62 / 0.86 / 0.86 | 0.41 / 0.59 / 0.59 | 0.62 / 0.84 / 0.84 |
| Expected tasks correct, of 26 | 18 | 23 | 15 |
| Clips passed, of 14 | 7 | 10 | 4 |
| Premature or duplicate dispatches | 0 | 0 | 2 |
| Expected side-effect requests that ran | 8 | 1 | 3 |
| Replies that invent something, of 14 | 1 | 4 | 3 |
| Replies that call a held task done | 0 | 0 | 0 |
| Tasks the gate ran during speech | 5 | none | none |
| Of those, wrong | 0 | none | none |
| Tasks LFM3 decided at the end | 14 | none | 10 |
| Decider calls per utterance, mean | 2.36 | 3.36 | 2.36 |

All numbers are measured. `python -m voice.research.prototype.eou_report` prints this table and the per-clip table from the run records.

- **The gate did its part.** It ran 5 tasks during speech, and all 5 were right (measured). It held none, because every task that reached it scored above 0.40.
- **The losses all came from LFM3's turn at the end.** `gated` lost 8 expected tasks that `decider` got right, and gained 3 (measured, per clip below). Two of the gains are 07 and 10, where LFM3 delegated the implicit rain question that the decider never treated as a request.
- **`gated` was 0.21 s slower at the median.** LFM3's tool turn took 0.40 s (median, measured). It replaced the decider's final call, about 0.11 s in the `decider` arm (snapshot ready minus final transcript, medians, measured).
- **More side effects ran, by design.** In `gated`, the task service holds nothing, because the gate and LFM3 decide what runs and LFM3's calls are final. Eight expected side-effect requests ran, all on the mock worker, so nothing real was sent or bought. No side effect reached the gate during speech, so LFM3 started all eight.
- **Replies were the most truthful of the three modes,** with 1 invention in 14 against 4 and 3 (measured, one reader's grade).

## How the gated turn works

### The task gate, during speech

The gate is `voice/segmentation/gate.py`. It runs when the orchestrator is about to dispatch a task, at a decision point while the person speaks. It asks the decider nothing new. Every decision point already answers, for each segment:

- what kind it is, and whether it is ready;
- whether it has a side effect, and what it depends on or corrects;
- for a piece whose end is in doubt, whether it is finished and where it ends.

Each answer has a confidence. For a choice, that is the margin between the top two options. For a yes-no answer, it is the distance from 0.5, doubled. The gate's score is the lowest of these. A task runs when the score reaches 0.40, or 0.80 for a side effect. A task below the bar is held, and the gate reads it again at the next decision point. The snapshot shows every task the gate ran.

The hybrid's stability rule still applies before the gate. A side-effect piece commits only after it holds at two decision points and at a pause. In these clips the only pauses came with the final transcript (measured in PR #6), so no side effect reached the gate during speech.

### When speech ends

There is no decider call, and no step settles tasks. `HybridSegmenter.close(..., settle=False)` only records that the utterance ended and works out the words no decision point read. A text-only LFM3 session then gets a snapshot that holds:

- every task with its `task_id` and status;
- a `note` on each task that has not run, saying why: the gate's weakest answer and its score, or that it was not ready yet;
- `unreviewed_speech`, the words no decision point read.

LFM3 may call `run_task`, `delegate`, `cancel_task`, or `amend_task`. Its calls are final. The task service checks only that each call names a task that exists, with valid arguments. A delegate that links to a task waiting to run starts it. The voice session then hears the updated snapshot as prefixed user text and replies.

A side effect never reaches a real lookup. In `gated`, it runs on the mock worker, which sends nothing. That replaces the uniform side-effect hold in this mode. My reading is that the gate and LFM3's final authority are now what decide whether a side effect runs. That is an inference from the brief, so it is listed for Mason below.

## Picking the gate on the tuning data

`python -m voice.research.segmentation.gate_tune` replays the hybrid's decision points on partials from cached answers. It uses the original 11 scenarios and the 4 synthetic clips, and it stops before the final transcript. Every task dispatched during speech is a candidate, labelled right or wrong from the scenario's expectations. A dispatch is wrong when it matches no expected task, the task ends corrected, it must never dispatch, it lacks a required word, or it repeats.

| Gate form | Candidates | Wrong ones | Score of the wrong one | Lowest right score | Right ones that run at 0.40 |
| --- | --- | --- | --- | --- | --- |
| Lowest of all answers | 10 | 1 | 0.35 | 0.41 | 9 of 9 |
| Lowest of the ready and side-effect answers | 10 | 1 | 0.35 | 0.43 | 9 of 9 |

All results are measured. The wrong candidate is `05_correction_after_dispatch`, whose first request runs and is then corrected. Both forms separate the candidates the same way, so I kept every answer, which also watches the break and correction answers. I froze the threshold at 0.40 in commit `526d631d4`, before any run on Mason's clips.

Three limits apply to that choice:

- One wrong case at 0.35 against a right one at 0.41 is a thin margin.
- No side effect was dispatched during speech in the tuning data, so the 0.80 side-effect bar is a judgment, not a fit.
- A single "OK to run" question was not tested. It would change every decision request, and the three live arms needed 113 of this round's 150 calls.

The COMMON.md sample of real outputs is `$VOICE_HOME/evidence/eou/gate-and-reply-sample.md`. It holds all 15 gate decisions, with score, weakest answer and label, and all 28 graded replies of `gated` and `decider`, each with its WAV path. It stays local, because it quotes Mason's clips. The one known bad dispatch in the tuning data, 05's corrected first request, is the one the gate holds. The known bad outcomes on Mason's clips were not gate decisions: 03's booking came from the old close rule, and 09's device run from LFM3. So the gate cannot be checked against them.

## What `gated` lost, and why

These are the expected tasks that `decider` got right and `gated` did not (measured, from the task records):

| Clip and task | What happened in `gated` | Step that caused it |
| --- | --- | --- |
| 03 T2, the booking | The decider's task stayed proposed. LFM3 ran the flight search with `run_task`, but delegated the booking as a new task, which lost its dependency on the search. | LFM3's task decision |
| 04 T2, the corrected reminder | The decider withdrew the first reminder on a partial. The snapshot leaves out a task a correction withdrew, so LFM3 delegated the new reminder instead of amending the old one. The new task records no `revises`. | The snapshot |
| `correction` T2, the corrected timer | The same as 04. | The snapshot |
| 05 T1, the milk | The side-effect piece never committed during speech, so its task stayed proposed. LFM3 did not run it. It delegated one combined task for both requests instead. | LFM3's task decision |
| 08 T1, the calendar search | The gate ran the search during speech. LFM3 then cancelled it, when the speaker had taken back the text to Sam. The decider had already withdrawn the text. | LFM3's task decision |
| 09 T1, the message and the lights | The side-effect task stayed proposed. LFM3 delegated the whole run-on as one read-only task, which ran. | LFM3's task decision |
| 09 T3, starting the RoboRock | That one combined task stood in for the lights, so no task was left to match the RoboRock. | LFM3's task decision |
| `long` T4, sending the slides | The task waited on its dependency. LFM3 delegated the whole 48-word utterance as one task instead of running it. | LFM3's task decision |

`gated` gained 09 T2, which `decider` merged into T1, and 07 T1 and 10 T1, the implicit rain question, which LFM3 delegated. The weather lookup then failed, because no place was given and `VOICE_DEFAULT_PLACE` was not set (measured), so the user heard that the task failed.

### What I would change

Each fix is an inference. None of them has run.

- **Run waiting tasks that a delegate covers.** LFM3-Audio-2B tends to re-delegate the whole utterance instead of calling `run_task` on the tasks already listed. That happened on 05, 09, and `long`. The dedup rule could link a combined delegate to every waiting task whose words it covers, and run each of them. That would address those three clips and 03.
- **Show tasks a correction withdrew.** If the snapshot listed them as `cancelled`, and `amend_task` could revive one, LFM3 could link a correction to the task it fixes. That would fix 04 and `correction`.
- **Let the gate decide side effects during speech.** The stability rule's pause requirement kept every side effect away from the gate, so LFM3 decided all eight. The gate's 0.80 bar could replace the pause, so confident side effects run in code during speech. That bar needs side-effect tuning data first.
- **Take the tool turn off the first reply.** The voice could reply from the snapshot at once, while the tools session acts, and report what it did at the next boundary. That would remove the 0.40 s tool turn from the first reply, at the cost of a second utterance when the tail held a request.
- **Set a default place** for weather lookups, or have LFM3 ask for one, so 07 and 10 get an answer.

## Where the first handoff run lost its 7 tasks

In the first round, `handoff` got 16 of 26 tasks right against `decider`'s 23 (measured, `$VOICE_HOME/evidence/eou/after`). These are the 7 tasks it lost:

| Clip and task | What happened | Step that caused it |
| --- | --- | --- |
| 03 T1, the flight search | The last decision, on a partial, said `wait`, and the words never changed. The close turned that into `needs_clarification`. | The close rule |
| 03 T2, the booking | The decider last read the booking's condition without "book the cheapest one", which came in the tail. Its dependency answer, made on partial words, named no dependency. | The uncovered tail |
| 04 T2, the corrected reminder | The snapshot left out the withdrawn first reminder, so LFM3 delegated a new one with no `revises`. | The snapshot |
| 08 T1, the calendar search | LFM3 cancelled the calendar search instead of the text to Sam. | LFM3's task decision |
| 09 T1, the message and the lights | The same `wait` on unchanged words became `needs_clarification`. | The close rule |
| `correction` T2, the corrected timer | The same as 04. | The snapshot |
| `single` T1, the Tokyo time | The whole request was in the tail, and LFM3 made no call. | LFM3's task decision, on the uncovered tail |

So the close rule cost 2 tasks, LFM3's decisions 2, the snapshot 2, and the tail 1. The tail also set up the `single` miss. The gated turn has no close rule, so its 03 T1 and 09 T1 survive. The snapshot and LFM3's decisions still cost tasks in `gated`.

## Calls and cost

This round made 113 decider attempts against its cap of 150: 33 for `gated`, 47 for `decider`, and 33 for `handoff` (measured, ledger under `$VOICE_HOME`). The tuning and the smoke run came from the cache. Every attempt returned HTTP 200, with no 429 and no billing stop (measured).

## Open decisions for Mason

- Whether a side effect that LFM3 or the gate starts should run without confirmation. The brief says LFM3's calls are final and take no confirmation step. That supersedes the uniform side-effect hold of 2026-11-15 in `gated`. In the mock, nothing real happens either way.
- Which of the fixes under [What I would change](#what-i-would-change) to try first. The first two address 7 of the 8 lost tasks.
- Whether to tune the side-effect bar on a set where side effects reach the gate during speech, which needs the pause rule relaxed.

The rest of this doc is the first round, which measured `handoff` against `decider`. Its numbers are unchanged. Its recommendation is superseded by Mason's choice of the gated design.

## Round 1: the handoff against the decider

Mason's decision of 2026-11-16 reads "we shouldn't run the decider at the end of utterances; authority should be handed off to lfm at that point." Round 1 built that decision into the prototype and measured it against the turn before it on the same audio. Its measured numbers come from one run per arm over 14 clips: Mason's 10 recordings and Stage 1's 4 synthetic clips. Both arms ran inside the GPU lease and `quiet_run.py`, with LFM3 at `-c 32768` and every decider request sent live.

### Result

In round 1 the handoff ran by default. As built, it made the assistant slower to answer and worse at tasks on these clips.

- **The first reply came later.** Audio started 0.60 s after the speech ended against 0.45 s before (medians, measured). The max was 0.92 s against 0.52 s. LFM3's tool turn took 0.42 s (median, measured), and it replaced a decider call that took 0.11 s (median wall time, measured).
- **Task outcomes got worse.** 16 of 26 expected tasks came out right against 23, and 4 of 14 clips passed against 10 (measured). No task dispatched early in either arm. The handoff made 2 duplicate dispatches and missed 6 tasks, against 0 and 3.
- **Side effects ran.** Four expected side-effect requests started running in the handoff run, in two tasks, against one request before (measured; the mock service ran nothing real). One of the two came from a close rule that this branch has since changed. The other came from LFM3's own side-effect flag.
- **Replies stayed about as truthful.** 2 of 14 handoff replies invented something against 3 of 14 before, and no reply in either arm called a held task done (measured, one reader's grade).
- **Calls fell by one per utterance,** from 3.36 to 2.36, which is the final call (measured). Four clips made no decider call at all.
- **Every utterance has a tail that only LFM3 sees.** The words of the last request reach the transcript about when the audio ends, so no decision point during speech reads them. A median of 5 words per clip, and 71 of 200 words overall, were never read by the decider (measured).

My recommendation is not to make the handoff plan A yet. With LFM3-Audio-2B as the tail's owner, the handoff removed a 0.11 s call and added a 0.42 s tool turn and a weaker task record. Before Mason decides, three changes are worth one run each. They are listed under [Next experiments](#next-experiments).

### How the handoff works

#### During speech

Nothing changes while the person speaks. The hybrid from PR #6 calls the decider at its usual decision points on partial transcripts, and it applies their answers as before. Each task now also keeps its last decision and the words that decision was made on (`Orchestrator.decided` in `voice/segmentation/tasks.py`).

#### At the end of the utterance, in code

`HybridSegmenter.close` takes the final transcript instead of `update`, and it makes no call. It rebuilds the pieces from the committed segments and the soft cuts that the last decision point chose. Then `Orchestrator.close` settles each task.

| A task's words since its last decision | What the close does |
| --- | --- |
| Unchanged | Settles the task with its last decision, as on a final transcript. A side effect dispatches, and the task service holds it for confirmation, as before. A `wait` becomes `needs_clarification`. |
| Changed | Holds the task (`held`, shown as `held_for_confirmation`). No decider read the new words, so code cannot tell whether they made the request a side effect. |
| No task (the words were never read) | Nothing. The words go to LFM3 as `unreviewed_speech`. |

The measured run used an earlier version of the second row. That version held a changed side effect but dispatched a changed read-only request with its final words. On clip 03 the decider had last read "and if any are under three hundred" and called it read-only. "book the cheapest one" then arrived in the tail, and the close dispatched a booking. This branch now holds every changed task. [After the measured run](#after-the-measured-run) gives that rule's effect on the recorded runs.

#### At the end of the utterance, in LFM3

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

### Tuning on the synthetic clips

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

### Results on the 14 clips

#### Timings

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

#### Decider lag behind speech

The lag runs from a word's first partial to the end of the first decision point that read it. These are seconds over words, as median, p95, and max.

| Decision points counted | Before | After |
| --- | --- | --- |
| Every decision point | 1.01 / 2.94 / 3.94 (200 words) | 1.08 / 3.01 / 3.94 (129 words) |
| Decision points on partials only | 1.07 / 3.01 / 3.94 (125 words) | 1.08 / 3.01 / 3.94 (129 words) |

The call policy sets this lag, not the decider's speed. A call takes about 0.1 s (measured). The hybrid calls when a marker appears or moves after the cursor, or for a second look. So a word waits until the next such event, often the next "and" or sentence end (inferred from the policy in `HybridSegmenter._needs_call`). A clip with no marker, such as 01, gets no decision point during speech at all.

#### Uncovered tail words

71 of the 200 words were never read by a decision point on a partial (measured). Per clip, the median was 5 and the range was 2 to 9. The tail is never empty. In 25 of this round's 28 clip runs, the partial that held the last word arrived 0.02 to 0.18 s after the audio ended (measured). No decision on that partial could finish before the final transcript. Four clips, 01, 07, 10, and `single`, were all tail, because their partials held no marker to trigger a call.

#### Task outcomes

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

#### Tail requests

Nine expected requests had no decider task after the close (measured). LFM3 delegated 6 of them, and 4 of those carried the expected side-effect flag. It missed 3: 07, 10, and `single`. The decider had also missed 07 and 10 in the before arm. LFM3 made 10 tool calls. Their instructions also restated 10 expected requests that the decider already had a task for. Dedup linked one call, on `long`. The other calls carried tail words, so the rule made them new tasks.

#### Replies

One reader graded every spoken reply with PR #5's rubric. A reply invents something when it makes a done claim the snapshot does not support, promises a step the snapshot does not show, or gives a result or reason the snapshot does not hold. Leaving a status out is not invention.

| Measurement | Before | After |
| --- | --- | --- |
| Replies that invent something | 3 of 14 | 2 of 14 |
| Replies that state every task's status | 8 of 14 | 10 of 14 |
| Replies that call a held task done | 0 of 14 | 0 of 14 |

All grades are measured. 06 and 10 invented in both arms. 06 said a task was complete when there was no task, and 10 promised to check the forecast. The before arm's third invention was 09, which said a running task would start after confirmation. The grades and the reader's notes stay under `$VOICE_HOME/evidence/eou/grades.json`, because the notes quote the replies. Most replies in both arms still narrate in the third person ("The user wants..."), as PR #5 found.

#### Calls per utterance

| Measurement | Before | After |
| --- | --- | --- |
| Decider calls per utterance, mean | 3.36 | 2.36 |
| Decider calls, total | 47 | 33 |
| LFM3 tool calls, total | none | 10 |

All numbers are measured. Every clip lost exactly its final call. In the before arm, every final decision point made one call, with no drain call.

### After the measured run

Reading the failures above led to one code change on this branch. The rule now holds a task whose words changed after its last decision, whatever its kind. A request whose new words no decider read is unclassified, and the prototype already holds unclassified requests (`needs_hold` in PR #5). I made the change for safety, after reading the test-set results, so it has no clean measurement. `python -m voice.research.prototype.eou_report` replays the recorded handoff run under the new rule, in code only, with LFM3's calls unchanged.

| Close rule, on the recorded handoff run | Clips passed | Expected tasks correct | Expected side-effect requests that ran |
| --- | --- | --- | --- |
| Measured: a changed read-only request dispatches | 4 | 16 | 4 |
| Shipped: every changed task is held | 4 | 15 | 3 |
| Shipped, and a `wait` on unchanged words dispatches | 4 | 17 | 3 |

The shipped rule stops 03's booking, and it also holds 05's read-only drive time for an OK. The three side-effect requests that still ran all come from LFM3's one read-only delegate on 09. The `wait` rule in the last row is an open decision, because it changes the score on the test set it came from.

### Next experiments

- **Speak first, then act.** The voice could reply from the close's snapshot right away while the tools session runs. Its tail actions would then arrive as a `task_update` at the next boundary. That takes the 0.42 s tool turn off the first reply (inferred). The cost is a second utterance whenever the tail held a request.
- **Show tasks that a correction withdrew.** Listing them in the snapshot as `cancelled`, and letting `amend_task` revive one, would let LFM3 link a last-moment correction to the task it fixes. This would address 04 and `correction` (inferred).
- **Ask the decider about the changed pieces only.** A call with only the held and tail pieces would bring the decider back after the speech ends, but for fewer questions than the full final call (guess). It costs a call, which is what the handoff removed.

### Limits

- Each arm ran once per clip. The two arms sent their partial decision points live and separately, so their answers could differ. On 13 clips, the two arms made their partial decision points at the same revisions, with the same categorical answers (measured). On 05, coalescing kept a later revision in the handoff arm, and the decider classified the second piece as a request instead of chitchat.
- Four tuning clips could not separate the tools prompts (passes 4 and 6). The frozen prompt's misses on Mason's clips may differ on a rerun.
- The side-effect flags in `expected.json` come from `dispatch_only_on_final`, which was written for the dispatch policy. It is a proxy for "this request is a side effect".
- One reader graded the replies, as in PR #5.
- The tools session reads text only. It sees the transcript, not the audio that the voice session heard.
- The handoff arm used three server slots (11,008 tokens of context each) against two (16,384 each) before (measured, from the server log). VRAM was not measured this round.

### Open decisions for Mason after round 1

- Whether the handoff stays the default. On this evidence it costs 0.15 s at the median and 7 of 26 tasks. The decider-at-the-end turn is one flag away (`--eou decider`).
- What a `wait` from a partial means at the close: `needs_clarification`, as measured, or dispatch, as in the counterfactual.
- Whether to try "speak first, then act" before anything else, since the tool turn is the reply's new limit.

### Calls and cost in round 1

This round made 97 decider attempts against its cap of 150, all in the prototype's ledger under `$VOICE_HOME`. The smoke and tuning runs made 17, the before arm 47, and the handoff arm 33. Every attempt returned HTTP 200, and none drew a 429 (measured). The 22 responses this round added to the cache all report `usage.cost` 0. The other 75 were `--fresh` resends of cached requests, which leave the cache unchanged. The client checks every response for a nonzero cost and stops on one, and it never stopped (measured, no `BILLING_STOP` file). The ledger is not committed, because its cache keys hold Mason's transcripts.

## Reproduce

Run from the repository root. The offline tests need no GPU, no key, and no network:

```
python -m unittest discover -s voice/segmentation/tests -t .
python -m unittest discover -s voice/prototype/tests -t .
python -m voice.research.segmentation.gate_tune --clips "$VOICE_HOME/evidence/eou/before"
```

`voice/segmentation/tests/test_handoff.py` covers the handoff close, dedup, cancel and amend. It also covers the gate: a confident task runs, a task below the bar waits and is read again, a side effect needs the stricter bar, the gated close settles nothing, and `run_task` starts a waiting task. `voice/prototype/tests/test_tasks.py` covers `call_tool`, the notes for tasks that have not run, and a gated side effect that runs on the mock and never reaches a lookup.

The three arms of round 2 ran inside the GPU lease with `quiet_run.py --holder voice-eou`, with `VOICE_ENV_FILE` set to the env file that holds `OPENROUTER_API_KEY`:

```
python -m voice.prototype.run CLIPS... --eou gated --fresh --out "$VOICE_HOME/evidence/eou/r2-gated"
python -m voice.prototype.run CLIPS... --eou decider --fresh --out "$VOICE_HOME/evidence/eou/r2-decider"
python -m voice.prototype.run CLIPS... --eou handoff --fresh --out "$VOICE_HOME/evidence/eou/r2-handoff"
python -m voice.research.prototype.eou_report gated="$VOICE_HOME/evidence/eou/r2-gated" decider="$VOICE_HOME/evidence/eou/r2-decider" handoff="$VOICE_HOME/evidence/eou/r2-handoff" --grades "$VOICE_HOME/evidence/eou/grades-r2.json"
```

CLIPS is `$VOICE_HOME/audio/real/01_single.wav` through `10_noisy.wav`, then `single`, `two_request`, `correction`, and `long` from `$VOICE_HOME/evidence/stage1/audio/`. Round 1's arms are `$VOICE_HOME/evidence/eou/before` (`--eou decider`) and `after` (`--eou handoff`). The gated arm ran before `Orchestrator.end` existed, so its records log LFM3's dispatches as partial ones. `eou_report` marks them as after the final transcript, which matches their place in the timeline.

## Files outside the repo

| Path | Contents | Size |
| --- | --- | --- |
| `$VOICE_HOME/evidence/eou/` | Both rounds' run records, reply WAVs, server logs, the tuning passes, the grades, the gate sample, and the report output | 93 MB |
| `$VOICE_HOME/evidence/prototype/decider-cache/` | The ledger lines and new cached responses of both rounds | part of the existing cache |

No model, package, or venv was added.
