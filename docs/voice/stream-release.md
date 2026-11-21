# Releasing reply audio while it is generated

**Superseded 2026-11-18.** The screening gate described here (`voice/prototype/release.py`) is gone: the fact check became a log, so each piece of reply audio now goes to the speaker the moment the model makes it, with no block of lookahead ([live-fact-check.md](live-fact-check.md)). What stays true below is the measured block structure of LFM3-Audio replies and the cost of holding a whole reply.

Written 2026-11-16. Every number carries a label: **measured** (from a record or a run on this machine), **inferred**
(arithmetic or code reading on measured numbers) or **guess**.

## Why

Mason wants the assistant to answer fast when he stops talking. Until this change, a live turn held every reply until
the model had generated all of it and the fact screens had passed it, then sent the whole reply's audio at once
(commit d6032b3df2, "Reply audio from the voice session no longer goes straight to the client"). That hold's cost was
measured only on short replies. `latency-research.md` later measured it as the largest wait on current replies: 0.14 s
for every second of reply speech, 1.03 to 1.18 s at the median, and ranked releasing the reply progressively as
change 1.

This change drops the hold. Each block of reply audio goes out once the reply's text up to the block after it has
passed every screen. The invariant stays: audio is never sent for text that has not passed. The screens
(`voice/live/facts.py` and `refused` in `voice/prototype/run.py`) are unchanged; only when they run and what they gate
changed.

