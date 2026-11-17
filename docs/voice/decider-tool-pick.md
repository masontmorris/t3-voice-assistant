# The decider picks the tool

The decider used to decide whether a task may start, and a separate word-rule step in `voice/tools/registry.py` picked
which tool ran it. The rules misrouted Mason's own call: after a San Diego forecast, "Pick a city on the east coast to
check the weather for." re-ran San Diego, because the word "weather" was in the request and the rules reuse the earlier
task's place (measured, turn 025 of the 20261116-152653 recording). Now the decider answers which tool in the same
decisions request that decides dispatch, and a dispatch runs that tool when the answer is confident.

Measured on 2026-11-16, branch `voice/decider-tool-pick` from `3c0576b98a`, and the polish round on branch
`voice/polish-toolpick` from `53ff4ff5a5`. Every claim says whether it is measured, inferred or a guess. The
summarizer model for the text level was Liquid's hosted `d1.2`.

| | Word rules | Decider, tool question at 0.30 |
| --- | --- | --- |
| Gate eval, train 46 items | 74% | 98% |
| Gate eval, held-out 24 items | 62% | 92% |
| Mason's 37 real turns, the named failure | San Diego re-run | the general worker, no re-run |

Both decider values are the polish round's fresh sample. The first two rows sit beside the word rules scored on the same
items, and the word rules' own number rose from 70% when a correction began running the corrected task's tool. The last
row is the only real-audio claim in that table and it is one turn. The sections below are the evidence, including the two
samples the earlier round measured.

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

`Registry.dispatch` is the entry. Three sources decide a call, in this order. `Registry.corrected` is a correction's
call and it wins, because a correction is about that task and the words of an amended instruction are no evidence of
which tool it needs. Then a tool answer that clears `TOOL_THRESHOLD` decides, and only an answer under the bar falls
back to the word rules. A confident `none` runs nothing, and so does `agent` when no general worker is configured,
because the rules are what misrouted the requests this replaces. The routing record on each execution says
`via: decider`, `via: correction` or `via: rules`, carries the tool answer and its margin, and sets
`disagreement: true` when the rules ran against an unsure answer that named a different tool.

The bar is 0.30. Two independent samples of the 70 items, three readings each, put train accuracy at 91% at every bar
from 0.00 to 0.45, and held-out at 88% and 92% across the same range (measured). Above 0.50 both samples lose accuracy,
because every item whose answer sits between 0.30 and the bar falls back to the weaker rule. 0.30 sits inside the flat
region of both samples with room either side. The bar is weakly identified by these items; the robust result is the gap
to the word rules, not the exact value. The polish round's sample is flat too, at 98% train and 92% held-out from 0.00
to 0.45 (measured). No threshold moved in the polish round, because none is tuned on these items.

Held-out is a fixed split of the same 70 items; no held-out item was used to choose the bar (measured: the tuning code
scores train first and prints held-out beside it).

## Text level

`python -m voice.segmentation.live_gate_eval --decider liquid --tools --arguments --fresh` ran the 70 items three times
each, 210 traces, and cost $0.00323 (measured). Every item carries the tool a dispatch should run, or `none`, written by
hand before any run, and the fourteen items that name a place and a day carry those too.

