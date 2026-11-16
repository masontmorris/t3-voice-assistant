# The local voice-assistant prototype

The prototype runs the whole assistant loop on this Desktop. A WAV file streams into LFM3-Audio-2B on llama.cpp's realtime server, the live transcript drives the Stage 2 segmenter with the free decider, tasks go to a mock task service, and the task snapshot goes back into the voice session before the model speaks. Each claim below carries a label. "Measured" means a run on this machine produced it, "inferred" means I reasoned it from code or docs, and "guess" means neither.

The turn this doc measures ends with a decider call on the final transcript. The prototype now defaults to the gated turn, which gates each task during speech and makes no call at the end, and `--eou decider` runs the turn described here. See [eou-handoff.md](eou-handoff.md).

## Result

`python -m voice.prototype.run CLIP.wav` runs end to end on all 14 clips: Mason's 10 recordings and Stage 1's 4 synthetic clips. It saves the spoken reply as a WAV and every event as JSON (measured, three passes, no clip failed). With injection method (c), the snapshot goes in as user text with a machine prefix. 13 of 14 snapshot replies invented nothing about the tasks, and the voice started speaking 0.28 s after the audio ended once the decider answers were cached (median of 14 clips, measured).

- **Pick method (c) for now.** The server rejects method (a) outright. Method (b) works, but in 3 of 13 replies the model said "I've set" or "has been placed" about a task the service was holding (measured). The cost of (c) is style: in 11 of 14 replies the model narrates in the third person ("The user wants me to...") instead of talking to Mason (measured).
- **The side-effect hold works.** Across 42 clip runs, no side-effect task executed and none dispatched before the final transcript (measured). Clip 09's two side-effect tasks were both held.
- **The free decider's rate limit sets the latency.** When the last decision needed a live call, the snapshot was ready 2.25 s after the audio ended (median of 8, measured). The call itself took 0.11 s. The rest is the client's 3.1 s spacing between calls, which round 2 set to avoid 429s.
- **The segmenter passed 10 of 14 clips against expectations written before any run** (measured). It missed the implicit request in 07 and 10, merged two of the three requests in 09, and turned "I need help planning tomorrow." into a clarification question in `long`.

## How the pieces connect

All code is in `voice/prototype/`.

| File | What it does |
| --- | --- |
| `run.py` | Starts `llama-server`, streams the clip at real-time pace into a `realtime` voice session and a `?intent=transcription` session at once, and drives one turn. |
| `realtime.py` | The server process and a WebSocket session with a background reader. It uses Stage 1's server flags (`--temp 0 -np 2 -ngl 99`) and the GGUF files from `voice/speech/llamacpp_rt.py`. |
| `transcript.py` | `TranscriptAdapter` turns transcription deltas into `TranscriptUpdate` records. Each update carries the whole utterance, a new revision number, and `is_final` on the last completed item after the final commit. |
| `segmenter.py` | The `Segmenter` interface, the update coalescing, and the two segmenters. The default is the hybrid from `voice/segmentation/hybrid.py` (see [stage2-hybrid.md](stage2-hybrid.md)). `--segmenter baseline` picks `BaselineSegmenter`, which wraps round 2's `Runner("a")` and `Orchestrator` from PR #2. The runs in this doc used the baseline. |
| `tasks.py` | `MockTaskService` and `build_snapshot`. A mock worker completes read-only tasks after 4 s with a canned result. Side-effect tasks, and delegate tasks that no decider answer classified, are held and never run. |
| `report.py` | WER, task scores against `expected.json`, the side-effect check, and the timing tables. It prints no transcript text. |
| `expected.json` | The expected task records per clip, written from `script.txt` and committed before the first run. |

`llamacpp_rt.py` is not imported. It parses `sys.argv` and imports torch on import, which took 7.2 s (measured), and it reads server events only while it waits, so it cannot feed a live consumer.

The turn runs in this order:

1. Both sessions receive the clip in 100 ms chunks. Every transcription delta becomes a transcript update, and the segmenter processes the updates one at a time on a worker thread. After each decision point the task service starts the new dispatches.
2. At the end of the audio, the runner commits both buffers. With method (b) it also asks the voice session for a response that must call a tool.
3. When the final transcript is processed, the service builds the `utterance_snapshot` from the task records alone and the runner injects it.
4. The voice session replies, and the runner saves the reply audio.
5. When the remaining mock tasks finish, the runner sends one `task_update` message for them and saves the second reply.

The snapshot uses the plan's section 5 shape. Its statuses are `pending`, `running`, `completed` with a `result_summary`, `held_for_confirmation`, `needs_clarification`, and `cancelled`. Superseded and withdrawn tasks are left out.

## Run it

Run from the repo root with the Stage 1 venv, inside the GPU lease. Set `VOICE_HOME` and set `VOICE_ENV_FILE` to the env file that holds `OPENROUTER_API_KEY`:

```
python -m voice.prototype.run "$VOICE_HOME/evidence/stage1/audio/two_request.wav"
```

The command prints the server PID and one metrics line per clip. It writes `two_request.json`, `two_request_reply.wav`, and, when a task finishes after the reply, `two_request_update.wav` to `$VOICE_HOME/evidence/prototype/inject-c/`. Pass several clips to reuse one server. `--inject a|b|c` picks the injection method, `--status-only` offers method (b) only the `get_task_status` tool, and `--delay` sets how long a mock task takes.

Weather and time tasks now make real lookups instead of mock ones. [real-tools.md](real-tools.md) covers the tools, the `--mock-tools` flag, and the results.

The decider cache and call ledger for these runs live in `$VOICE_HOME/evidence/prototype/decider-cache/`, not in `voice/segmentation/cache/`, because their keys hold the transcripts of Mason's recordings. The runner caps live decider calls at 150. Cached requests make no calls.

## Injection methods

llama.cpp's realtime server has no mid-session system item. I tested three other ways to get the snapshot in, in the brief's order. Methods (b) and (c) ran on all 14 clips. The tool-only variant of (b) ran on the 4 synthetic clips.

| Method | What the server does | Replies that invent nothing | Replies that talk to the user as "you" |
| --- | --- | --- | --- |
| (a) Client-made `function_call` item, then its `function_call_output` | Rejects both. The call item gets "only user messages and function call outputs are supported", and the output gets "a function call output must follow the response that called it". The reply then ignores the task. | Not applicable | Not applicable |
| (b) Output of the model's own call, `delegate` and `get_task_status` offered, `tool_choice: required` | Accepts the `function_call_output`. The model called a tool on 13 of 14 clips. On `single` it made no call, so the snapshot had nowhere to go and the reply failed with `conversation_empty`. | 10 of 13 | 13 of 13 |
| (b) with only `get_task_status` offered | `tool_choice: required` is not enforced. The model called the tool on 2 of 4 synthetic clips. | Not graded | Not graded |
| (c) User text with the prefix `[task service snapshot, machine-generated, not spoken by the user]` | Accepts it as a user message. | 13 of 14 | 3 of 14 |

All results are measured, one run per clip, method (a) on one clip. Method (a) fails a type check on the item before the model sees it, so one clip settles it (inferred from the error text).

The rubric for "invents nothing": the reply says nothing about a task that the snapshot does not support. That covers a done claim for a task that is not `completed`, a promised step the snapshot does not show, and a result or reason the snapshot does not hold. Leaving a status out is not counted as invention. I read every reply once and graded it. A second reader might move a borderline case either way.

The two failure modes differ, and that difference decides the pick. With (b), the model's own `delegate` call reads to it like the action itself. On the synthetic `long` clip it said "The order for more coffee for the office has been placed" while that task was `held_for_confirmation`. With (c), the replies stay inside the snapshot but read it aloud as a description. On the synthetic `correction` clip it said "The user asked to set a timer for 10 minutes, but then said to make it 15 minutes instead." That reply also gave an invented reason for the hold, the one invention (c) made in 14 snapshot replies. Wrong status is the failure the plan rules out, so (c) wins. Narration is a prompt problem that one persona change did not fix (measured, the change ran on the synthetic clips before Mason's clips ran).

Method (b) also has side effects in the segmenter (measured, pass 2):