**Result.** On Mason's 37 recorded turns, replayed with the liquid decider and real tools, speech end to first audio
sent fell from a median of 1.84 s to 1.29 s and from a p90 of 2.59 s to 1.54 s (measured, one run each, before at the
integration head 90ac28d6f2 and after at this branch's ae525736d7). The audit re-screened all 141 releases of the
final run against the sources each was screened with, rebuilt each response's block map from the recorded events,
and found no release whose text failed or whose audio ran past what the text allowed (measured). The client logged
no underrun in either run (measured). Two replies stopped part way, both on values nothing in the call gave. Turn 2 is
a clean partial. Turn 20 left the person a one-block fragment, "The weather", which one more block of lookahead would
have held back at a cost of 0.16 s on every turn (measured in the simulator; it is Mason's call, below).

## Design

### 1. How a released block maps to screened text

What llama.cpp gives: a response is a sequence of `response.output_text.delta` and `response.output_audio.delta`
events, with no alignment between them. In text-and-audio mode LFM3-Audio emits them in blocks: a run of text deltas,
then that block's audio frames, then the next run (measured on polish/before turn 2 and every reply the simulator
read: 6 text tokens, then 12 frames of 80 ms; the first frame is 60 ms). The model writes text faster than it speaks
it, about 6 tokens per 0.96 s of audio, so the text leads the audio and the lead grows through the reply (inferred
from the block shape; measured below).

The gate (`voice/prototype/release.py`, `Release`) records, for each text run, the text length and the audio length
when the run ended. When run m is complete (the first audio frame after it arrives), it screens the text so far cut
back to its last whole word. If the screens pass, it releases the audio of every block j whose own run is inside the
screened text and whose next run is complete. With one block of lookahead (`LOOKAHEAD_BLOCKS = 1`), block j goes out
only after the whole words of run j + 1 have passed with it. At `response.done` the whole text is screened as a
finished reply, uncut, and either the rest is released or nothing more is.

Choices inside that rule, and why:

- **Whole words only.** A run often ends mid-word ("in R", "ale", "igh"). Screened as it stands, "Ral" would read as
  a place nothing gave. The cut keeps the screen from failing on a half word, and the lookahead covers the word that
  is still arriving.
- **A repeat holds until it is clear.** The reply polish refuses a reply that says again a line the call already
  said (`repeated`), and that needs the reply's whole opening. Mid-reply, `may_repeat` holds the release while the
  words so far are still the start of such a line of 8 words or more, or already open with the line just said. A
  stale reply therefore releases nothing, and its retry gets `REPEAT_NOTE` as before. A reply that only opens like an
  earlier line streams from the word where it parts from it.
- **A chosen place passes.** With `asked_to_choose` (from `may_choose`), a place-only objection passes the gate, and
  the places go on `established`, as the reply polish does for a whole reply.
- **A prefix that fails holds, it does not stop.** "in New" can fail where "in New York" passes. A failing prefix
  releases nothing more, and a later prefix that passes may release again. Every release is justified by the text
  that passed when it went out, so the invariant holds (inferred from the rule; the audit below checks it on every
  release of the replay).
- **No pause cut.** I first cut each release back to the last quiet window, so a stopped reply would stop between
  words. On the recorded replies it delayed the first release by a median of 0.16 s and up to 0.4 s at p90 (measured
  in the simulator, 0.32 s against 0.16 s after the first frame). A looser window matched the no-cut timing but would
  also cut inside words on quiet consonants (inferred). It came out. A reply that stops mid-way stops on an 80 ms
  frame edge, which the listening step must judge.

**Measured alignment.** `align.py` sent 24 replies' own audio, cut at every block boundary, through the LFM3 ASR and
matched each transcript back to the reply's words (81 boundaries, replays polish/before and polish/after2):

| What                                                                           | Result                                                            |
| ------------------------------------------------------------------------------ | ----------------------------------------------------------------- |
| Words the audio of blocks 0..k said beyond the whole words of text runs 0..k+1 | 0 at every boundary (maximum 0)                                   |
| Words the text of runs 0..k leads the audio of blocks 0..k                     | median 7, p10 3, maximum 16                                       |
| Boundaries where the audio reached past run k itself                           | 1 of 81 (turn 25, block 1, by 5 words, still inside run k+1)      |
| First blocks (0.94 s) the ASR read as empty                                    | 16 of 24; 4 of 24 with 1 s of silence after them (`pad_check.py`) |

So one block of lookahead held on this sample: no released audio said a word the screens had not passed (measured,
24 replies, one speaker's call). The ASR's own delay inflates the lag a little, since it reads nothing from most
0.94 s clips (inferred from the last row).

The lag decides what a stopped reply was heard to say. The text the released blocks were released on overstates it
by about 7 words, so quoting that text would tell the model the person heard words they never did. At a stop the turn
therefore transcribes the audio it released, with 1 s of silence after it, and quotes the start of the reply's own
text up to the last word the transcript matched (`heard_prefix`, `by_ear` in `run.py`). The quote is never longer
than the released text, and when the transcript reads nothing the note quotes the released text instead, so a reply
the person heard any of is continued, never restarted. Measured on the final run: turn 2's transcript ended at
"Fahrenheit" where the released text ran ten words further, into "George Washington was born on".

### 2. A failure mid-reply

Mason's default, implemented. A reply whose finished text fails a screen stops at the last block already released.
The turn records `fact_check` with what was heard and asks again once:

- If anything was heard, the note quotes it: "they heard only the start of your last reply, "...", and not the rest,
  because nothing gave you Sunday. Go on from there in one short sentence, ... without saying again what they heard."
  (`continue_note`). The continuation goes through its own gate with one more screen, `restated`: a continuation that
  opens with the first four words already heard is refused, and while its words so far could still turn into that
  opening it is held.
- If nothing was heard, the re-ask is the one the turn already made: the retry note, the escalation to a lookup, or
  the JSON fact-check item in the older modes.
- A second failure stops the continuation where its own gate stopped it. A turn that has still said nothing gets the
  reply polish's third ask (`last_ask`, `NEW_WORDS_NOTE` after a repeat and `FINAL_NOTE` otherwise), whose gate does
  not hold for repeats. A reply already partly heard gets no third ask: the person is not in silence, and Mason's
  rule is one re-ask. A turn that has said nothing after all of that speaks `FALLBACK_LINE` in the older modes; note
  mode stays silent.
- The reply polish's three-word minimum for a kept opening went with `spoken_part`. A heard opening is always followed
  by a continuation ask now, so "Hello!" alone is the outcome only if the continuation fails too.

Both responses go into the call's one voice session, as every response did before. Nothing is rebuilt from text, no
session is opened per turn, and the stopped response is not cancelled: llama.cpp keeps generating it to the end, and
the turn waits for `response.done` before it asks again, because the session takes one response at a time. The
already-released audio plays meanwhile.

`spoken_part` and `cut_at_pause`, which estimated the sentence ends of a held reply from its share of the characters,
and the `keep_opening` flag that chose them, are gone. A stopped stream is the partial reply now.

### 3. Interplay

- **Wire format.** `speak(text, pcm, done)` replaces `speak(text, pcm)`. The server sends one `reply_start` before the
  first released piece, each piece in 40 ms frames, and one `reply_end` when the reply is done, across the first
  response and its re-ask. So the client's `Player` sees one reply, and its underrun counter (an output callback that
  finds the buffer short before `reply_end`) measures any gap between released blocks. `client.py` is unchanged.
- **Playback clock and the overlap gate.** `Playback.add` is called once per released piece with that piece's
  duration and the words it added, so `until` extends as the stream arrives and `reply_audible` stays true while
  anything released is still playing. The echo check reads each word once.
- **Barge-in.** A turn the person talks over is muted as before. Its later pieces send no audio, the send loop drops
  frames already queued, and `reply_end` still goes out so the client is not left waiting. The turn keeps running to
  the end of its response, so the session's history stays whole. A turn starts counting as having spoken
  (`reply_started`) at its first released piece, which is earlier than before: speech after that point is an overlap,
  not a continuation of the turn.
- **Task update and the question for a missing place.** The update reply and the follow-ups' clarification question
  go through the same `answer`, so they stream too, and their first audio goes out as soon as their first block passes.
- **Retry note flow.** Unchanged when nothing was heard. When something was, the continuation note replaces it, as
  above.

## Measured before and after

Speech end to first audio sent, `live.speech_end_to_first_audio_sent_s` in each server record, median / p90 in
seconds, split by the length of the turn's first reply as sent. Reply length is sampled, so the counts per band
differ between runs (measured).

| Reply length                               | Before, 90ac28d6f2 (held reply) | After, ae525736d7 (streamed) |
| ------------------------------------------ | ------------------------------- | ---------------------------- |
| short, under 4 s                           | 1.69 / 3.13, n=11               | 1.28 / 1.49, n=18            |
| medium, 4 to 8 s                           | 1.84 / 2.26, n=24               | 1.25 / 1.49, n=16            |
| long, 8 s or more                          | 2.35 / 2.40, n=2                | 1.44 / 1.53, n=3             |
| **all turns**                              | **1.84 / 2.59, n=37**           | **1.29 / 1.54, n=37**        |
| client, speech end to first audio received | 1.85 / 2.60                     | 1.29 / 1.54                  |
| underruns (client `reply_end` counts)      | 0 in 43 replies                 | 0 in 43 replies              |
| releases audited / violations              | none, held                      | 141 / 0                      |
| responses stopped after releasing audio    | none, held                      | 2                            |
| turns with no reply heard                  | 0                               | 0                            |

Every turn that did not stop part way released its first block a median of 0.155 s after the model's first audio
frame, at most 0.204 s (measured, `lookahead.py final`). The rest of the gain is the reply's remaining generation,
which now overlaps playback. The simulator had projected 1.20 s from the older polish records (inferred), and the
measured 1.29 s is inside the 0.30 s run-to-run spread the research measured for identical replays.

Mid-reply stops in the final run (measured, `audit.json`):

| Turn | Heard                                                             | Why it stopped                                                                    | What followed                                                                                                                         |
| ---- | ----------------------------------------------------------------- | --------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------- |
| 2    | "The weather in Raleigh today is a high of 57 degrees Fahrenheit" | `date February 22`, `number 1732` (the Washington birthday, which nothing gave)   | the continuation said the same fact and was held, so the turn ended on the forecast                                                   |
| 20   | "The weather" (0.94 s)                                            | `number 57`, `number 0`: a forecast invented while the Seattle lookup was running | the continuation restarted "The weather in Seattle..." and the `restated` screen held it; the task update then said the real forecast |

An intermediate run at eae21082e2, before the by-ear padding, stopped six responses on turns 20 to 22 the same way.
There the ASR read the one-block fragment as empty, the turn took that as nothing heard, and the retry went out from
the top, so the person heard two openings. That run's latency was 1.22 / 2.44 s (measured). The padding and the
fallback in ae525736d7 are its fix, and `test_a_fragment_the_ear_cannot_read_is_still_continued_not_restarted` pins
it.

**More lookahead, measured in the simulator on both streamed runs with each response's own logged screens.** With two
blocks of lookahead, the first release moves from a median of 0.155 s after the first frame to 0.312 s. The responses
that stop after releasing audio fall from 7 to 1 in the intermediate run and from 2 to 1 in the final run. Only turn
2's clean partial is left. The brief fixes one block, so the branch keeps one block; `LOOKAHEAD_BLOCKS` is the one
constant to change.

## Tests

| Behaviour                                                                          | Test                                                                                                                                                                                           | Fails without it                                                                    |
| ---------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------- |
| No audio for text that has not passed                                              | `test_stream_release.ReleaseGateTest.test_no_audio_for_text_that_has_not_passed`, `test_a_word_still_arriving_is_not_screened`, `test_a_prefix_that_fails_and_then_passes_releases_again`      | yes: a gate that ignores the screens fails 6 tests                                  |
| Audio goes out while the reply is generated                                        | `test_audio_reaches_the_speaker_before_the_reply_is_done`, `test_a_block_is_released_once_the_next_runs_text_has_passed`                                                                       | yes: a gate that holds until done fails 8 tests                                     |
| A late failure stops at the last passing block and re-asks once, without repeating | `test_a_late_failure_stops_at_the_last_boundary_that_passed_and_goes_on_without_repeating`, `test_a_continuation_that_says_the_heard_part_again_is_not_spoken`                                 | yes: a from-the-top re-ask, and a missing `restated` screen, each fail one          |
| The note quotes what the audio said                                                | `test_the_continuation_quotes_what_the_released_audio_said_not_what_it_was_released_on`, `test_a_fragment_the_ear_cannot_read_is_still_continued_not_restarted`                                | yes (checked by reverting run.py)                                                   |
| Barge-in during a streamed reply                                                   | `test_server.LiveServerTest.test_a_barge_in_during_a_streamed_reply_stops_its_audio_and_ends_it_once`, `SpeakTest.test_a_turn_muted_mid_reply_sends_no_more_audio_and_still_ends_the_reply`    | yes: a muted turn that never ends its reply fails 2, one that keeps sending fails 1 |
| One reply on the wire                                                              | `SpeakTest.test_a_reply_released_in_pieces_is_one_reply_on_the_wire`                                                                                                                           |                                                                                     |
| Reply polish under streaming                                                       | `test_a_stale_repeat_releases_nothing_before_it_is_asked_again`, `test_a_reply_that_opens_like_an_earlier_line_streams_once_it_differs`, and the merged `PickedPlaceTest` and `StaleReplyTest` |                                                                                     |
| The question for a missing place                                                   | `test_the_question_for_a_missing_place_streams_and_a_guessed_city_stops_it`                                                                                                                    |                                                                                     |

The mutation script is `$VOICE_HOME/evidence/stream-release/mutate.py`, outside the repo: each mutant breaks one behaviour,
runs the tests that should catch it, and restores the file (measured, all six caught, on da24f47216).

All 8 suites at ae525736d7 (measured): segmentation 110, prototype 166, tools 72, local_decider 12, liquid_decider 13,
archive 7 and delegation 24 under `venv-lfm3`, and live 176 under `venv-live-client`, 580 tests, all passing.

## What this cannot see

- Whether a stop on a frame edge sounds acceptable, and whether a continuation sounds like one reply. Only listening
  decides that; Mason's live call is acceptance.
- The alignment and the lookahead result come from one speaker's call and 24 replies. A model, persona or speaking
  rate that let audio run ahead of text would need the measurement again (`align.py`).
- The by-ear quote rests on the LFM3 ASR, which still reads nothing from 4 of 24 one-block fragments; those quote the
  released text, which can run a few words ahead of what was heard.
- The screens' own blind spots (`live-fact-check.md`) are unchanged.
- The replay runs on loopback. The laptop adds about 0.04 s plus the device buffer (`latency-research.md`).
- One replay run per configuration. Two identical replays differed by 0.30 s at the median in the research, because
  reply length is sampled.

## Reproduce

```
python $VOICE_HOME/evidence/stream-release/simulate.py RUN_DIR ... --repo <checkout>   # CPU: replay recorded events
python $VOICE_HOME/evidence/stream-release/align.py RUN_DIR ... --repo <checkout>      # GPU: ASR of reply audio
python $VOICE_HOME/evidence/stream-release/run_replay.py LABEL --worktree <checkout> --difference ... --expects ...
python $VOICE_HOME/evidence/stream-release/audit.py LABEL --repo <checkout>
python $VOICE_HOME/evidence/stream-release/lookahead.py LABEL --repo <checkout> --blocks 1 2
```

The runs are `before/`, `after/` (eae21082e2) and `final/` (ae525736d7) under `$VOICE_HOME/evidence/stream-release/`,
222 MB with their audio, outside the repo.