The eval records each segment's tool answer whether or not the gate made a task, so the comparison does not depend on
the gate. The label for a correction item is the tool the corrected task would need. The decider is asked nothing on a
correction turn (measured: `close_with_decider` calls the decider only when the utterance's intent is a request), so
those items score the registry on both sides: since the polish round, a correction runs the corrected task's own tool.

| Split | n | Word rules, before | Word rules, after | Decider, sample 1 | Decider, sample 2 | Decider, sample 3 |
| --- | --- | --- | --- | --- | --- | --- |
| Train | 46 | 32 (70%) | 34 (74%) | 42 (91%) | 42 (91%) | 45 (98%) |
| Held-out | 24 | 15 (62%) | 15 (62%) | 21 (88%) | 22 (92%) | 22 (92%) |

Every value is measured; samples 1 and 2 are the earlier round's, sample 3 is the polish round's. The word rules' train
number rose by two, both correction items, and held-out did not move. The decider's sample 3 rose by three on train and
one on held-out, which is more than the correction fix explains, so read that rise as the model's spread as well
(inferred: no code that forms a tool answer changed in this round).

The wrong items at the shipped bar in sample 3 are:

| Item | Words | Should be | Got | Margin | Who decided |
| --- | --- | --- | --- | --- | --- |
| t32 | "I'll check the weather later." | none | agent | 0.39 | the decider |
| t33 | "My sister wants me to look up flights to Lisbon." | none | agent | 0.87 | the decider |
| t51 | "What about Friday morning?" | agent | none | 0.46 | the decider |

t32 is a description of a task, so no dispatch happens whatever the tool answer says; it is in the table because the
tool question should still have said `none`. It answered `weather` at 0.22 in the earlier round and `agent` at 0.39
here, wrong both times and at different margins (measured). t33 is a description too, and the decider named the general
worker for it at 0.87 both rounds, which is the one confident wrong answer left. t51 asks for a calendar in a follow-up
and the decider called it `none` at 0.46, above the bar, so that answer stands and the item's label is missed.

The three items the earlier round read as low-margin, as sample 3 answered them:

| Item | Should be | Answer | Margin | Label met |
| --- | --- | --- | --- | --- |
| t32 | none | agent | 0.39 | no, and the answer moved from `weather` at 0.22 in the earlier round |
| t43 | agent | agent | 0.43 | yes, in this sample, where the earlier round's 0.33 fell under the bar |
| t50 | agent | agent | 0.59 | yes, in this sample, where the earlier round's 0.40 was under it |

The last two clear the bar in sample 3 and were under it in both earlier samples, so they hold by a margin in the model's
own spread rather than by anything this round changed (measured). Neither task runs either way: the decider read both as
needing a clarifying question, so they are held before the gate is asked (`readiness clarify`, measured).

The two corrections were a registry gap, not a decider one, and the polish round closes it. A correction runs the
corrected task's own tool with the argument the correction changes, so `t58`'s weather lookup runs for Portland and
`t55`'s calendar task, which this build cannot run, goes to the general worker its label names (measured). The gap had two
causes. The replacement a correction stated kept the value it rejected, "not Boulder, Portland" as one string, so nothing
amended. And the amended sentence was re-routed through the word rules, which claimed neither a calendar nor a corrected
place.

The gate is not what these items test, but it is worth one line: scored by the same rule on the same traces, train is
89% with 5 false holds and held-out 83% with 4, which is exactly the number [liquid-decider.md](liquid-decider.md)
reports without the tool question (measured). Asking the tool question did not disturb the gate, and the polish round's
sample scores the same 89% and 83% (measured).

## Arguments

d1.2 reads the place as a `text` answer and the day as a `choice` over the same day names the weather tool already uses.
Both ride in the same request. This is on by default (`VOICE_DECIDER_ARGS=off` turns it off).

| Reading | n | Exact, before the polish round | Exact, after |
| --- | --- | --- | --- |
| Decider's place and day, filled for dispatch | 10 | 9 | 7 |
| The word rules' place and day | 10 | 8 | 7 |

Both columns are measured on the eval's ten labeled items that the decider answered; the second is the polish round's
fresh sample. The two items the guard costs are `t52`, "And what about the day after?", and `t67`, "And s aturday?".
Both are follow-ups whose words do not say so to a parser: "the day after" is not a day name, and the ASR split
Saturday. The carry used to fill their place from the earlier call, and the decider's own reading does not supply one.
`t52`'s carried day was wrong anyway, "tomorrow" for the day after tomorrow (measured). `t64` is wrong in both columns:
"DEN ver" and "DEN" are neither of them the city.

The decider's raw reading is deliberately incomplete: it leaves out a place that only an earlier segment named, and
`s1_place` says so in the question. Dispatch fills what it leaves out from the word rules' own reading, the earlier
lookup and the default place, in that order, so an empty place costs nothing. The earlier lookup is a guess, and the
polish round makes it a narrower one: `Registry.continues` asks the tool's own parser whether the words continue it,
which needs a subject word or a value of its own, a place or a day. The decider's confident path filled from the earlier
call whatever the words said, so "Wait, do it again." re-ran the Durham forecast two turns after the hello it meant. On
turns that are follow-ups the fill is where the gain is: with the questions on, turn 022's reading was San Diego and the
forecast ran for San Diego, where the same turn under the word rules forecasts Portland (measured, see the next section).

A place answer that is not a name is dropped and logged, and the word rules' reading fills the argument instead (the
`contract.py` parse boundary). That is what turn 020 of the replay showed (see [Real audio](#real-audio)).

Asking the day as a `date` answer was not tried, because the docs say the model reads a written value rather than
converting it, and "Saturday" would need today's date to become an ISO date (inferred).

## Real audio

Two live servers from this branch, `--decider liquid`, REAL tools, and `VOICE_AGENT=on`, each in a `voice-toolpick` GPU
lease. The client sent the 37 `turn_NNN_input.wav` clips in order over loopback. The first arm asked the tool question
only; the second arm also asked the place and day, which is the shipped default. Driver, scorer and records are in
`$VOICE_HOME/evidence/toolpick/`. Each replay took 8 minutes (measured, wall clock including server startup; 59 and 63
decisions at a median of 166 ms and 167 ms).

The polish round ran two more arms of the shipped default on this branch, 37 clips each, 7 minutes 36 seconds and 7
minutes 23 seconds of wall clock including server startup (measured). Driver, scorer and records are in
`$VOICE_HOME/evidence/toolpick-polish/`, which are copies of the earlier driver with this worktree's path. The first ran
the branch at `b1efc8a9df`; the second added the place-answer guard of `7c3846bf33`, which is the head.

The recording this is compared with ran without the general worker; `VOICE_AGENT` was unset. Every turn where a replay
uses the worker is therefore a change the worker's presence explains, not the tool question (inferred from the recorded
turns 025 and 028, which ran no lookup at all).

| Measurement | Tool question only | Tool question and arguments | Polish, before the guard | Polish, at the head |
| --- | --- | --- | --- | --- |
| Clips sent | 37 | 37 | 37 | 37 |
| Server turns | 37 | 37 | 37 | 37 |
| Turns with a transcript | 37 | 37 | 37 | 37 |
| Recording turns matched | 35 of 37 | 35 of 37 | 35 of 37 | 35 of 37 |
| Lookups in the recording | 7 | 7 | 7 | 7 |
| Lookups in the replay | 9 | 7 | 7 | 7 |
| Replay lookups decided by the decider | 9 | 7 | 7 | 7 |
| Replay lookups decided by the word rules | 0 | 0 | 0 | 0 |
| Turns whose tool or arguments changed | 5 | 5 | 6 | 5 |

Every value is measured, and the unmatched recording turns are the two wordless ones in every arm. Every dispatch in all
four replays carried a tool answer over the bar, so the word rules decided nothing in any call. That is the strongest
statement the runs support and also their main risk, because the rules no longer catch a confident wrong answer.

Against the previous tool-pick replay, `toolpick-args`, this branch changes one turn's tool or arguments:

| Turn | Words | Recording | `toolpick-args` | Head |
| --- | --- | --- | --- | --- |
| 016 | "Wait, do it again." | nothing | weather Durham, today, ok | weather no place, today, failed |

That is measured: `diff_replays.py` over the two record sets, comparing each turn's tool and its JSON arguments. Turn
025's agent call also differs there, but only in the conversation text it carries, because the two replays' earlier
replies differ; its tool and instruction are the same, and both ran `ok`.

The five turns of the head's replay that differ from the recording's own lookups:

| Turn | Words | Recording | Head |
| --- | --- | --- | --- |
| 003 | "Are you checking the weather?" | weather Raleigh, today | nothing |
| 016 | "Wait, do it again." | nothing | weather no place, today, failed |
| 022 | "Okay, and what about San Diego?" | nothing | weather San Diego, today |
| 023 | "You can't check the weather in San Diego?" | weather San Diego, today | nothing |
| 025 | "Pick a city on the east coast to check the weather for." | weather San Diego, today | agent, ok |

Every value is measured. Each change:

- **025, the failure this work exists for, is fixed.** The decider picked the general worker, so no San Diego forecast
  ran. Every earlier round's worker timed out on HTTP 429; the base commit moved the worker to a cheap paid model and
  this round's call answered, `ok` (measured).
- **022 is the argument questions' turn.** The word rules cannot read a place there, because "about San Diego?" has no
  preposition, so their fill reuses Portland. The decider named weather at 0.70 and read San Diego and today, so the
  forecast ran for San Diego. This is the real-audio reason the questions ship on.
- **023 needs no lookup once 022 works.** The decider called it conversation, and the recording ran a lookup there only
  because its 022 had failed (inferred from the turn order).
- **003 is d1.2's own behaviour, not the tool question.** The earlier d1.2 replay on this recording reported the same
  turn as its one changed task decision, and the 70-item eval's `about_assistant` items expect no dispatch.
- **016 is this round's change.** "Do it again" came two turns after the hello it meant, and the confident path filled
  the place from the last forecast, so a Durham forecast ran a second time over. With the guard the tool answer stands
  (weather at 0.53) and no place is carried, so the lookup runs and fails with "needs a place" (measured). The word rules
  would not route that turn at all, so a wrong forecast became a spoken failure.

The first polish run changed one turn more, 020, where the place answer was a copy of the request's own state and the
forecast failed on it; the head's answer for that turn named no place and the words filled Seattle, `ok` (measured). The
guard against the copy is a test on the recorded answer, because the model's answer varies and a replay cannot force it.

Turn 028, "Do I need to wear a jacket?", is the one expected to change and did not, in any arm. The decider named
`weather` in the earlier arms at margins of 0.60 and 0.19, which is the right tool either way, but the task never reached
a dispatch: the gate held it (measured, no execution and no dispatch log entry for that turn). The tool pick is not what
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

- Two eval items still lose a place they should carry (measured). "And what about the day after?" and "And s aturday?"
  are follow-ups by ear, and no parser sees it: the first names no day the day table knows, the second is split by the
  ASR. The guard asks the tool's parser, so both lose the earlier place and the decider's reading supplies none. A
  reading-based test would rescue them and re-open turn 016, because the model answers "today" for words that name no
  day (measured on turns 004 and 022).
- Turn 016 runs a weather lookup with no place and reports its failure. The tool answer is confident (0.53) and a
  confident answer is not overruled, so the dispatch stands even though the words give it nothing to look up. Whether
  such a turn should reach the general worker instead is a tool-choice question, not a fill one (judgment).
- t33 is a description the decider sends to the general worker at 0.87, the one confident wrong tool answer left in the
  sample (measured). t32 is the same shape at a low margin, now `agent` where the earlier sample said `weather`.
- The 70-item eval is small, and held-out is 24 items, so one item moves 4 points. The argument table is 10 items. The
  bar's value is the weakest number in this page, and the decider's own spread between samples is larger than the
  corrections the polish round fixed.
- The replies were not checked for whether they stated the right forecast. The earlier d1.2 replay found one turn
  repeating the previous turn's line, and one earlier arm repeated "I'm checking the weather in a city on the East Coast.
  Which one?" on its last three turns (measured, client transcripts; cause not traced, guess: the failed worker task's
  update).
