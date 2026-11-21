# Prompt pre-writing: drafts that are ready at dispatch

Measured on 2026-11-16 from committed decider responses, from 123 live calls to DeepSeek V4.1 Flash through Command Code, and from the live timeline of the hybrid's prototype run on Mason's ten clips. Each claim says whether it is measured, inferred, or a guess.

`voice/research/delegation/` drafts the prompt that each task's frontier agent will receive while the user is still speaking. The hybrid segmenter's per-segment completion answer starts the draft.

Only `deepseek/deepseek-v4.1-flash-ultrafast` is fast enough to get ahead of dispatch. Its median draft took 0.80 s and its p95 took 1.12 s. `-fast` took 4.09 s and 8.64 s, and default reasoning took 7.57 s and 11.99 s (measured, 22 matched drafts each). On Mason's clips, 5 of the 9 dispatched tasks complete 0.67 s to 2.49 s before dispatch (measured). With each draft landing after its own measured latency, ultrafast had all 5 drafts in hand at dispatch, and the other two settings had none (measured). Draft quality was about the same across the three. Default, `-fast`, and `-ultrafast` asked for exactly the span in 10, 11, and 11 of the 15 matched scenario drafts, and in all 7 clip drafts each (hand-checked). Each faster setting produced one draft that added an outside action, and `check` rejected both.

A second race turned reasoning off. Command Code rejects `reasoning_effort: "minimal"` and accepts `off`, `low`, `medium`, `high`, `xhigh`, and `max` (measured, HTTP 400 on all three ids). With `off`, ultrafast's median draft took 0.52 s and its p95 took 0.70 s, against 0.88 s and 1.12 s on its default reasoning (measured, the same 12 drafts). Default and `-fast` with reasoning off took 1.99 s and 1.67 s medians, which is still too slow for 4 of the 5 early clip tasks (measured).

## Recommendation

- Use `deepseek/deepseek-v4.1-flash-ultrafast` with `reasoning_effort: "off"` as the author. It was the fastest setting tested, with a slowest draft of 0.96 s. Every early clip draft was in hand at dispatch, and its drafts were as faithful as the other settings' (measured, small sample: 5 scenario and 7 clip drafts). On its default reasoning, ultrafast also had every early clip draft in time, so either ultrafast setting works on these clips. Reasoning off leaves more margin under the 0.67 s tightest lead (measured, 0.47 s against 0.64 s on that clip).
- Dispatch with the raw span when no current draft exists, and do not hold for a draft. 4 of the 9 clip tasks complete only at their dispatch point, so no author helps those (measured). `Prewriter.dispatch` already sends the raw span.
- Fix `check` before it gates anything. It rejected both drafts that added an outside action. It also rejected 2 of ultrafast's 5 in-time clip drafts, and both of those were faithful (measured). The false rejections come from word-prefix matches: "in the order given" reads as a purchase, "what is scheduled" as a booking, and "(e.g., booking)" inside a confirmation constraint as an added booking. The next thing to try is to scan only the affirmed instruction and success criteria. That would have kept both catches here (inferred, not run, because it would be tuned on this same sample).
- Change the author prompt before a wider run. Tell it not to restate other requests and not to write prohibitions. Every setting folded a neighboring request's read-only work into 3 to 5 of the 15 matched scenario drafts (measured).

## How drafting works

### The data shape

`voice/research/delegation/draft.py` owns the shape.

- `DraftInput` is everything the author sees for one task: the task id, the instruction, the dependency ids, the side-effect flag, and the utterance so far as context. Two inputs are equal when the instruction's lowercased words, the dependencies, and the side-effect flag are equal. Casing and punctuation do not count, because the final transcript re-cases and re-punctuates the partials ("email dana that" becomes "Email Dana that"). An earlier version compared the raw text. It refused 11 of the 12 held-out drafts that were ready before dispatch, for that reason alone (measured).
- `Draft` is the author's structured answer: `instruction`, `context`, `constraints`, `success_criteria`, and `open_questions`. `DRAFT_SCHEMA` goes in `response_format` under the name `prompt_draft`.
- `PromptDraft` is the newest draft for one task. It holds the input it came from and a status: `drafting`, `current`, `stale`, or `rejected`.
- `Prewriter` keeps the drafts for one utterance. It allows at most one draft request in flight per task. If the task's input changes while a request is in flight, the newest input waits and goes out when the first reply lands.

### When a draft starts

