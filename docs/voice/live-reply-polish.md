# The reply path: the chosen city, and the line it said twice

**Superseded 2026-11-18.** The retries, the last ask, the chosen-place memory and the repeat screen below were removed when the fact check became a log ([live-fact-check.md](live-fact-check.md)). The measurements stay as the record of why they existed.

Written 2026-11-16 on branch `voice/polish-reply` from `53ff4ff5a5`. Two defects sat on the default live path. A turn
that lost both replies went silent, and a turn could say what the previous turn already said. Every number below is
measured from the records named beside it, except where it says inferred or guess.

## What was wrong

### 1. The retry repeated what the screen refused, and the turn went silent

The fact screen's sources are the person's words in this call and the newest machine note. The assistant's own words
are deliberately not a source, so a value it said once cannot become allowed. That rule is right for a guessed value
and wrong for a place the person asked it to choose.

Measured on the 2026-11-16 call's replays:

| Run | Turn | Person | First reply | Screen | Second reply | Screen |
| --- | --- | --- | --- | --- | --- | --- |
| `callfix3/replay/fix3` | 25 | "Pick a city on the east coast to check the weather for." | "Let's check the weather in Boston, Massachusetts." | `place Boston`, `place Massachusetts` | "I'm checking the weather in Boston." | `place Boston` |
| `callfix3/replay/fix3` | 27 | "Whoa, why do you sound different?" | "I'm checking the weather in Boston." | `place Boston` | same | `place Boston`, blocked |
| `liquid/real-audio/liquid` | 28 | "Do I need to wear a jacket?" | "The weather in Boston is not mentioned ..." | `place Boston` | "I can't look up the weather in Boston ..." | `place Boston`, blocked |
| `polish/before-nw` | 25 | "Pick a city on the east coast to check the weather for." | "I can't check the weather for a city on the East Coast because the lookup couldn't be completed." | `lookup` | same | `lookup`, blocked |

Turns 27 and 28 of the callfix3 and liquid replays said nothing at all (measured). Nothing in the call could ever name
Boston, because the person never said it and the assistant's spoken words are not a source, so the retry note could
only tell the model to drop the one city the turn was about.

The same silence has a second shape, a word the screen forbids rather than a fact. In `polish/before-nw` turn 25 the
model said "lookup", which the screen refuses as machinery, and the retry said it again. In both shapes the retry has
no lever, because it is the same line in the same context.

### 2. The model continued its own last line

A turn with no lookup gets no note, so the append-only voice session has the person's audio and then the reply. The
nearest text the model reads on the next turn is its own last reply, and llama-server runs `--temp 0`, so it can
continue itself word for word.

| Run | Turns | Line said again |
| --- | --- | --- |
| `toolpick/toolpick-real` | 25, 26, then 28 to 37 | "Hello! I'm checking the weather in a city on the East Coast. Which one?" |
| `polish/before` (this replay) | 17 to 19, 23, 24, 27, 28, 30 to 37 | its checking line, then "Hello! I'm checking the weather in New York City. The result is a high of 51 degrees ..." |

Fifteen of the baseline replay's 37 turns repeated an earlier line (measured). The second tool-pick arm repeated a
different line on turns 28 and 29 only, so the degree varies with the line's content, not with the branch (measured).

## The change

All in `voice/live/facts.py`, `voice/prototype/run.py` and `voice/live/ledger.py`.

