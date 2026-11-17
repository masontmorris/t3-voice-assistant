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

`python -m voice.research.segmentation.live_gate_eval --decider liquid --tools --arguments --fresh` ran the 70 items three times
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
"DEN ver" and "DEN" are neither of them the city. The round after this one takes both columns to 9 of 10 without
giving either item back ([Follow-ups](#follow-ups)).

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

## Follow-ups

A follow-up runs in both directions in a call. The person's follow-up that leans on the previous lookup continues it,
and the assistant's own question, asked because a confident tool choice had nothing to look up, is answered and
completed. Measured on 2026-11-16, branch `voice/followups` from `0f28b27bec`. The summarizer is Liquid's hosted `d1.2`
for the decisions and LFM3 for the words, as before.

### The person's follow-up

The polish round's guard asked the tool's own parser whether the words continue the earlier call, so a follow-up that
states a day or a place of its own keeps the earlier place. The parser could not read two of Mason's follow-ups. "And
what about the day after?" names a day the day table did not hold, and "And s aturday?" is one word the ASR split in
two, so both stated nothing and the guard dropped the earlier place. The decider's reading supplied no place for either
(measured, three readings each, `weather` at 0.83 to 0.87). Its day for `t52` was `tomorrow` where the words mean the
day after tomorrow (measured, 0.07 to 0.12 on a `choice` answer, and it agrees with itself across the three readings).

The root cause was the day table, not the guard. A parser that cannot read a day cannot tell a day-only follow-up from
"Wait, do it again.", so two real follow-ups were lost for want of one word each. `voice/tools/lookups.py` now reads
four things more:

- `day after tomorrow`, with `pick_days` taking the place's third day;
- `the day after` and `the next day`, which `resolved` counts from the earlier lookup's own day: after a Saturday it is
  Sunday, and after a tomorrow it is the day after tomorrow;
- a day the ASR split in two, rejoined when the join is a day the table knows, so "s aturday" and "to morrow" read as
  the days they are;
- `answer_place`, the place a person gives when asked for one, which the next subsection uses.

The second half of the fix is what a reading may supply. The decider's place and day are now kept only where the
segment's own words say them (`lookups.supported`), and dropped otherwise. That is one rule with three measured
cases. `t52` keeps the words' day over the model's `tomorrow`. Turn 016's "Wait, do it again." cannot take a place
from the earlier Durham forecast even when the model reads one, because the words name no place. And turn 022's
"Okay, and what about San Diego?" keeps its place, because those words do say it, which the preposition rule alone
cannot read (measured, turn 022 of the replay).

| Split | n | Arguments exact, polish round | Arguments exact, now |
| --- | --- | --- | --- |
| The polish round's recorded answers, scored again | 10 | 7 | 9 |
| A fresh sample, three readings per item | 10 | not run | 9 |

Both columns are measured. The first row scores the same 210 traces the polish round recorded, so the change is the
code and not the model's spread. The second is a fresh 210-call sample at $0.00323. `t64` is the one item wrong in both
rows: "DEN" and "DEN ver" are neither of them the city (measured). `t52`'s day is now `day after tomorrow`, and the
eval's label for that item compares the place alone (measured from the label file).

The word rules alone rose with the same answers, from 34 of 46 to 36 of 46 on train, and stayed at 15 of 24 held out
(measured). That is `t52` and `t67`, whose place the rules now carry with no decider answer at all.

Tool choice did not move, as nothing in this round forms a tool answer. The polish round's recorded answers score 45 of
46 on train and 22 of 24 held out when scored again with this branch's code; the fresh sample scores 44 of 46 and 22 of
24 (measured). The one item that moved is `t43`, "Send that to Sam.", which the fresh sample called `none` at 0.48 where
the polish sample called it `agent` at 0.43 (measured); the task is held for a clarifying question in both samples, so
neither answer runs anything. `t32`, `t33` and `t51` are wrong in both samples (measured). `t51`'s `none` at 0.54 clears
the bar and its task dispatches with nothing to run, exactly as the polish sample's `none` at 0.46 did (measured from the
two traces).

### The assistant's own question

A confident tool choice can have nothing to look up. Turn 016's weather call at 0.53 had no place, ran, and reported a
spoken failure, "needs a place" (measured, the polish round's replay). The task machinery already has the state for
this: `needs_clarification`, which `Readiness.CLARIFY` sets and which the snapshot reports. What it lacked was the
argument, so `Registry.waiting` now derives it from the tool's own `needs` field rather than from a list of tools:
a call that leaves a required argument unset returns the tool, the arguments it would run with, and the fields it
lacks. The value comes from the same `filled` the dispatch would use, so an earlier lookup's place or a
`VOICE_DEFAULT_PLACE` counts as a value and no question is asked.

The orchestrator's `_settle` consults that check before the gate and before readiness, so a task with nothing to look up
waits instead of running, whatever the model said about readiness. The task keeps the call it would make, and the
snapshot names what it lacks. `task_note` then asks for it in the voice model's own words, the way the other notes
work, with no fixed line anywhere:

> [Note for you, not said by the person: the weather lookup has not started because they have not said which city or
> place they mean. Ask them in one short sentence, and don't guess one.]

On the replay's turn 016 the model wrote "Sure! Which city or place would you like the weather for?" and nothing ran
(measured, that turn's record).

The answer turn completes that task rather than starting a new one. `carried_tasks` carries a waiting task across the
turn, `MockTaskService.answer` asks the registry whether these words are the answer, and the answered task runs the
call with the value they gave. The decider is asked nothing on that turn, because the turn answers the assistant's own
question and is not a new decision. Two conditions keep a request from being swallowed: the words must name no other
tool's subject, so "What's the time in Tokyo?" runs a time lookup, and they must state a value for every field the task
lacked, so "Never mind." answers nothing and leaves the question open.

### Should "Wait, do it again." be a question back?

Yes. That is a judgment, and this round's mechanism makes it the behaviour with no special case. The measured case for
the alternative is the polish round's own: with the place carried the turn re-ran the Durham forecast two turns after
the hello the person meant, and with the guard it ran a lookup that could only fail. A guess at the referent is
therefore wrong more often than not, and a failure sentence is not an answer to the person. Asking costs one turn and
cannot be wrong: the tool answer stands (weather at 0.53), the words supply no place, and the person either names the
city for the forecast they want or moves on, in which case the question is dropped with the task. Both outcomes are
better than a forecast nobody asked for and than a spoken failure (judgment on the two measured alternatives).

What this round does not do: turn 016 still picks the weather tool, so the question it asks is about a city. Reading
"do it again" as a repeat of the previous task would need the referent, which is exactly what is missing. The
clarification is the honest answer to a request the words do not fill in.

### What the replay changed

One 37-clip replay of Mason's recording on this branch, `--decider liquid`, real tools, `VOICE_AGENT=on`, in a
`voice-followups` GPU lease, 7 minutes 35 seconds of wall clock including server startup (measured, under the build
lock). The driver, the scorer, the reply differ and the records are in `$VOICE_HOME/evidence/toolpick-followups/`.
Against the previous worker's `toolpick-polish2`:

| Measurement | `toolpick-polish2` | This branch |
| --- | --- | --- |
| Clips sent | 37 | 37 |
| Server turns | 37 | 37 |
| Turns with a transcript | 37 | 37 |
| Recording turns matched | 35 of 37 | 35 of 37 |
| Lookups in the recording | 7 | 7 |
| Lookups in the replay | 7 | 6 |
| Replay lookups decided by the decider | 7 | 6 |
| Turns whose tool or lookup arguments differ from the recording | 5 | 4 |

Every value is measured. The replay's lookups fall by one because turn 016 no longer runs one, and that turn now matches
the recording, which ran nothing there either.

Turn 016 is the only turn whose tool or lookup arguments differ between the two replays. Its arguments in the tool
answer are unchanged; what changed is that nothing runs and the task waits for a city. Turn 025's agent call differs
only in the conversation text it carries, as in the previous round, because the call's earlier replies differ (measured
with `diff_replays.py`). Spoken replies differ in wording on 33 of 37 turns, which is the voice model's own variation
between two runs of the same code as much as anything else, so only two turns' replies changed in kind (measured, text
compared turn by turn):

| Turn | Words | `toolpick-polish2` | This branch |
| --- | --- | --- | --- |
| 016 | "Wait, do it again." | failing weather lookup, empty reply | nothing runs, "Sure! Which city or place would you like the weather for?" |
| 025 | "Pick a city on the east coast to check the weather for." | "I'm checking the weather in a city on the East Coast." then New York | "Sure! Which city on the East Coast would you like the weather for?" then New York |

Turn 025 is a cost of the change (inferred): its task and instruction are the same, so the first line is the voice
model's own wording, and the earlier turn's question is now in the call's history. Asking a city there and then
answering it is harmless but not useful, and one run cannot say whether the history or the model's spread caused it.
Turns 023 and 026 also read differently and neither is this round's (inferred: 023 runs nothing in both replays, and
026's city came from the general worker's own choice, New York before and Boston now).

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
- **016 is the polish round's change.** "Do it again" came two turns after the hello it meant, and the confident path
  filled the place from the last forecast, so a Durham forecast ran a second time over. With the guard the tool answer
  stands (weather at 0.53) and no place is carried, so the lookup runs and fails with "needs a place" (measured). The
  word rules would not route that turn at all, so a wrong forecast became a spoken failure. The follow-ups round turns
  that failure into a question about the city and runs nothing there ([Follow-ups](#follow-ups)).

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

- The two follow-ups the polish round lost are fixed on the recorded answers and on a fresh sample, so the argument
  table stands at 9 of 10. The item left is `t64`, "What's the whether in DEN ver?", where the words split the city and
  neither the rules nor the model joins it back (measured). A place-joining rule like the day table's would need a
  gazetteer or a second lookup to know that "DEN ver" is Denver (guess).
- The assistant asks for a city whenever a confident tool answer has no place, and turn 016 is the case where its tool
  answer is itself the guess. It now asks rather than failing, and the question it asks is about a city for a request
  that may not be a forecast at all (judgment, see [Should "Wait, do it again." be a question
  back](#should-wait-do-it-again-be-a-question-back)).
- Turn 025's first line became a question after the earlier turn's question entered the call's history (measured turn,
  inferred cause, see [What the replay changed](#what-the-replay-changed)). One run cannot separate the history's pull
  from the voice model's own spread.
- A waiting question is asked once. If the person's next turn asks something else, that task is dropped with the turn,
  so the city is never asked for again. Carrying it forward until it is answered would need the waiting entry to be
  re-carried on a turn that did not answer it (inferred from the code path).
- The clarification is the decider path only. A task that reaches the tools session through a voice delegate has no
  confident tool choice to read `needs` from, so it still runs and fails as before (inferred; measured on the
  `test_with_no_earlier_place_a_follow_up_stays_empty` case of the prototype suite).
- t33 is a description the decider sends to the general worker at 0.87 in both samples, the one confident wrong tool
  answer left (measured). t32 is the same shape at a low margin, `agent` at 0.39 in both samples.
- The 70-item eval is small, and held-out is 24 items, so one item moves 4 points. The argument table is 10 items. The
  bar's value is the weakest number in this page, and the decider's own spread between samples is larger than the
  corrections the polish round fixed. This round left the bar alone.
- The replies were not checked for whether they stated the right forecast. The earlier d1.2 replay found one turn
  repeating the previous turn's line, and one earlier arm repeated "I'm checking the weather in a city on the East Coast.
  Which one?" on its last three turns (measured, client transcripts; cause not traced, guess: the failed worker task's
  update).
- The voice model's own `amend_task` call still re-routes an amended sentence through the word rules. That path needs the
  tools session, which live mode does not open on the decider path (`elif not final_decider`), so it is not reachable in
  the shipped configuration (inferred from the two code paths).

## Reproduce

```
python -m voice.research.segmentation.live_gate_eval --decider liquid --tools --arguments --fresh \
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

The follow-ups round's own numbers, from branch `voice/followups`:

```
python -m voice.research.segmentation.live_gate_eval --decider liquid --tools --arguments --fresh \
    --decider-dir $VOICE_HOME/evidence/live-gate-eval/calls-followups \
    --traces $VOICE_HOME/evidence/live-gate-eval/traces-followups.jsonl --tool-sweep
python -m voice.research.segmentation.live_gate_eval --decider liquid --tools --arguments --score-only \
    --traces $VOICE_HOME/evidence/live-gate-eval/traces-polish.jsonl
python $VOICE_HOME/evidence/toolpick-followups/run_replay.py toolpick-followups     # inside the GPU lease
python $VOICE_HOME/evidence/toolpick-followups/score_tools.py toolpick-followups
python $VOICE_HOME/evidence/toolpick-followups/diff_replays.py \
    $VOICE_HOME/evidence/toolpick-polish/toolpick-polish2/server $VOICE_HOME/evidence/toolpick-followups/toolpick-followups/server
python $VOICE_HOME/evidence/toolpick-followups/diff_run.py \
    $VOICE_HOME/evidence/toolpick-polish/toolpick-polish2/server $VOICE_HOME/evidence/toolpick-followups/toolpick-followups/server
python $VOICE_HOME/evidence/toolpick-followups/count_changes.py \
    $VOICE_HOME/evidence/toolpick-polish/toolpick-polish2/server $VOICE_HOME/evidence/toolpick-followups/toolpick-followups/server
```

`--arguments` adds the place and day questions to the eval, for the argument comparison; the polish round's run used it,
since that is the shipped body. `report_numbers.py` prints the tool and argument tables for one trace file and scores the
same answers with the fill the guard replaced, which is how the 9-of-10 beside 7-of-10 above is separated from the
model's spread. `VOICE_DECIDER_ARGS=off` turns the questions off in a live run, which is how the first replay arm above
was run.