`completed` reads one applied hybrid decision point. A segment whose end was in doubt carries the hybrid's `finished` answer, and the segment is complete when that answer reaches 0.5, the hybrid's own threshold. A segment without a `finished` question had a certain end, either a hard cut or the end of the final transcript, so it is complete. A malformed answer, a failed call, or an unapplied drain point completes nothing.

A task's first completion starts its draft. After that, `Prewriter.observe` redrafts only when the task's input changes. The task record changes only at decision points, so the hybrid's call policy also limits how often drafts go out.

### What the draft cannot change

The draft is untrusted model output. Deterministic code reads it at two points.

1. `parse_draft` requires every field, and at least one success criterion. It also accepts the object nested under `prompt_draft` as its only key. Command Code did not enforce the strict schema, and 7 of 31 answers came back nested that way (measured).
2. `check` rejects a draft that names an outside action the span does not name. The actions come in families: message, purchase, booking, removal, publish, and call. A draft may say "send Dana a message" for "email Dana". A sentence that forbids or sets something aside, such as "Do not book any flights", is skipped, because it names no action to take. `check` also rejects a `{{task_N.result}}` placeholder for a task that the record does not depend on.

`render` builds the prompt the agent receives. The dependency placeholders and the side-effect confirmation come from the task record, never from the draft. A draft that says "no need to confirm" still gets the confirmation line on a side-effect task. So the draft cannot change the side-effect flag, the dependencies, or when the task dispatches.

### At dispatch

`Prewriter.dispatch` sends the rendered draft when the draft is `current` and its input equals the task record. Otherwise it sends the raw instruction, with the same dependency and confirmation lines, and gives the reason: `no draft started`, `still drafting`, `stale`, or `rejected`. A side-effect task gets its draft while the hybrid holds it for the final transcript.

### The author

`voice/research/delegation/author.py` keeps the provider behind a `Transport`, which has a `model` and a `send` method that returns an `AuthorReply`. `author_request` builds a provider-neutral request from a `DraftInput`. `CachedAuthor` wraps any transport.

- A response cache that never re-requests. Failed replies are not cached.
- An attempt budget of 150.
- One request in flight, and the decider's adaptive pacer on 429.
- A stop when more than 20% of attempts draw a 429, after at least 5 attempts.
- A stop on HTTP 402, or on any error that mentions a usage limit, a quota, billing, credit, payment, or the subscription.

`voice/research/delegation/commandcode.py` is the Command Code transport. It posts to `https://api.commandcode.ai/provider/v1/chat/completions` with the model `deepseek/deepseek-v4.1-flash`. It reads the key at each send from the file that `VOICE_AUTHOR_KEY_FILE` names, and puts it only in the Authorization header. By default the body it sends has exactly five keys: `model`, `max_tokens`, `temperature`, `messages`, and `response_format` (measured, from the `sent` field of every default-reasoning cache record). It carries no reasoning parameter, so the model runs on its default reasoning. `CommandCodeTransport(model, reasoning_effort="off")` adds `reasoning_effort` as a sixth key and nothing else. The cache keys on that setting, so an answer from one reasoning setting is never reused for another. Tests pin both key sets.

`max_tokens` is 4096. The first call used 600, and default reasoning spent 539 of them before the answer, so the reply came back cut off with no draft (measured, one call).

## The model

Command Code lists 100 models. The only V4.1 Flash id at default speed is `deepseek/deepseek-v4.1-flash`, named "DeepSeek V4.1 Flash (latest)" (measured, 2026-11-16). Every reply named that id as its model and had an empty `system_fingerprint` (measured, 31 of 31). So the API does not say whether it serves the 1017 build. Hugging Face has both `deepseek-ai/DeepSeek-V4.1-Flash-1017`, created 2026-10-17, and `deepseek-ai/DeepSeek-V4.1-Flash`. Whether "latest" means 1017 is a guess until Command Code says so. Mason called the build question a non-issue, so this doc leaves it open.

Command Code also lists `deepseek/deepseek-v4.1-flash-fast`, named "DeepSeek V4.1 Flash Fast (latest)", and `deepseek/deepseek-v4.1-flash-ultrafast`, named "DeepSeek V4.1 Flash Ultrafast" (measured, model list). The listing does not say how they differ from the default. They spent fewer reasoning tokens per draft (median 494 and 155, against 782), so a lighter reasoning setting is one difference (inferred). Whether they also run on different hardware or a smaller model is unknown. The transport takes the id as `CommandCodeTransport(model)`, and the replay takes it as `--model`.

