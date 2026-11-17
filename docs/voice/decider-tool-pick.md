# The decider picks the tool

The decider used to decide whether a task may start, and a separate word-rule step in `voice/tools/registry.py` picked
which tool ran it. The rules misrouted Mason's own call: after a San Diego forecast, "Pick a city on the east coast to
check the weather for." re-ran San Diego, because the word "weather" was in the request and the rules reuse the earlier
task's place (measured, turn 025 of the 20261116-152653 recording). Now the decider answers which tool in the same
decisions request that decides dispatch, and a dispatch runs that tool when the answer is confident.

Measured on 2026-11-16, branch `voice/decider-tool-pick` from `3c0576b98a`. Every claim says whether it is measured,
inferred or a guess. The summarizer model for the text level was Liquid's hosted `d1.2`.

| | Word rules | Decider, tool question at 0.30 |
| --- | --- | --- |
| Gate eval, train 46 items | 70% | 91%, both samples |
| Gate eval, held-out 24 items | 62% | 88% and 92%, one per sample |
| Mason's 37 real turns, the named failure | San Diego re-run | the general worker, no re-run (the worker timed out) |

The last row is the only real-audio claim in that table and it is one turn. The sections below are the evidence.

## What a tool now declares

Each tool in the registry carries two lines. `label` is the short name a failure sentence speaks, unchanged in wording
("weather lookup"). `line` is what it does and when to use it, and that line is the option text the decider reads:

| Option | Line |
| --- | --- |
| `weather` | Weather forecast for a named city on a named day: the high temperature and the chance of precipitation. Use it for a forecast, or for anything that depends on one. |
| `time` | The current local time in a named city. Use it when the speaker asks what time it is. |
| `agent` | General lookup: information from outside this call that no tool above covers, including a request that asks the assistant to choose a detail first, such as a city. |
| `none` | No tool: conversation, a reaction, or a question the assistant answers from this call's own words. |

`Registry.choices` builds that option set from the tools it holds plus the two fixed options, and
`voice/segmentation/contract.py` puts it in a `choice` question per segment. A tool added to the registry therefore
appears in the question with no other edit, and a test asserts that the question's criteria are exactly the registered
tools plus `none` and `agent` and nothing else (`voice/segmentation/tests/test_contract.py`).