- `asks_to_choose` reads a request that hands the choice to the assistant ("Pick a city ...", "Pick for me", "you
  pick", "surprise me"). When the person's words are one of those, a reply whose only ungrounded value is the place
  it chose is spoken, and the place goes on the turn record as `established`. It cannot make the model choose; it
  decides whether the city it chose may be said.
- The ledger carries `established` into every later turn's sources, so "I'm checking the weather in Boston" is
  grounded on turns 27 and 28. Nothing else the assistant said becomes a source.
- The opening cut of a refused reply must be at least three words. The baseline spoke "Hello!" alone on turn 25
  (measured).
- `repeated` refuses a reply that opens with the line just said, or that repeats an earlier line exactly.
  `REPEAT_NOTE` says the model already said that. A retry that fails again gets one last ask with
  `NEW_WORDS_NOTE` after a repeat, or `FINAL_NOTE` otherwise, both of which take the values away so the model has to
  answer the person's turn. A reply that is stale through all three asks is spoken as it is, because silence after
  the person speaks is worse. No fixed line is played and the model writes every word.
- The last ask runs in note mode only. The prototype's older snapshot modes keep their two attempts and their fixed
  fallback line, so their records still compare with earlier runs.

## Real audio

Four replays of Mason's 37 recorded turns, one server each, `--decider liquid`, REAL weather and time tools,
`$VOICE_HOME/evidence/polish/`. The pairs differ only in the branch. `before` is `53ff4ff5a5` and `after3` is this
branch. Driver `run_replay.py`, scorer `score.py` (the turn-loss scorer), audit `audit.py`.

The recorded call ran with no general worker, so the first pair replays its own configuration (`VOICE_AGENT=off`).
This is the pair that reproduces the silence.

| Measurement | `before-nw` | `after-nw` |
| --- | --- | --- |
| Turns with no reply at all | 1, turn 25 | 0 |
| Refused replies | 9 | 7 |
| Refusals that ended blocked | 1 | 0 |
| Replies that repeat an earlier line | 5 | 4 |
| Turn transcripts matching their clip | 37 of 37 | 36 of 37 |

Turn 25 before said nothing at all. Both of its replies were refused for naming the lookup ("I can't check the
weather for a city on the East Coast because the lookup couldn't be completed."), and the retry repeated it. After,
the same turn says "I can't check the weather for a city on the East Coast." It still does not pick a city, because
this configuration has no worker to pick one; the pick rule below covers the turn only when the model chooses a place
(measured, recorded `callfix3/replay/fix3` turn 25). The one transcript mismatch is turn 24, where the ASR dropped the
word "you"; it is not a reply-path turn (measured, `after-nw/score.json`).

The second pair has the general worker on, as the tool-pick replays ran it. It is where the repetition was worst.

| Measurement | `before` | `after3` |
| --- | --- | --- |
| Turns with no reply at all | 0 | 0 |
| Refused replies | 2 | 11 |
| Refusals that ended blocked | 0 | 0 |
| Replies that repeat an earlier line | 15 | 4 |
| Turn transcripts matching their clip | 37 of 37 | 37 of 37 |
| Turns that ended with no transcript | 0 | 0 |

In the baseline, the screen's refusal of the chosen city cost turn 25 all but the word "Hello!", because the opening
cut kept the greeting. With the fix that turn says it is checking and then the real Boston forecast (measured).

The 4 repeats after the fix, by turn (measured):

| Turn | Person | Line | Stale? |
| --- | --- | --- | --- |
| 17 | "Say hello to me as fast as possible." | "Hello!" | no, the person asked for it |
| 18 | "The word hello." | "Hello!" | no, the person asked for it |
| 26 | "I wanted a city on the east coast." | "I'm checking the weather in a city on the East Coast. It might take a moment ..." | yes, said on 25 |
| 29 | "Answer the question." | "I don't have information about the weather in Boston today, so I can't say if you need a jacket." | yes, said on 28, and stale through all three asks |

Turn 29 is the cost of never going silent. The third ask was told to name no weather, and the model named it anyway,
so the repeated refusal was spoken rather than dropped (measured). The no-worker pair's one genuinely stale turn is
the same shape, its turn 29 (measured).

## Every refusal in the agent-on after replay

| Turn | Refusals | Reason | Outcome |
| --- | --- | --- | --- |
| 2 | 1 | `date February 22`, `number 22`, `number 1732` | asked again, then spoken |
| 16 | 4 | `number 57`, then repeated | both replies asked again, then spoken |
| 26 | 4 | repeated | both replies asked again, then spoken, and the update repeated the snapshot |
| 29 | 2 | repeated | asked again twice, then spoken |

In the no-worker after replay, the refusals are turn 16 (numbers, then repeated) and turns 23 and 29 (repeated), and
none ended blocked (measured, `after-nw/audit.json`). No refusal in either after replay named the machinery or spoke
of the person in the third person (measured).

## Tests

| Suite | Result |
| --- | --- |
| `voice/prototype/tests` | 137 pass, up from 127. New: the chosen place is spoken and carried; a place with no pick request is still refused; a chosen place grounds the next turn; a line already said is asked again; a stale retry gets the last ask; a reply stale through every ask is spoken; a one-word opening cut is refused; the last ask gives a would-be silent turn one line. `test_a_reply_refused_twice_is_asked_one_last_time_and_a_third_refusal_is_silent` pins the remaining floor. |
| `voice/live/tests` | 165 tests, 161 pass, up from 162 tests and 158 passing on the base. The 4 errors are `test_client_recording`, which needs `sounddevice`; that package is in the client venv, not the server venv, and the same 4 failed on the base. New: `asks_to_choose` on six phrased requests and four ordinary ones; the ledger carries `established` and every spoken reply. |
| `voice/segmentation/tests` | 105 pass, the same count as the base. |
| `voice/tools/tests` | 51 pass, unchanged. |

The new tests were committed first (`dc7e27f7cd`) and run against the code before the fix. They failed, because the
pick turn spoke the retry's "I'm still on it." instead of the chosen city.

## What this cannot see

- Whether a place is the right city. The pick rule only decides whether the assistant may say the city it chose; it
  cannot tell Boston from Baltimore, and it does nothing when the model asks which city instead of choosing one. The
  no-worker replay's turn 25 is that case (measured).
- A repeat that rewords the earlier line. The screen matches word for word, or the line just said as a prefix; the same
  forecast with fewer numbers in it is not caught.
- Whether the person wanted the repeat. "Say that again" reaches the same screen, costs one extra generation, and the
  third ask is what the person hears if the model will not repeat itself.
- Anything the model says in audio that its text deltas do not carry.

## Reproduce

```
python $VOICE_HOME/evidence/polish/run_replay.py before-nw --worktree <checkout at 53ff4ff5a5> --agent off
python $VOICE_HOME/evidence/polish/run_replay.py after-nw --worktree <this branch> --agent off
python $VOICE_HOME/evidence/polish/run_replay.py before --worktree <checkout at 53ff4ff5a5>
python $VOICE_HOME/evidence/polish/run_replay.py after3 --worktree <this branch>
python $VOICE_HOME/evidence/polish/audit.py before-nw after-nw before after3
python $VOICE_HOME/evidence/polish/score.py before-nw after-nw before after3
python -m unittest discover -s voice/prototype/tests -t .
python -m unittest discover -s voice/live/tests -t .
```