## Faster settings: a small matched test

Mason asked for very small but representative tests of `deepseek/deepseek-v4.1-flash-fast` and `deepseek/deepseek-v4.1-flash-ultrafast`. Both ran on the same requests as default reasoning, with no reasoning parameter, through the same adapter and cache.

- Scenarios: the 15 draft requests from `01_two_independent`, `05_correction_after_dispatch`, `08_delegate_after_dispatch`, `10_side_effect_email`, and `post_and_read`. They cover a plain lookup, a correction, a voice delegate, a dependency, two side-effect tasks, and 4 of the 5 requests where default reasoning folded in a neighbor's task. `voice/research/delegation/results/subset-<model>.json` holds each model's drafts.
- Clips: the 7 draft requests from Mason's clips. Each draft lands after its own measured latency, so the clip result is a direct readiness measurement, not a sweep. The clip drafts and their cache hold transcripts, so they stay under `$VOICE_HOME/evidence/prewrite/`.

| Measurement | Default | `-fast` | `-ultrafast` |
| --- | --- | --- | --- |
| Median latency, 22 drafts | 7.57 s | 4.09 s | 0.80 s |
| p95 latency (nearest rank) | 11.99 s | 8.64 s | 1.12 s |
| Range | 2.47 s to 14.79 s | 2.91 s to 8.80 s | 0.51 s to 2.04 s |
| Median reasoning tokens | 782 | 494 | 155 |
| Answers nested under the schema name | 5 of 22 | 0 | 0 |
| Clip tasks with the draft in hand at dispatch | 0 of 5 | 0 of 5 | 5 of 5 |
| Clip tasks dispatched with the draft | 0 of 9 | 0 of 9 | 3 of 9 |
| Scenario drafts that ask for exactly the span | 10 of 15 | 11 of 15 | 11 of 15 |
| Scenario drafts that fold in a neighbor's read-only task | 5 | 3 | 3 |
| Scenario drafts that add an outside action | 0 | 1 | 1 |
| Clip drafts that ask for exactly the span | 7 of 7 | 7 of 7 | 7 of 7 |
| Drafts `check` rejected, scenarios and clips | 6 of 22 | 5 of 22 | 6 of 22 |

All 66 answers in this test returned HTTP 200 with `finish_reason` `stop`, and every one parsed (measured). Each reply named the model that was asked for.

These are the two drafts that added an outside action.

- `-fast` turned "find the gallery opening date" in `post_and_read` into "Find the gallery opening date and post the message". `check` rejected it.
- `-ultrafast` turned "and then check if room four b is free" in `10_side_effect_email` into a draft that emails Dana first. `check` rejected it.

On the clips, ultrafast's 5 early drafts all landed before dispatch. The tightest was `09_run_on`, with a 0.67 s lead. `check` rejected 2 of the 5, in `08_cancel` and `09_run_on`, and both drafts were faithful (hand-checked). So 3 dispatched with their drafts.

This is one run per request, on 15 scenario and 7 clip requests. Ultrafast's slowest draft (2.04 s) was faster than the default's median, so the latency ranking is unlikely to reverse on a rerun (inferred). The quality counts differ by one draft, which this sample cannot separate (inferred).

## Reasoning off: a drag race

Mason asked for a super small drag race on minimal reasoning.

### What Command Code accepts

Each id first got one request with `"reasoning_effort": "minimal"` added to the body. All three answered HTTP 400 with `Invalid option: expected one of "off"|"low"|"medium"|"high"|"xhigh"|"max"` and `param` `reasoning_effort` (measured). So `minimal` is not an option, and `off` is the lowest value Command Code accepts.

The same request then went out with `"reasoning_effort": "off"`. The body was the five default keys plus `reasoning_effort` (measured, from the `sent` field). All three ids answered HTTP 200 with 0 reasoning tokens. On their defaults, the same request had used 860, 254, and 156 reasoning tokens for default, `-fast`, and `-ultrafast` (measured). So Command Code honors the setting on every id, and none of them ignored it.

### The race

The race used 12 draft requests per setting. 5 came from `10_side_effect_email` and `post_and_read`, the two scenarios where a faster setting had added an outside action. The other 7 came from Mason's clips, where each draft lands after its own measured latency. The baseline is ultrafast on its default reasoning, using the cached answers to the same 12 requests from the matched test above. `voice/research/delegation/results/race-off-<model>.json` holds the scenario drafts. The clip drafts stay under `$VOICE_HOME/evidence/prewrite/`.