- The voice model's own `amend_task` call still re-routes an amended sentence through the word rules. That path needs the
  tools session, which live mode does not open on the decider path (`elif not final_decider`), so it is not reachable in
  the shipped configuration (inferred from the two code paths).

## Reproduce

```
python -m voice.segmentation.live_gate_eval --decider liquid --tools --arguments --fresh \
    --decider-dir $VOICE_HOME/evidence/live-gate-eval/calls-polish \
    --traces $VOICE_HOME/evidence/live-gate-eval/traces-polish.jsonl --tool-sweep
python $VOICE_HOME/evidence/toolpick-polish/report_numbers.py $VOICE_HOME/evidence/live-gate-eval/traces-polish.jsonl
python $VOICE_HOME/evidence/toolpick-polish/run_replay.py toolpick-polish2     # inside the GPU lease
python $VOICE_HOME/evidence/toolpick-polish/score_tools.py toolpick-polish2
python $VOICE_HOME/evidence/toolpick-polish/diff_replays.py \
    $VOICE_HOME/evidence/toolpick/toolpick-args/server $VOICE_HOME/evidence/toolpick-polish/toolpick-polish2/server
python $VOICE_HOME/evidence/toolpick/args_probe.py
python $VOICE_HOME/evidence/toolpick/cost_probe.py
```

`--arguments` adds the place and day questions to the eval, for the argument comparison; the polish round's run used it,
since that is the shipped body. `report_numbers.py` prints the tool and argument tables for one trace file and scores the
same answers with the fill the guard replaced, which is how the 9-of-10 beside 7-of-10 above is separated from the
model's spread. `VOICE_DECIDER_ARGS=off` turns the questions off in a live run, which is how the first replay arm above
was run.