The question rides in the request the segmenter already sends. Liquid bills the state once per request and each
question adds only its own tokens, so this is one call per decision point as before (measured, see
[Cost and latency](#cost-and-latency)).

## Dispatch

`Registry.dispatch` is the new entry. A tool answer that clears `TOOL_THRESHOLD` decides, and only an answer under the
bar falls back to the word rules. A confident `none` runs nothing, and so does `agent` when no general worker is
configured, because the rules are what misrouted the requests this replaces. The routing record on each execution says
`via: decider` or `via: rules`, carries the tool answer and its margin, and sets `disagreement: true` when the rules
ran against an unsure answer that named a different tool.

The bar is 0.30. Two independent samples of the 70 items, three readings each, put train accuracy at 91% at every bar
from 0.00 to 0.45, and held-out at 88% and 92% across the same range (measured). Above 0.50 both samples lose accuracy,
because every item whose answer sits between 0.30 and the bar falls back to the weaker rule. 0.30 sits inside the flat
region of both samples with room either side. The bar is weakly identified by these items; the robust result is the gap
to the word rules, not the exact value.

Held-out is a fixed split of the same 70 items; no held-out item was used to choose the bar (measured: the tuning code
scores train first and prints held-out beside it).

## Text level

`python -m voice.segmentation.live_gate_eval --decider liquid --tools --fresh` ran the 70 items three times each, 210
traces per sample, two samples. Every item now carries the tool a dispatch should run, or `none`, written by hand before
any run, and the fourteen items that name a place and a day carry those too.

The eval records each segment's tool answer whether or not the gate made a task, so the comparison does not depend on
the gate. The label for a correction item is the tool the amended task would need, because live mode routes the amended
request through the registry; the decider is asked nothing on a correction turn (measured: `close_with_decider` calls the
decider only when the utterance's intent is a request), so those items score the word rules on both sides.

| Split | n | Word rules | Decider, sample 1 | Decider, sample 2 |
| --- | --- | --- | --- | --- |
| Train | 46 | 32 (70%) | 42 (91%) | 42 (91%) |
| Held-out | 24 | 15 (62%) | 21 (88%) | 22 (92%) |

Both values are measured. The wrong items at the shipped bar are:

| Item | Words | Should be | Got | Margin | Who decided |
| --- | --- | --- | --- | --- | --- |
| t32 | "I'll check the weather later." | none | weather | 0.22 | decider, then rules |
| t43 | "Send that to Sam." | agent | none | 0.33 | decider, then rules |
| t50 | "And the return flight?" | agent | none | 0.40 | decider, then rules |
| t55 | "Actually, not Thursday, Friday." | agent | none | none | the rules |
| t58 | "Nope, not Boulder, Portland." | weather | none | none | the rules |

The two marked "decider, then rules" held a margin under 0.30, so the word rules made the pick and both were wrong
anyway. In sample 1 the same two items came out differently, which is the model's own spread on one body (measured).
t32 is a description of a task, so no dispatch happens whatever the tool answer says; it is in the table because the
tool question should still have said `none`.

The two corrections are a registry gap, not a decider one. An amended instruction reads "What's the weather in Boulder
tomorrow? (corrected: Portland)", and no parser claims that string (measured with `Registry.route`). Fixing it means
letting the amended task carry the corrected place, which is a separate change.

The gate is not what these items test, but it is worth one line: scored by the same rule on the same traces, train is
89% with 5 false holds and held-out 83% with 4, which is exactly the number [liquid-decider.md](liquid-decider.md)
reports without the tool question (measured). Asking the tool question did not disturb the gate.

## Arguments

d1.2 reads the place as a `text` answer and the day as a `choice` over the same day names the weather tool already uses.
Both ride in the same request. This is on by default (`VOICE_DECIDER_ARGS=off` turns it off).

| Reading | n | Exact |
| --- | --- | --- |
| Decider's place and day, filled for dispatch | 10 | 9 |
| The word rules' place and day | 10 | 8 |

Measured on the eval's ten labeled items that the decider answered. The one miss is `t64`, "What's the whether in DEN
ver?", where the decider reads "DEN ver" and the rules read "DEN"; both are wrong and neither is the city.

The decider's raw reading is deliberately incomplete: it leaves out a place that only an earlier segment named, and
`s1_place` says so in the question. Dispatch fills what it leaves out from the word rules' own reading, the earlier
lookup and the default place, in that order, so an empty place costs nothing. On the real turns that fill is where the
gain is: with the questions on, turn 022's reading was San Diego and the forecast ran for San Diego, where the same turn
under the word rules forecasts Portland (measured, see the next section).

Asking the day as a `date` answer was not tried, because the docs say the model reads a written value rather than
converting it, and "Saturday" would need today's date to become an ISO date (inferred).

## Real audio

Two live servers from this branch, `--decider liquid`, REAL tools, and `VOICE_AGENT=on`, each in a `voice-toolpick` GPU
lease. The client sent the 37 `turn_NNN_input.wav` clips in order over loopback. The first arm asked the tool question
only; the second arm also asked the place and day, which is the shipped default. Driver, scorer and records are in
`$VOICE_HOME/evidence/toolpick/`. Each replay took 8 minutes (measured, wall clock including server startup; 59 and 63
decisions at a median of 166 ms and 167 ms).

The recording this is compared with ran without the general worker; `VOICE_AGENT` was unset. Every turn where a replay
uses the worker is therefore a change the worker's presence explains, not the tool question (inferred from the recorded
turns 025 and 028, which ran no lookup at all).

| Measurement | Tool question only | Tool question and arguments |
| --- | --- | --- |
| Clips sent | 37 | 37 |
| Server turns | 37 | 37 |
| Turns with a transcript | 37 | 37 |
| Recording turns matched | 35 of 37 (the two wordless ones) | 35 of 37 |
| Lookups in the recording | 7 | 7 |
| Lookups in the replay | 9 | 7 |
| Replay lookups decided by the decider | 9 | 7 |
| Replay lookups decided by the word rules | 0 | 0 |
| Turns whose tool or arguments changed | 5 | 5 |

Every value is measured. Every dispatch in both replays carried a tool answer over the bar, so the word rules decided
nothing in either call. That is the strongest statement the runs support and also their main risk, because the rules no
longer catch a confident wrong answer.

| Turn | Words | Recording | Tool question only | Tool question and arguments |
| --- | --- | --- | --- | --- |
| 002 | "check the weather in Raleigh ... and tell me what day George Washington was born on" | weather Raleigh, today | weather Raleigh, today; agent for the second clause | weather Raleigh, today |
| 003 | "Are you checking the weather?" | weather Raleigh, today | nothing | nothing |
| 016 | "Wait, do it again." | nothing | weather Durham, today | weather Durham, today |
| 022 | "Okay, and what about San Diego?" | nothing | weather Portland, today | weather San Diego, today |
| 023 | "You can't check the weather in San Diego?" | weather San Diego, today | weather San Diego, today | nothing |
| 025 | "Pick a city on the east coast to check the weather for." | weather San Diego, today | agent, timed out | agent, timed out |

All values are measured. Each change, in order:

- **025, the failure this work exists for, is fixed in both arms.** The decider picked the general worker at a margin of
  0.41 in the first arm and 0.58 in the second, so no San Diego forecast ran. The worker then timed out after 12 s,
  because every OpenRouter call it made came back 429 (measured, four 429s in
  `$VOICE_HOME/evidence/agent/calls.jsonl` in each arm's minute). The turn reported a failed lookup instead of a wrong
  forecast.
- **022 is the argument questions' turn.** The word rules cannot read a place there, because "about San Diego?" has no
  preposition, so their fill reuses Portland. The decider named weather in both arms, 0.90 without the arguments (place
  filled from Portland) and 0.67 with them, reading San Diego and today. This is the real-audio reason the questions
  ship on.
- **023 needs no lookup once 022 works.** The decider called it conversation there, with a tool answer of `none` at
  0.50, where the first arm ran a San Diego forecast as the recovery from 022's failure. The recording ran a lookup
  there only because its 022 had failed (inferred from the turn order).
- **003 is d1.2's own behaviour, not the tool question.** The earlier d1.2 replay on this recording reported the same
  turn as its one changed task decision, and the 70-item eval's `about_assistant` items expect no dispatch. The tool
  answer here was `none` (measured in the text probe below).
- **016 repeats the last forecast** in both arms, at 0.59 and 0.53, where the word rules would route nothing. "Do it
  again" does ask for a repeat, so this reads as a plausible dispatch rather than a misroute (judgment). The place is
  the earlier task's in both arms, because nothing reads one from those words.
- **002 gained a second lookup in the first arm only**, because the worker is on and the second clause of the utterance
  reached it. The second arm ran the weather lookup alone. This is the worker, not the tool question (inferred).

Turn 028, "Do I need to wear a jacket?", is the one expected to change and did not. The decider named `weather` at a
margin of 0.60 in the first arm and 0.19 in the second, which is the right tool either way, but the task never reached a
dispatch: the gate held it (measured, no execution and no dispatch log entry for that turn). The tool pick is not what
blocks that turn.

A text probe over the replays' 37 transcripts asked the tool, place and day questions with the recording's lookups
carried in (`args_probe.py`, 8 calls, $0.0002). It read Raleigh and today for turn 002, Portland for 021, San Diego for
022 and 023, and no place for 025, with margins 0.5 to 0.98 (measured). The probe is not the live path: it omits the
segmenter's conversation-mode criteria and its task list, so its tool answers differ from the live ones on three turns
and only its place and day readings are used here (measured).

## Cost and latency

Same ten decision bodies, three versions each, one fresh call per body to Liquid (measured; `cost_probe.py` in the same
evidence directory):

| Body | Questions | Input tokens | Change | Median latency |
| --- | --- | --- | --- | --- |
| As before | 3 | 410 | | 136 ms |
| Plus the tool question | 4 | 624 | +214 tokens, +52% | 152 ms |
| Plus the tool question and the arguments | 6 | 817 | +407 tokens, +99% | 128 ms |

At $0.02 per million input tokens, +407 tokens is $0.0000081 per decision, so a call of 40 decisions costs about three
tenths of a cent more (inferred from the measured token counts and the published price). The latency column is one
reading per body and the third row is lower than the first, which is the model's spread rather than a speed-up
(inferred). A single decision is 128 to 152 ms, so the tool question does not change what a dispatch costs in time.

## What still fails

- The general worker was unavailable for both turns that needed it, in both arms, on HTTP 429 from the free OpenRouter
  tier (measured). A tool pick that ends in a timeout is not a better outcome than the wrong forecast it replaced, so
  the east-coast fix is only half demonstrated: the misroute is gone and the answer is still not there.
- The confident-tool path removes the word rules' place guard. When the decider names `weather` for a follow-up whose
  place nothing can read, dispatch fills the earlier task's place. Turn 016 does that deliberately; with the argument
  questions off, turn 022 did it wrongly (measured). The argument questions are the fix, and they are now on by
  default, so the guard's absence is covered only when the decider's reading is not empty.
- A correction amends an earlier request into a string no parser claims ("... (corrected: Portland)"), so both
  correction items that need a weather lookup get `none` (measured on the eval).
- Five eval items are wrong at the shipped bar, two of them from the word rules under the bar (measured). t32's
  "I'll check the weather later." is a description the tool question reads as a weather request at a low margin.
- The 70-item eval is small, and held-out is 24 items, so one item moves 4 points. The bar's value is the weakest
  number in this page.
- Neither replay's replies were checked for whether they stated the right forecast. The earlier d1.2 replay found one
  turn repeating the previous turn's line; the first arm here repeated "I'm checking the weather in a city on the East
  Coast. Which one?" on its last three turns, and the second did not (measured, client transcripts; cause not traced,
  guess: the failed worker task's update).

## Reproduce

```
python -m voice.segmentation.live_gate_eval --decider liquid --tools --fresh \
    --decider-dir $VOICE_HOME/evidence/live-gate-eval/calls-tools \
    --traces $VOICE_HOME/evidence/live-gate-eval/traces-tools.jsonl --tool-sweep
python $VOICE_HOME/evidence/toolpick/run_replay.py toolpick-real     # inside the GPU lease
python $VOICE_HOME/evidence/toolpick/score_tools.py toolpick-real
python $VOICE_HOME/evidence/toolpick/args_probe.py
python $VOICE_HOME/evidence/toolpick/cost_probe.py
```

`--arguments` adds the place and day questions to the eval, for the argument comparison. `VOICE_DECIDER_ARGS=off`
turns them off in a live run, which is how the first replay arm above was run.