| Measurement, 12 drafts each | Ultrafast, default | Default id, off | `-fast`, off | `-ultrafast`, off |
| --- | --- | --- | --- | --- |
| Median latency | 0.88 s | 1.99 s | 1.67 s | 0.52 s |
| p95 latency (nearest rank) | 1.12 s | 2.31 s | 1.95 s | 0.70 s |
| Range | 0.51 s to 2.04 s | 1.59 s to 2.56 s | 1.30 s to 2.09 s | 0.34 s to 0.96 s |
| Median reasoning tokens | 154 | 0 | 0 | 0 |
| Clip tasks with the draft in hand at dispatch | 5 of 5 | 1 of 5 | 1 of 5 | 5 of 5 |
| Clip tasks dispatched with the draft | 3 of 9 | 1 of 9 | 1 of 9 | 4 of 9 |
| Scenario drafts that ask for exactly the span | 3 of 5 | 4 of 5 | 2 of 5 | 3 of 5 |
| Scenario drafts that add an outside action | 1 | 0 | 2 | 1 |
| Clip drafts that ask for exactly the span | 7 of 7 | 7 of 7 | 7 of 7 | 7 of 7 |
| Drafts `check` rejected | 5 of 12 | 3 of 12 | 4 of 12 | 4 of 12 |

All 36 race answers returned HTTP 200 with `finish_reason` `stop`, and every one parsed (measured).

Turning reasoning off cut each id's latency. The default id went from 7.57 s to 1.99 s and `-fast` from 4.09 s to 1.67 s (medians). Those drafts still landed in time only for the clip task with the longest lead, `03_dependent` at 2.48 s. Ultrafast with reasoning off was ready for all 5 early clip tasks, including `09_run_on`, where the lead is 0.67 s and the draft took 0.47 s (measured).

Turning reasoning off did not make the drafts worse in this sample. Every setting folded the gallery date lookup into "and post the message gallery opens soon" (measured). The outside-action additions moved around between settings. Ultrafast added an email to the room check on its default reasoning, and with reasoning off it added the post to "find the gallery opening date" instead. `check` rejected all 4 such drafts in the table (measured). With 5 scenario drafts per setting, a difference of one or two drafts is noise (inferred).

The extra clip dispatch for ultrafast with reasoning off came from `08_cancel`. On its default reasoning, ultrafast had written "report what is scheduled", which `check` misread as a booking. With reasoning off it wrote "Check the user's calendar for Friday" and passed (measured). Both drafts ask for the same thing, so this is the check's false rejection, not a quality gap (hand-checked).

## Live drafts on the scenarios

`python -m voice.research.delegation.replay scenarios --author commandcode` drives the hybrid from the decider cache and sends each draft request to Command Code. Only synthetic scenario text went to Command Code. `voice/research/delegation/results/scenarios-commandcode.json` holds every landed draft with its request status, latency, token usage, and response metadata. `voice/research/delegation/cache/` holds the raw records.

### Latency

| Measurement | Value |
| --- | --- |
| Answers | 31, all HTTP 200, all `finish_reason` `stop` |
| Median latency | 7.0 s |
| p95 latency (nearest rank) | 12.0 s |
| Range | 2.4 s to 14.8 s |
| Reasoning tokens per answer | median 769, maximum 1,991 |

The replay ran one request at a time. Latency was not the replay's clock, so these are request times, not readiness.

### Quality, hand-checked

I read all 31 drafts against their spans.

- 26 ask for exactly what the span asks. Several resolve the span well: "look up the height of mount elbert (corrected: actually mount massive)" became "Look up the height of Mount Massive", and "and then summarize it" with a dependency became a summary of `{{task_1.result}}`.
- 5 fold a neighboring request into the task. In `08_delegate_after_dispatch`, "find the q3 budget spreadsheet" became "Find the Q3 budget spreadsheet and tell the user", and two drafts for "and tell me" also re-found the spreadsheet. In `05_correction_after_dispatch`, "and check hotel" became "Find flights to Austin on Friday and check hotel". In `post_and_read`, "and post the message gallery opens soon" also found the gallery's opening date. Each one adds read-only work that another task already does.
- None adds an outside action. The two side-effect drafts that matter most, the email to Dana and the deletion of "april", stay on their spans and ask for confirmation.
- The drafts lean on `open_questions`. 29 of 31 ask 2 to 7 questions (measured), such as "Which Friday (specific date)?" The agent will see these as questions to ask, which may slow it on tasks a person would just do (guess).

