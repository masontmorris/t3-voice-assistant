# Prompt pre-writing: drafts that are ready at dispatch

Measured on 2026-11-16 from committed decider responses and from the live timeline of the hybrid's prototype run on Mason's ten clips. No author model has run yet. Each claim says whether it is measured, inferred, or a guess.

`voice/delegation/` drafts the prompt that each task's frontier agent will receive while the user is still speaking. The hybrid segmenter's per-segment completion answer starts the draft. The design works offline against hand-written fixtures, and 20 tests pass (measured). No Flint call has run. Mason moved the author from OpenRouter to his opencode Go subscription, and the route is not settled, so draft latency and draft quality are still unmeasured.

The replay already gives the time budget that a draft has to fit in. On Mason's clips, 4 of the 9 dispatched tasks completed only at the decision point that dispatched them, so no author can get ahead of those 4. The other 5 completed 0.67 s to 2.49 s before dispatch (measured). An author that answers in under 0.67 s covers all 5. An author that takes 1 s covers 1 of the 5 (measured with a stand-in author at fixed latencies).

## Recommendation

- Measure Flint's latency on the opencode route before you tune anything else. On the clips, the gap from a task's first completion to its dispatch has a median of 0.67 s (n=9, measured). Unless Flint's median is under about 0.7 s, most drafts land after their task has gone out (inferred from the latency sweep below).
- Dispatch with the raw span when the draft is not ready, and do not wait. Holding a task for its draft adds the author's latency to every task that completes at its dispatch point, and that is 4 of 9 tasks on the clips and 22 of 44 on the scenarios (measured).
- Keep drafting tied to the completion answer. It started 0.83 to 1.64 drafts per utterance on the scenarios and 0.7 on the clips (measured). That leaves the 150-call budget room for every replay plus repeats.

## How drafting works

### The data shape

`voice/delegation/draft.py` owns the shape.

- `DraftInput` is everything the author sees for one task: the task id, the instruction, the dependency ids, the side-effect flag, and the utterance so far as context. Two inputs are equal when the instruction's lowercased words, the dependencies, and the side-effect flag are equal. Casing and punctuation do not count, because the final transcript re-cases and re-punctuates the partials ("email dana that" becomes "Email Dana that"). An earlier version compared the raw text. It refused 11 of the 12 held-out drafts that were ready before dispatch, for that reason alone (measured).
- `Draft` is the author's structured answer: `instruction`, `context`, `constraints`, `success_criteria`, and `open_questions`. `DRAFT_SCHEMA` is the JSON schema that goes in `response_format`.
- `PromptDraft` is the newest draft for one task. It holds the input it came from and a status: `drafting`, `current`, `stale`, or `rejected`.
- `Prewriter` keeps the drafts for one utterance. It allows at most one draft request in flight per task. If the task's input changes while a request is in flight, the newest input waits and goes out when the first reply lands.

### When a draft starts

`completed` reads one applied hybrid decision point. A segment whose end was in doubt carries the hybrid's `finished` answer, and the segment is complete when that answer reaches 0.5, the hybrid's own threshold. A segment without a `finished` question had a certain end, either a hard cut or the end of the final transcript, so it is complete. A malformed answer, a failed call, or an unapplied drain point completes nothing.

A task's first completion starts its draft. After that, `Prewriter.observe` redrafts only when the task's input changes. The task record changes only at decision points, so the hybrid's call policy also limits how often drafts go out.

### What the draft cannot change

The draft is untrusted model output. Deterministic code reads it at two points.

1. `parse_draft` requires every field, and at least one success criterion. Anything else makes the draft `rejected`.
2. `check` rejects a draft that names an outside action the span does not name. The actions come in families: message, purchase, booking, removal, publish, and call. A draft may say "send Dana a message" for "email Dana", but a draft for "look up the weather" that says "email the forecast" is rejected. `check` also rejects a `{{task_N.result}}` placeholder for a task that the record does not depend on.

`render` builds the prompt the agent receives. The dependency placeholders and the side-effect confirmation come from the task record, never from the draft. A draft that says "no need to confirm" still gets the confirmation line on a side-effect task. So the draft cannot change the side-effect flag, the dependencies, or when the task dispatches.

### At dispatch

`Prewriter.dispatch` sends the rendered draft when the draft is `current` and its input equals the task record. Otherwise it sends the raw instruction, with the same dependency and confirmation lines, and gives the reason: `no draft started`, `still drafting`, `stale`, or `rejected`. A side-effect task gets its draft while the hybrid holds it for the final transcript. The test `test_a_held_side_effect_task_gets_its_draft_before_the_final_and_dispatches_with_it` shows the email task dispatching with its draft on the final transcript.

### The author

`voice/delegation/author.py` keeps the provider behind a `Transport`. A transport has a `model`, an `assert_free` method, and a `send` method that returns an `AuthorReply`. `author_request` builds a provider-neutral request from a `DraftInput`. `CachedAuthor` wraps any transport with the rules the brief sets.

- A response cache that never re-requests. Failed replies are not cached.
- An attempt budget, 150 by default.
- One request in flight, and the decider's adaptive pacer on 429.
- A stop when more than 20% of attempts draw a 429, after at least 5 attempts.
- A stop on any nonzero cost.
- `assert_free` runs before every attempt. The transport owns that check, because the free id's form depends on the provider.