- On 2 clips, `02_two_requests` and `two_request`, one delegate covered both requests. The segmenter's `same_as` question can match a delegate to one segment only, so each of those delegates became an extra task. The service held both, because no decider answer classified them. Before that rule, one smoke run started a mock execution of an "email Dana" task through this path.
- Each delegate adds a decision point, and live decision points pay the 3.1 s spacing. `long` got four delegates, and its snapshot was ready 10.07 s after the audio ended.
- The delegate changed the decider's answers on clips 07 and 10. With the delegate in the decision state, the decider classified "I wonder if it's going to rain on Saturday" as a request, so both clips passed under (b) and failed under (c).

## Per-clip results

Pass 1 ran method (c) on all 14 clips inside `quiet_run.py` and the GPU lease. The decider made a live call for each request it had not cached. The synthetic clips were the tuning set, so they ran first in earlier passes and hit the cache here.

| Clip | WER | Tasks | Failures | Snapshot statuses | Side-effect tasks held / executed / dispatched early | Final transcript (s) | Snapshot ready (s) | First reply audio (s) | Live decider calls |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 01_single | 0.00 | pass | - | running | 0 / 0 / 0 of 0 | 0.06 | 0.32 | 0.44 | 1 |
| 02_two_requests | 0.00 | pass | - | running, pending | 0 / 0 / 0 of 1 | 0.15 | 0.15 | 0.31 | 0 |
| 03_dependent | 0.00 | pass | - | running, pending | 0 / 0 / 0 of 1 | 0.13 | 2.09 | 2.38 | 3 |
| 04_correction | 0.00 | pass | - | held_for_confirmation | 1 / 0 / 0 of 2 | 0.13 | 2.41 | 2.72 | 2 |
| 05_topic_change | 0.00 | pass | - | held_for_confirmation, running | 1 / 0 / 0 of 1 | 0.11 | 2.73 | 3.06 | 3 |
| 06_not_a_request | 0.00 | pass | - | - | 0 / 0 / 0 of 0 | 0.19 | 0.30 | 0.50 | 1 |
| 07_implicit | 0.00 | FAIL | T1 missing | - | 0 / 0 / 0 of 0 | 0.10 | 0.19 | 0.37 | 1 |
| 08_cancel | 0.00 | pass | - | completed | 0 / 0 / 0 of 1 | 0.09 | 6.73 | 7.07 | 5 |
| 09_run_on | 0.14 | FAIL | T2 missing | held_for_confirmation, held_for_confirmation | 2 / 0 / 0 of 2 | 0.10 | 2.88 | 3.24 | 3 |
| 10_noisy | 0.00 | FAIL | T1 missing | - | 0 / 0 / 0 of 0 | 0.03 | 0.04 | 0.22 | 0 |
| correction | 0.00 | pass | - | held_for_confirmation | 1 / 0 / 0 of 1 | 0.12 | 0.12 | 0.31 | 0 |
| long | 0.00 | FAIL | extra needs_clarification task; asked to clarify | needs_clarification, completed, completed, pending, held_for_confirmation, held_for_confirmation | 2 / 0 / 0 of 3 | 0.11 | 0.11 | 0.34 | 0 |
| single | 0.00 | pass | - | running | 0 / 0 / 0 of 0 | 0.12 | 0.12 | 0.26 | 0 |
| two_request | 0.00 | pass | - | running, pending | 0 / 0 / 0 of 1 | 0.10 | 0.10 | 0.25 | 0 |

Times are seconds after the audio ended. "Tasks" scores the segmenter's records with `voice.segmentation.score`, the Stage 2 scorer, against `expected.json`.