### What the checks did

`parse_draft` read all 31 answers (measured). `check` rejected 11 before the negation fix: 3 of the 5 scope additions and 8 of the 26 faithful drafts (measured). After the fix it rejects 8: 2 scope additions and 6 faithful drafts (measured). I chose the fix from these same drafts, so its numbers are not a held-out result. The remaining false rejections come from drafts that quote another request in `context` ("The email request is not part of this task") or ask about it in `open_questions` (measured).

### Readiness on the scenario clock

With live answers and decision point i at time i, a draft that lands before the next point is ready.

| Set | Dispatched | With a draft | No draft started | Rejected | Stale |
| --- | --- | --- | --- | --- | --- |
| Original | 18 | 6 | 8 | 3 | 1 |
| Held-out | 26 | 9 | 14 | 3 | 0 |

This table assumes each draft lands within one decision point. At 7.0 s per answer it would not, so the scenario readiness here is an upper bound (inferred).

## The time a draft has on Mason's clips

The clip replay uses the as-run updates from the hybrid's prototype run (`$VOICE_HOME/evidence/hybrid/run-h-c`). It reproduced that run's task records and dispatch logs on all 10 clips, with every author (measured). Each decision point happens at the second it finished in the live run. The sweep below uses the stand-in author at fixed latencies. [Faster settings](#faster-settings-a-small-matched-test) has the live clip drafts.

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

Clips 06, 07, and 10 dispatched no task. The 4 tasks at 0.00 are the last request of their utterance. The hybrid's completion answer for those first said yes on the final transcript, at the same point that dispatched them (measured).

| Author latency, s | Dispatched with a draft | Raw, no draft started | Raw, still drafting | Median added wait if dispatch held for the draft, s |
| --- | --- | --- | --- | --- |
| 0.25 | 5 of 9 | 4 | 0 | 0.00 |
| 0.5 | 5 of 9 | 4 | 0 | 0.00 |
| 1 | 1 of 9 | 4 | 4 | 0.33 |
| 2 | 1 of 9 | 4 | 4 | 1.33 |
| 4 | 0 of 9 | 4 | 5 | 3.33 |
| 7.0 (measured median) | 0 of 9 | 4 | 5 | 6.33 |
| 12.0 (measured p95) | 0 of 9 | 4 | 5 | 11.33 |

The clips started 7 drafts for 7 tasks, with no redrafts (measured). The 2 drafted tasks that did not dispatch, in clips 02 and 03, wait on `task_1`'s result (measured). These are 10 clips with one run each, so a different speaker or pace could move the 0.67 s floor (inferred).

## Where it plugs into the prototype

This change does not touch `voice/prototype/`. Two hooks would connect it, both in `voice/prototype/run.py`.

1. In `segment_worker`, after `segmenter.update` returns an applied point, call `Prewriter.observe(point, segmenter.orch.tasks, update.text)`. Send each returned input to the author on a worker thread, and call `Prewriter.land` with the reply.
2. Where `MockTaskService.sync` starts a dispatched task, take the prompt from `Prewriter.dispatch(task)` instead of the bare instruction.

## Calls and cost

This branch made 123 calls to Command Code, with no 429, no 402, and no limit message (measured). That is 123 of the 150-call cap. 120 returned HTTP 200, and 3 were the HTTP 400 answers to `minimal`. `voice/research/delegation/cache/calls.jsonl` holds the 81 scenario calls: the 600-token test call, 32 default drafts, 15 drafts each for `-fast` and `-ultrafast`, the 3 `minimal` probes, and 5 drafts per id with reasoning off. The ledger under `$VOICE_HOME/evidence/prewrite/author-cache/` holds the 42 clip calls: 7 per id on its default reasoning and 7 per id with reasoning off. No Flint call ran, through OpenRouter or any route. The replays made no decider call. Every decider request hit the committed cache or the prototype cache under `$VOICE_HOME` (measured, the offline client raises on a miss).

## Limits

- Each draft request ran once. Latency on a different hour or a longer session is untested.
- The quality check is one reader's judgment on 31 drafts.
- The negation fix to `check` was chosen on the same 31 drafts it is scored on.
- Draft quality on real speech rests on 7 clip requests per model.
- The faster settings ran on 15 of the 33 scenario requests, chosen to cover each kind of task and most of default reasoning's scope additions.
- The reasoning-off race ran on 5 scenario and 7 clip requests per id, with one run each. `low` and the higher efforts were not tested.