No transport ships in this change. The opencode adapter is one class with `model`, `assert_free`, and `send`, and nothing else in the module changes when it lands (inferred from the interface).

## What the replay measured

`python -m voice.delegation.replay` drives a fresh hybrid from the decider cache, with no live call, and attaches a `Prewriter`. The author is a stand-in that returns a valid draft of the instruction after a fixed latency. So these numbers measure timing and staleness, not draft quality.

### Mason's ten clips

The clip replay uses the as-run updates from the hybrid's prototype run (`$VOICE_HOME/evidence/hybrid/run-h-c`). It reproduced that run's task records and dispatch logs on all 10 clips (measured). Each decision point happens at the second it finished in the live run.

| Clip | Task | Dispatched on | First completion to dispatch, s | First completion to final transcript, s |
| --- | --- | --- | --- | --- |
| 01_single | task_1 | final | 0.00 | 0.00 |
| 02_two_requests | task_1 | partial | 0.91 | 2.04 |
| 03_dependent | task_1 | final | 2.49 | 2.49 |
| 04_correction | task_2 | final | 0.00 | 0.00 |
| 05_topic_change | task_1 | final | 0.79 | 0.79 |
| 05_topic_change | task_2 | final | 0.00 | 0.00 |
| 08_cancel | task_1 | partial | 1.00 | 4.10 |
| 09_run_on | task_1 | final | 0.67 | 0.67 |
| 09_run_on | task_2 | final | 0.00 | 0.00 |

Clips 06, 07, and 10 dispatched no task. The 4 tasks at 0.00 are the last request of their utterance. The hybrid's completion answer for those requests first said yes on the final transcript, at the same point that dispatched them (measured).

| Stand-in latency, s | Dispatched with a draft | Raw, no draft started | Raw, still drafting | Median added wait if dispatch held for the draft, s |
| --- | --- | --- | --- | --- |
| 0.25 | 5 of 9 | 4 | 0 | 0.00 |
| 0.5 | 5 of 9 | 4 | 0 | 0.00 |
| 1 | 1 of 9 | 4 | 4 | 0.33 |
| 2 | 1 of 9 | 4 | 4 | 1.33 |
| 4 | 0 of 9 | 4 | 5 | 3.33 |

The clips started 7 drafts, for 7 tasks, with no redrafts (measured). The 2 drafted tasks that did not dispatch, in clips 02 and 03, wait on `task_1`'s result. Their drafts are ready when that result arrives (measured, both `ready` with a dependency at the end of the utterance).

These are 10 clips, one run each. The 0.67 s floor among the 5 drafted tasks is a single clip, so a different speaker or pace could move it (inferred).

### The scenario sets

Scenarios have no clock, so the replay puts decision point i at time i and uses a latency of 0.5. A draft is then ready when its task completed at an earlier decision point than the one that dispatched it.

| Set | Utterances | Dispatched | With a draft | No draft started | Stale | Drafts requested | Per utterance | Redrafts |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Original | 11 | 18 | 8 | 8 | 2 | 18 | 1.64 | 6 |
| Held-out | 18 | 26 | 12 | 14 | 0 | 15 | 0.83 | 0 |

Both stale drafts were real changes to what was asked (measured). In `01_two_independent`, the hybrid answered `finished` yes on "look up how long the drive" one revision before "from boulder takes" arrived. The record changed again only at the final point, which also dispatched it. In `10_side_effect_email`, ASR turned "check if room four b is free" into "room 4B". The first is a premature completion answer, and the second is a word change that the normalization correctly treats as new input.

`voice/delegation/results/scenarios-standin.json` holds the per-task rows.

## Where it plugs into the prototype

This change does not touch `voice/prototype/`. Two hooks would connect it, both in `voice/prototype/run.py`.

1. In `segment_worker`, after `segmenter.update` returns an applied point, call `Prewriter.observe(point, segmenter.orch.tasks, update.text)`. Send each returned input to the author on a worker thread, and call `Prewriter.land` with the reply.
2. Where `MockTaskService.sync` starts a dispatched task, take the prompt from `Prewriter.dispatch(task)` instead of the bare instruction.

## Not measured yet

These need the author route.

- Draft latency, median and p95, on the opencode route.
- Draft quality on a hand-checked sample: does the draft ask for exactly what the user asked, with nothing added and nothing dropped.
- How often real drafts fail `parse_draft` or `check`. The action families may reject a good draft that mentions an action in a negative way, such as "do not send anything" (guess). A rejected draft falls back to the raw span, so that failure costs quality, not safety.
- Readiness with the real latency, on the scenarios and the clips.

The live run needs about 40 distinct requests for one pass over the 29 scenarios and 10 clips (measured from the stand-in counts: 18 + 15 + 7). That leaves room in the 150-call budget for one repeat pass and a quality sample.

## Calls and cost

No Flint call ran, through OpenRouter or any other route (measured, no author ledger exists). The replays made no decider call either. Every decider request hit the committed cache or the prototype cache under `$VOICE_HOME` (measured, the offline client raises on a miss).