- **WER.** WER is 0.00 on 13 of 14 clips (measured). `report.py` lowercases, drops punctuation, and spells digits as words before it counts, so "at 3" matches "at three". The one error is on 09, where the ASR wrote the product name in `script.txt` as two words. That costs a substitution and an insertion. `script.txt` notes that Mason spoke naturally, so a nonzero WER could also be his wording. Here the transcripts matched the script.
- **The misses are all in the segmenter** (measured, the same in all three passes for 07, 09, 10, and `long`). The clause splitter cuts at "and then" but not at "then" alone, so 09's first two requests became one task. Nothing in round 2's question set asks about implicit requests, so 07 and 10 got no task. The decider answered "I need help planning tomorrow." with `wait`, and the policy turns `wait` on a final transcript into `needs_clarification`.
- **Clip 09 and the side-effect hold.** The decider flagged both of 09's tasks as side effects. The orchestrator held both until the final transcript, then dispatched them, and the service held both. Neither executed. The reply told Mason both tasks need his confirmation (measured, passes 1 and 2, the passes whose replies I read). The same held for every side-effect task on every clip: 0 executed and 0 dispatched early in 42 clip runs.
- **Clip 07 against clip 10.** The noise in 10 changed nothing measurable. Both final transcripts had WER 0.00. Every decision request for 10 hit the cache entries 07 had just made, so the decider saw byte-identical transcript updates for both clips. The task outcome was the same in every pass. On one pair of clips, that says the ASR coped with this level of noise, not that noise never matters (inferred).

## Timings

Each cell gives the median, the range, and the clip count, over one run per clip.

| Seconds after the audio ends | Pass 1, (c), live decider where uncached | Pass 3, (c), every decision cached | Pass 2, (b) |
| --- | --- | --- | --- |
| Final transcript | 0.11 (0.03 to 0.19), n=14 | 0.12 (0.04 to 0.19), n=14 | 0.14 (0.05 to 0.26), n=14 |
| Snapshot ready | 0.25 (0.04 to 6.73), n=14 | 0.12 (0.04 to 0.19), n=14 | 0.44 (0.30 to 10.07), n=14 |
| Snapshot ready, final decision made a live call | 2.25 (0.19 to 6.73), n=8 | none | 0.77 (0.45 to 10.07), n=7 |
| Snapshot ready, final decision cached | 0.12 (0.04 to 0.15), n=6 | 0.12 (0.04 to 0.19), n=14 | 0.36 (0.30 to 0.43), n=7 |
| First reply audio | 0.41 (0.22 to 7.07), n=14 | 0.28 (0.16 to 0.36), n=14 | 0.55 (0.37 to 10.69), n=13 |
| First reply audio, counted from snapshot ready | 0.19 (0.13 to 0.36), n=14 | 0.17 (0.08 to 0.25), n=14 | 0.09 (0.07 to 0.62), n=13 |

All runs were measured inside `quiet_run.py` and the GPU lease, on Q4_K_M weights on the RTX 3060.

What limits each number:

- **Final transcript.** Final transcripts arrive 0.11 s after the audio ends, close to Stage 1's 0.14 s with both sessions open (measured). This is the live ASR's flush after the final commit.
- **Snapshot ready, with a live decision.** The decider client waits until 3.1 s have passed since its previous live call (`MIN_INTERVAL_S` in `voice/segmentation/client.py`). Round 2 set that spacing after back-to-back calls drew 429s. In pass 1, the last decision point took 2.02 s of wall time beyond its call time (median of 8, range 0.01 to 2.98), while the call took 0.11 s (measured). Clip 08 shows the worst case. The final transcript was ready 0.09 s after the audio ended, but the segmenter still had two decision points on older partials in its queue. Each waited out the spacing, so the final call started at 3.61 s and the snapshot was ready at 6.73 s (measured, `run1-c/08_cancel.json`).
- **Snapshot ready, cached.** With no live call, the snapshot follows the final transcript within 0.01 s (measured, pass 3). The segmenter and the snapshot builder cost almost nothing.
- **First reply audio from snapshot ready.** The voice session needs 0.17 s to start speaking after the injection (median, pass 3). That is in line with Stage 1's 0.23 s from audio end to first audio (measured). Method (b) starts faster from the snapshot, 0.09 s, because its tool turn already ran (inferred). But that tool turn delays the snapshot, so (b) is slower end to end.

Two changes would cut the live-decision wait (inferred, not run). The first drops queued partial updates once a newer update is waiting, so the final decision never queues behind stale ones. The second skips the first decision point on a one-word transcript. Every clip's first decision point fires at revision 1, because the segment count goes from 0 to 1 on the first word. In pass 1 that request was not cached for 4 of the 8 clips that made live calls, so it cost a live call and started the spacing clock (measured).

## Later task updates

When mock tasks finish after the first reply, the runner waits for them. It then sends one user message prefixed `[task service update, machine-generated, not spoken by the user]` that holds a `task_update` for each completion, and asks for a reply. That message comes at the next safe boundary in a one-clip run, after the first reply ends and with no user audio arriving. The `function_call_output` route is not available for updates, because the server takes an output only right after the response that made the call (measured, method (a)).

Each update reached the voice exactly once, and each update reply stated the completed result (measured, 6 updates in pass 1 and 9 in pass 2). A completion that was already in the snapshot got no update. `MockTaskService.take_updates` marks each completion as told. On clip 08 in pass 1, the calendar task finished before the snapshot, so the snapshot carried its result and no update followed. One update reply under each method promised a next step that would not happen. On clip 03, after the flight search finished, the reply said it would book the flight, but the booking task was pending on the search and held as a side effect.

## Limits

- Each clip ran once per pass, so the per-clip numbers are single samples. The two (c) passes differ only in decider caching. Their voice-side timings agree within 0.02 s at the median.
- One reader graded every reply against the rubric above. The grades for real clips are in the run records, not quoted here, because the replies repeat Mason's words.
- The persona prompt changed once after the synthetic smoke runs, before any run on Mason's clips. The synthetic clips are the tuning set, and Mason's ten clips are the test set.
- The mock worker's results are canned strings chosen per topic. A real agent's results would be longer and less predictable, and the voice would have more room to invent.
- The prototype runs one turn per clip. It does not test barge-in, a user who speaks during an update, or a second turn that refers to the first.
- A task that depends on another stays `pending` after its dependency completes, because the mock service does not release dependents. Stage 3 needs that rule.

## Open decisions for Mason

- Which method goes forward. The recommendation is (c), with prompt work on the narration. The other choice is (b) plus a rule that a delegate call is only a request. That choice needs a fix for the "I've done it" replies.
- Whether the service should keep holding tasks that no decider answer classified. Today that rule catches whole-utterance delegates, at the price of a duplicate held task in the snapshot.
- Whether the segmenter should drop stale partial updates. That would trade the in-order decision trail Stage 2 scored for a shorter wait on the final decision.

## Reproduce

The offline tests need no GPU, no key, and no network:

```
python -m unittest discover -s voice/prototype/tests -t .
```

```
----------------------------------------------------------------------
Ran 21 tests in 0.001s

OK
```

The three passes ran inside the GPU lease and `quiet_run.py`, with the 10 clips from `$VOICE_HOME/audio/real/` and the 4 from `$VOICE_HOME/evidence/stage1/audio/`:

```
python -m voice.prototype.run CLIPS... --inject c --out "$VOICE_HOME/evidence/prototype/run1-c"
python -m voice.prototype.run CLIPS... --inject b --out "$VOICE_HOME/evidence/prototype/run2-b"
python -m voice.prototype.run CLIPS... --inject c --out "$VOICE_HOME/evidence/prototype/run3-c"
python -m voice.prototype.report "$VOICE_HOME/evidence/prototype/run1-c" "$VOICE_HOME/evidence/prototype/run2-b" "$VOICE_HOME/evidence/prototype/run3-c"
```

The decider made 52 live calls for this work, all HTTP 200, with no 429s and no billing signals (measured, the ledger in `$VOICE_HOME/evidence/prototype/decider-cache/calls.jsonl`). That count includes the smoke and tuning runs. With that cache in place, a rerun of the same clips makes no new calls, as long as the transcription deltas come out the same.

## Files outside the repo

| Path | Contents | Size |
| --- | --- | --- |
| `$VOICE_HOME/evidence/prototype/` | Run records, reply WAVs, server logs, the decider cache and ledger, and the report output | 51 MB |

No model, package, or venv was added. The prototype uses the Stage 1 venv and the GGUF files already listed in [licenses.md](licenses.md).
