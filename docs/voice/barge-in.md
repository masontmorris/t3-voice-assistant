# Barge-in and crosstalk

Goal (Mason's): talk to the assistant the way people talk to each other. Talking over a reply stops it quickly. A backchannel, other voices in the room or the assistant's own voice does not. Nothing said during an interruption is lost.

## What was wrong

**False barge-ins (measured).** Call 1 of 2026-11-16 15:26 (`$VOICE_HOME/evidence/live/20261116-152653`, turns 1 to 19) logged 16 `barge_in` events on the laptop. That matches, one for one, the 16 turn starts whose previous turn had sent reply audio. The server sent `barge_in` on every new turn after a reply, whether or not the reply was still playing. Rebuilding the call's wall clock from the decider calls' timestamps and each reply's audio length puts 15 of the 16 starts 1.5 to 17 s after the reply had finished playing. Only turn 8 began during playback, about 0.6 s before turn 7's reply ended. The client flush was harmless for those 15. The mute that came with it was not: a turn still waiting for a task update lost that update. That loss did not happen in these calls, because every update arrived within 5 s, which is inferred from the turn records.

**Every sound stopped the reply (measured in code, not yet heard live).** A barge-in fired after 100 ms of voiced audio. A backchannel, a cough, the TV or the reply's own echo through speakers would all stop the reply. Mason barely talked over the assistant in these calls, so the records hold no case of it.

**Turns 8 and 9 (measured).** Mason paused about 0.7 s mid-sentence ("just for the record...", "many things like, uhm,"). The endpointer ended both turns, and his next words arrived before any reply, so the server folded each one into the next. Turn 10 holds the whole sentence. No words were lost. The client printed `heard: None` for 8 and 9, and turn 12 is Mason saying "all of my past utterances just went through blank, empty". The link between the two is inferred. Checking the transcript when a turn ends cannot spot an unfinished sentence: the live ASR is about one word behind at that moment, so that check would hold open 16 of 45 complete turns (measured). That approach was dropped.

## What the server does now

`voice/live/overlap.py` holds the rules and `server.py` wires them in.

1. **Playback clock.** Each checked reply adds its audio's duration, from when it is sent or when the audio before it ends. A reply counts as audible until then, plus 250 ms of grace for the client's buffer and the socket.
2. **Overlap.** Speech that starts while a reply is audible is held with all its audio. The client ducks the reply at once (`duck`, to 0.35 gain).
3. **Verdict.**
   - The overlap reaches 600 ms of voiced audio: barge-in.
   - It ends sooner and its one-shot transcript is a backchannel, empty, or mostly the reply's own words: dismissed. The reply comes back to full volume (`unduck`), and the audio goes to `overlap_NNN.wav`.
   - It ends sooner and the transcript says anything else ("stop", "can you hear me?"): barge-in.
   - The reply finishes first: a plain turn.
4. **No lost words.** The barge-in turn opens with the overlap's audio, pre-roll included.
5. **Record.** Every overlap is a line in `overlaps.jsonl`, and the barge-in turn's record names its overlap under `live.overlap`.
6. **What the person heard.** A muted reply still finishes inside the call's one voice session, so the session holds a reply the person did not hear in full. Every barge-in, by voice or push-to-talk, records what they heard under `live.barge_in` in the new turn's record.
   - The server keeps each reply a turn speaks as its own text and audio, because a turn can speak an answer and then a task update. It reads back the audio the playback clock says had played (`played_part`) and keeps the replies' text, joined in the order they were spoken, up to the last word the read-back matched (`heard_end`).
   - A barge-in inside the 250 ms grace window, or one settled after the reply ended, heard the whole reply. The record says `whole: true` and no read-back runs. A reply still being released is never whole, even when its audio ran out, because its text runs ahead of its audio.
   - The read-back runs beside the turns and never holds one: the next turn's reply and `turn_done` do not wait for it. A turn that ends first writes `heard: null, pending: true` and rewrites its record when the read-back lands. A read-back that fails, or takes over 5 s (`READBACK_TIMEOUT_S`, a guess, not a measured read-back time), records `heard: null` and the reason.
   - `played_s` is the server's estimate from the audio it sent and its own clock, not the client's report. How far it can run past what the client played is not measured.
   - A turn that continues a barge-in turn, because the person kept talking before it answered, takes over its record along with its audio.
   - `--barge-in-note on` also puts one short note in front of the new turn's audio saying the person interrupted and heard only the reply's start. The record keeps the note and the session's answer to it (`note_item`). Nothing matched, or the whole reply heard, sends no note. With the note on, the turn waits for its read-back, up to the 5 s bound, before its audio goes in. The note ships off, because the measurement below found no gain.
   - `--trim-unheard on` takes the unheard part out of the session instead, and needs the patched llama-server (see [Trimming what was not heard](#trimming-what-was-not-heard)). The record keeps what it cut under `trim`.

The voiced count uses a -60 dBFS floor. Call 2's quiet short turns peak near -40 dBFS, and the endpointer's -50 dBFS floor counts only 260 to 300 ms of "Answer the question." and "Okay, thanks." as voiced (measured).

**Why 600 ms.** On Mason's 40 recorded turns, `voice/research/live/overlap_bench.py` shows every turn reaching 600 ms voiced within 680 to 980 ms of its onset, median 700 ms. One exception, "Can you hear me?", reached it only through the transcript stage. His lone "Okay." holds 540 ms voiced, so a shorter threshold would stop the reply for it (measured).

### Sound before the reply

Sound that starts after a turn has ended, while that turn is still working on its reply and nothing is audible, used to cancel the turn at once and carry its audio into a new one ("continued"). On a live call with steady background noise, noise a second after a thread question did that twice, and the caller heard nothing for three turns. The endpointer starts a turn on energy alone, so it cannot tell noise from words.

Such sound is now held like an overlap, without the 600 ms voiced rule, and judged on its transcript: every second while it lasts (`judge_held`, `HELD_CHECK_S`) and once when it ends, however short it is. Words before any reply continue the turn at once: the turn is cancelled and the words, with the rest of what follows, carry into a new one, as before. No words so far lets the reply go while the sound is still held. A reply that becomes audible while the sound is held is ducked, and from then the overlap rules decide on the transcript: an interruption stops the reply within about a second, and an echo, a backchannel or noise does not. Noise never counts as voiced here, so a reply is not stopped for sound alone.

Until the first judgment lets the reply go, the call's voice session takes no new item and makes no response (`floor_free`), the turn's reply audio waits (`Conversation.hold_floor`), and the turn does not finish. So a turn that the sound continues has put nothing in the session. A relay waits for the floor before its shield, and again after its note is acknowledged, and then makes no response for a turn that was cancelled or muted meanwhile. The turn's transcript, decider pick, tools and drafts keep running.

Measured with `python -m voice.live.rehearse drive --noise` (five calls; synthetic road noise 2 to 4.4 s long, 4 to 12 dB below the speech, starting 1.0 to 1.3 s after a thread question; then a thread question continued after a 0.9 s pause), two runs each, GPU:

| | Before | Judged at the end | Judged every second (shipped) |
| --- | --- | --- | --- |
| The noise cancelled the question's turn | 9 of 10 calls | 0 of 10 | 0 of 10 |
| The question's status line was said | 8 of 10 (both misses were the decider asking a confirmation) | 10 of 10 | 10 of 10 |
| Its first audio, after the question's speech ended | | 4.6 to 6.9 s | 1.4 to 2.8 s |
| The continued question was joined into one turn | 10 of 10 | 8 of 10 | 8 of 10 |
| Its status line was said | 10 of 10 | 8 of 10 | 9 of 10 |

Every noise burst the endpointer took for speech transcribed empty. A plain thread status question takes a median of 2.0 s from the end of speech to first audio (99 rehearsed turns: 0.7 s endpointer silence, 0.36 s final transcript, decider pick and tools, 0.20 s draft, 0.52 s relay). The start and mixed calls scored as before on the shipped code (start: 4 of 5 threads started, 0 false claims; mixed: 10 of 10 first relays and both halves heard, 0 free lines naming a thread).

What is open: of the two missed joins in the shipped runs, one was the call with the longest noise, still running when the next words came, so those words joined the held noise and were judged against the reply that had gone out meanwhile; the other was the first part's reply becoming audible 1.5 s after its speech ended, before the second part's sound began, a race the old code loses too, by muting instead of joining. On joined two-clause questions the status relay ran past its draft ("...is working. It is") on the first attempt in 3 of 8 shipped turns, against 0 of 10 before; the cause is not found. A probe of the first part's audio already sitting in the session was inconclusive (4 of 5 against 3 of 5). Idle turns are not held: sound during idle news still supersedes it.

## Results

Real audio and synthetic audio are reported separately.

End to end, on 2026-11-16 17:05 ET. One live server from this branch with `--mock-tools`, in a `voice-bargein` lease under `quiet_run`, and the real client over loopback with `--wav`. The driver is `$VOICE_HOME/evidence/live/bargein/run_e2e.py`, the scorer is `score_e2e.py` beside it, and the records are in `gate1/`. The overlap set and the replay are Mason's recorded turns. The long reply they overlap is from a synthetic prompt ("Tell me about the history of lighthouses in three sentences.").

**Real audio.**

| Case | Before (measured) | After (measured) |
| --- | --- | --- |
| Call 1 turns 1 to 5, 11, 13 and 14 replayed in order, each after the reply has played | live call: 16 `barge_in` in 19 turns, 15 with nothing playing | 0 `barge_in`. All 8 turns came back with their full transcripts. |
| Mason's backchannels over a reply: "Uhm", "Hmm" (cut from turns 10 and 19), "Okay." (turn 14), "Okay, thanks." (turn 34) | every one would stop the reply (start rule) | 4 of 4 dismissed: "Uhm" by its empty transcript, "Hmm" as too short, the two "Okay" turns as backchannels. The reply kept playing. |
| Cut clip that turned out to hold "Oh, did you?" (turn 36) | stops | stops. The transcript shows real words, so the label was wrong, not the gate. |
| Mason's interruptions over a reply: turns 4, 11, 16, 21 and 29 | stops at 80 ms from onset (offline bench) | 5 of 5 stop. `barge_in` reached the client 696 to 1714 ms after onset, median 862 ms. "Can you hear me?" took the transcript path (1714 ms). Every interrupting turn's transcript was its whole clip. |

**Synthetic audio (LFM3 TTS).**

| Case | After (measured) |
| --- | --- |
| Backchannels "Yeah.", "Right.", "Okay.", "I see.", "Got it." | 5 of 5 dismissed |
| Backchannels "Mm-hm.", "Uh-huh.", "Yeah, totally." | 3 of 3 stopped the reply: each reached 600 ms voiced |
| Commands "Stop.", "Wait.", "No, no.", "Hold on." | 4 of 4 stop, 683 to 1360 ms after onset ("Wait." by transcript). Every transcript whole. |

**Offline bench.** `python -m voice.research.live.overlap_bench` on Mason's three live records gives 40 of 40 turns as barge-ins and 4 of 5 cut backchannels dismissed. The miss is the "Oh" cut above. The old rule dismissed 0 of 5.

## The note after a barge-in

The note ships off (`--barge-in-note off`). The record of what was heard stays on. The measurements below show no gain
from the note, and one fair reading of them shows harm.

**Quoting the heard words is worse (measured, 2026-11-17).** With a note that quoted the words heard, three answers
continued the interrupted reply and two more talked about the note itself. So the note, when on, names no words. It
says the person interrupted and heard only the reply's start.

**The session comparison.** It runs the voice session alone, one LFM3 session per case, with no decider and no task
service (`voice/research/live/bargein_note_session.py`, records under `$VOICE_HOME/evidence/live/bargein-note-session/`).
The nine cases are the interruption clips of the gate-1 run: five of Mason's turns from the 2026-11-16 call and four
synthetic commands. Each session runs the synthetic lighthouse request, its reply, then (with the note) the note, then
the interruption. Each run generated its own interrupted reply, so the arms were not paired by design. In 8 of the 9
cases the saved interrupted replies differ between all four runs (measured). In the ninth, "No, no."
(`synth/turn_012.wav`), `control` and `note-noquote` saved the identical interrupted reply (measured). That one paired
case reads against the note: the control stopped and the unquoted note continued the old reply, under both the hand
labels and the harness score.

Hand labels, read from the saved replies of the `control`, `note-noquote` and `note` runs:

| Case | No note | Note, no quote | Note, quoted |
| --- | --- | --- | --- |
| Call-1 turn 4 | answered | answered | answered |
| Call-1 turn 11 | answered | answered | answered |
| Call-1 turn 16 | restarted | restarted | restarted |
| Call-1 turn 21 | answered | answered | answered |
| Call-1 turn 29 | continued | continued | continued |
| "Stop." | stopped | continued | continued |
| "Wait." | continued | continued | read the note aloud |
| "No, no." | stopped | continued | repeated the old reply |
| "Hold on." | continued | answered | continued |

**The control ran twice.** `control-badjoin` started at 06:12 and `control` at 06:15, with the same configuration. The
first run joined the reply's text pieces with spaces inside words (measured in its saved texts), which breaks the
harness's word-based score but not the replies. Treat it as a second control sample.

Three readings of the same runs, each a count of nine, not a rate:

| Reading | No note, 06:12 | No note, 06:15 | Note, no quote | Note, quoted |
| --- | --- | --- | --- | --- |
| Harness score `reused_old_answer`, named before the runs | 4 (invalid: split words) | 7 | 7 | 8 |
| Hand labels above: continued or ignored the interruption | not labelled | 3 | 4 | 5 |
| Independent review's content rule | 1 | 1 | 3 | not read |

The harness's own score puts no note and the unquoted note level at 7 of 9. The hand labels put the unquoted note one
case worse. The review's content rule puts it two cases worse. The independent review reports that the same
configuration moved by one case from run to run, which is as large as the one-case gap in the hand labels (reported by
the review, not re-derived here). So there is no measured gain from the note, and the content rule reads it as harm.

**The live-server A/B could not answer the question.** `$VOICE_HOME/evidence/live/bargein-note/`
(`voice/research/live/bargein_note_ab.py`) runs the whole pipeline. The decider and the mock task service put their own
text in front of the model, so both arms' replies were mostly the same failed-lookup line. The arms did not produce the
same barge-in set either: 16 cases without the note, 10 with it. Its harness score is 11 of 16 without the note and 9 of
10 with it. These runs used an older build that quoted the heard words. Their records hold that note under
`live.barge_in_note` but carry no read-back field, and nothing in them shows where the note sat against the turn's
audio (measured: no record has the read-back field). That order is shown by the unit test, not by these records.

**Cutting the unheard part out instead.** The installed build (b12428) has no event that removes anything from a
session. The patched build does: see [Trimming what was not heard](#trimming-what-was-not-heard).

**Before turning the note on.** A test that could show a gain needs:

- Pairing. Both arms answer the same interrupted reply per case, injected as fixed text and audio, not regenerated per
  run.
- Seeds. A fixed sampling seed per sample, shared by both arms.
- More than one sample per case. At least three seeds per case per arm, so the control-to-control spread is measured
  before any effect is claimed.
- A scoring rule named before the run and shown to catch the known failures on record: carrying on the old reply,
  reading the note aloud, and repeating the old reply.
- Cases where the interrupted answer is long and no task text follows it, so the decider and task service do not swamp
  the note.
- A gain larger than the measured control-to-control spread, reported per case as paired differences.

## Trimming what was not heard

`--trim-unheard on` (default off) makes the voice session's record of an interrupted reply what the caller heard. It
needs `--llama-server` pointing at a build with [the truncate patch](realtime-truncate-llamacpp.patch); the installed
build answers the new event with an error, and the turn then sends no note and keeps the whole reply, as today.

**What the session holds after a barge-in, without it.** One llama-server slot holds the call's whole conversation as
one sequence of positions (`server-realtime.cpp`, `server_realtime_slot`). A reply in mixed mode goes in as the
generation prompt and `<|mixed_start|>`, then blocks of text tokens and audio frames: the frames go back in as
embeddings of their codes, so the slot holds the reply's audio as well as its words. A muted reply still runs to its
end, so all of it, heard or not, is in the slot, and the next turn's audio follows it. Measured on a bread request in
the options run below: the reply took 379 to 707 positions.

**The options, measured** (`voice/research/live/trim_options.py`, records under
`$VOICE_HOME/evidence/live/trim-options/run1`, five trials per arm, the patched b12428 build on the RTX 3060 in the
GPU lease). Each trial builds the same three-turn call on a fresh session (two short questions, then the long bread
request), takes back all but the long reply's first 12 words, and asks the caller's next turn ("Sorry, someone was at
the door. Please go on."). "First audio" runs from the first step after the long reply to the next reply's first audio.

| Option | Works on this build | First audio, median (range) | What it loses |
| --- | --- | --- | --- |
| Nothing (control) | | 0.149 s (0.141 to 0.220) | |
| (a) Cut the slot's sequence at the reply's start (`llama_memory_seq_rm`), no saved state | No. LFM3 is a hybrid: its 13 short-convolution layers keep one running state that cannot move back, and 5 of its 11 attention layers keep only a 256-position window. `seq_rm` returned false (n_past 1198 to 815), and the session ended with an error. | | |
| (b) The truncate patch: the state saved when the reply began is put back, the heard words go in as text | Yes | 0.153 s (0.149 to 0.161) | The interrupted reply's audio frames; its heard words stay, as text. Earlier turns are untouched. |
| (c) A new session, seeded with the call as text (the reply cut to the heard words) | Runs | 0.220 s (0.199 to 0.236) | Every earlier turn's audio, and the voice's own audio history. 4 of 5 next replies repeated the caller's words back ("It looks like someone was at the door. Please continue."), as the recorded call's seeded restarts did. |
| (d) llama-server's `POST /slots/{id}?action=save` and `restore` | No. A realtime slot is processing for the whole session, and a save is deferred until it is not: the call timed out after 20 s. | | |

The live server has not re-seeded a session from text since the call of 2026-11-16, for the reason (c) shows again.
The table was measured on the patch's first version, which saved a checkpoint on every response; the second, below,
saves one only for a session that asks, and answers a truncate once it has run.

**How (b) works.** A session asks for it with `session.update {"response_checkpoints": N}`, 0 to 8. With 0, the
default, the patched build saves no state and keeps no list of items, and does what the installed one does: on the same
clips, first audio of the turn after a long reply had a median of 0.142 s on the installed build and 0.145 s on the
patched one with checkpoints off (10 trials each, ranges 0.135 to 0.153 s and 0.139 to 0.157 s; the two are different
builds, CUDA 12 and 13), and its log holds no checkpoint (`trim_options --arms control --clips`, records under
`$VOICE_HOME/evidence/live/trim-options/pair-*`). Asked for, the slot saves the sequence's partial state at the start of
every response (`llama_state_seq_get_data_ext`, `LLAMA_STATE_SEQ_FLAGS_PARTIAL_ONLY`: the convolution state and the
sliding-window cells, as llama-server's own context checkpoints do) and keeps the last N, never more than 64 MiB in all;
they go with the slot when the session ends, by any path. Measured on this model: 2.2 to 3.1 MiB each, and at most 5.4
MiB held at once over 48 responses.

`conversation.item.truncate {item_id, text, expect_last_item_id}` takes back what the listener did not hear of one of
those responses. llama-server refuses it, and changes nothing, when:

- `expect_last_item_id` is not the conversation's last item: the client is behind the session (`item_truncate_stale`);
- the caller's audio was committed after the response (`item_truncate_caller_turn`), so a later caller turn is never
  dropped;
- `text` is not the start of what the response itself generated (`item_truncate_invalid_text`);
- a response is still generating, or audio is appended and not committed;
- the session keeps no checkpoints (`item_truncate_off`), or the response's checkpoint was dropped.

Otherwise it puts the saved state back, removes the full-attention cells after it, and appends `text` as the
assistant's text; an empty `text` leaves none of the response, and the caller's turn open. The items after the response
in the same caller turn (a note, a task update) go with it, and `conversation.item.truncated` names them. The session's
reader waits for the slot to have run the rewind before it reads the next client event, and only then answers. So
nothing the client sends after a truncate can enter the conversation before it, and an answer that comes late describes
a rewind that came first. From sending to the answer: 1.1 ms median idle (at most 1.3 ms), 10.0 ms median beside a
session generating a long reply (at most 18.7 ms; 19.3 ms across three runs of 20).

Checked on the real server (`voice/research/live/trim_protocol.py`, records under
`$VOICE_HOME/evidence/live/trim-protocol/run3`), each case on the events and the server's log, all passing: a session
without checkpoints saved none and refused; a made-up `text` and the reply's own text with more words after it were
refused and nothing was rewound, and the right prefix then was taken; after a note, a truncate that expected the reply
to be last was refused; after the caller's next audio, a truncate of the reply before it was refused; an answer, a
note and an update in one turn: truncating the answer removed the note and the update and named them; and the
truncate, the caller's next audio and a response sent at once, with no wait: the answer came before the audio's commit,
and the next checkpoint sat at position 211, past the rewound 167, so the new audio went in after the rewind.

**What the live server does with it.** With `--trim-unheard on` the call's session asks for 8 checkpoints. Each reply a
turn speaks records the session item whose audio it carries (`reply_item`), and whether it is a line the trim never
cuts: a relayed T3 line, a reply that asks the caller for something a task waits on (`asks_caller`, `ASK_NOTE`), and the
line that takes a wrong reply back. On a barge-in, `hearing` works out what was heard:

- Where playback stopped is the client's own report. On `barge_in` every client (`client.py`, the browser page,
  the app's call in `apps/web/src/voiceCall/`) sends `position {turn, reply, samples}`: how many samples of the
  playing reply it had handed to its output when it dropped the rest, about 60 bytes, once per barge-in. The server
  waits for it at most 1 s (`POSITION_WAIT_S`, beside the turns) and only then falls back on its own playback clock.
- Every reply before the playing one played whole, whatever the read-back says, so a read-back that misses a
  confirmation question the caller heard does not cut it.
- In the playing reply, the position is placed in its words by the reply's own timing (`spoken_end`): LFM3 says a
  reply's words at an even pace across all the audio it makes for it, so the share of its whole audio that played
  says that share of its whole text, and never a word the model had not yet written when that audio arrived, since it
  writes each block's text before its audio. On the 89 barge-ins of the previous eval this reading sat a median of 0
  words from the read-back, within one word in 61; the share of only the released text, the earlier rule, sat 2
  words past it.
- The read-back of the played audio (`heard_end`, its continuous match from the start) is the cross-check, kept in
  the record beside the position (`by`), and a barge-in where the two are more than 2 words apart is logged.
- A turn whose playing reply, or any later one, is a kept line is not trimmed: the line stays whole in the session.
- The truncate expects the newest item the session told of (`last_item`).

The turn waits for the read-back, then for the trim's answer for at most 0.5 s (`TRUNCATE_WAIT_S`, about 25 times the
slowest answer measured); an answer that comes later still lands before the turn's audio, and the record is rewritten
when it does. A turn that continues the barge-in turn waits on the same trim, never a second one. When it is taken, the
note (`cut_note`) goes in, and the call's other records of those replies become what was heard:

- The ledger (`ConversationLedger.cut`): the stopped turn's replies become the heard words, delivery `cut`, so the next
  turns' fact check and the workers' call context read what was heard.
- The call transcript recorded on the thread (`CallTranscript.cut`): the cut line keeps the heard words, flagged
  `interrupted`; a later reply of that turn, unheard, is left out.
- The reply check needs nothing: it already judges a cut-off reply by the part that played, by the same time rule,
  before it plans a correction (`replycheck.py`), and a claim only in the unheard part gets none.
- What "it" or "that thread" refers to (`T3Tasks.said_source`, `last_line`) is set only by checked T3 lines, which the
  trim never cuts, so it still names a line the session holds.

**A question cut off before it was heard.** The sender writes a relayed line to the client as fast as the socket
takes it, so its delivery reads "sent" within moments and T3 counts the question heard (`Conversation._send_loop`,
`T3TaskService.record_delivery`). With the trim on and the client's position known, a barge-in over a kept line that
asks the caller something (`ASKED_KINDS`) before it had all played gives it back: the line stays whole in the
session, T3 marks the open question not heard (`T3Tasks.unheard_question`), so the caller's words over it cannot
answer it, and its line goes back through the news path (`retry_news`) to be asked again once the caller's turn is
done. A question heard to its end stays heard. With the trim off, nothing changes: it counts as heard.

**Operator only.** `--trim-unheard` and `--llama-server` are flags of `voice.live.server`, and `--server-arg` is a flag
of the gate; no client message sets them (`hello` reads the mode, the recording flag, the thread and the environment).
A `--llama-server` that is not there runs the installed build with the trim off and says so in one line. A server
without the patch refuses the truncate: the turn sends no note, the call logs it once and goes on untrimmed.

**The live comparison** (`voice/research/live/trim_eval.py`, records under `$VOICE_HOME/evidence/live/trim-eval-pos/`,
2026-11-24). The real live server (`voice.live.rehearse` serve: its in-process T3 seam, the hosted decider, LFM3 on the
patched build in both arms, so the flag is the only difference), and synthetic callers in the server's own TTS whose
client reports where playback stopped. Each caller asks for a long answer and talks over it 1.5, 2.0, 2.5, 3.0 or 3.5 s
after the reply starts, three calls per delay and shape. The hosted decider (the scenarios' `Judge`) answers one stated
question per reply, with what the caller heard (the barge-in record) and the rest of the stopped reply as its context.
The context and the questions are frozen: the first run's, unchanged. Counts are of calls.

| Shape | Question (yes is) | Off | On |
| --- | --- | --- | --- |
| "Sorry, someone was at the door. Please go on." | goes on from where the heard part stops (good) | 2 of 15 | 3 of 15 |
| | treats the unheard part as already told (bad) | 2 of 15 | 2 of 15 |
| "Wait, what was the last bridge on your list?" (asked one bridge into five) | talks of the list as if it was heard whole (bad) | 12 of 12 | 0 of 7 |
| | treats the unheard part as already told (bad) | 0 of 12 | 7 of 7 |
| "Actually, never mind that. How many legs does a spider have?" | answers it (good) | 15 of 15 | 15 of 15 |
| | treats the unheard part as already told (bad) | 0 of 15 | 0 of 15 |

The unscored "bridges" calls, 3 off and 8 on, are the decider's: it sent the request to a lookup, the reply was "I
can't look that up.", and the caller's line came after it had ended, so there was no barge-in. All 37 barge-ins with the
trim on were trimmed; every one used the client's report, none the clock.

**The heard point.** On the 78 barge-ins with a read-back, the position's reading sat a median of 0 words from the
read-back with the trim off and 1 with it on (median distance 1 word in both, within one word in 54 of 78, from 3
fewer to 11 more). The 11 is one reply whose text opened with markdown ("**Ingredients**"), which the voice does not
say. The previous rule, the later of the clock's share of the released text and the read-back, sat a median of 2 words
past the read-back, up to 14.

**"Go on" is not clearly better on than off.** 3 of 15 against 2 of 15, and the same 2 of 15 treat the unheard part as
told. With the exact position the cut falls early, most often in the reply's opening ("Sure! Here's a simple
step-by-step guide to"), so what the caller missed is the whole of the content, and in most calls the voice then gives
it from its first step, in both arms. Three typical on-arm calls, word for word, heard and then said:

- Heard "Sure! Here's a simple step-by-step guide to baking bread at home. First"; said "Next, you'll need to mix the
  ingredients like flour, yeast, water, and salt. Then, combine them to form a dough and let it rise. After that, shape
  the dough into a loaf, place it in a baking pan, and bake it in a preheated oven." The judge: did not go on from
  there (0.19), treats unheard as told (0.57).
- Heard "Sure! To bake bread at home, you need"; said "To bake bread at home, you need to start by preparing your
  ingredients. You'll need flour, water, yeast, salt, and optionally a bit of oil. First, mix the dry
  ingredients—flour, salt, and yeast—in a bowl and stir until combined." The judge: goes on from there (0.51).
- Heard "Sure! Here's a simple step-by-step"; said "To bake a loaf of bread at home, start by preheating your oven to
  375°F (190°C) and placing a baking sheet or baking pan inside. In a large bowl, mix 3 cups of all-purpose flour, 1
  teaspoon of salt, and 1 teaspoon of baking powder." The judge: did not go on (0.20).

Read by hand, all three give the steps the caller had not heard, from the first, and none says it already gave them;
the judge's "goes on" question marks two of the three no. Of the other twelve, two "go on" turns were taken as a
request about T3 threads ("I checked the recent threads and couldn't find a match."), and one read the caller's words
back ("The person said, 'Sorry. Someone was at the door. Please go on.'").

**The list.** Without the trim, every scored "last bridge" answer named one as the last on the list. With it, all 7
named the list's next bridge as next ("Next, the Tower Bridge in London, UK."): none claims a list the caller never
heard, and none answers the question. The two frozen questions disagree on those 7 (0 of 7 against 7 of 7), and the
second is the one misjudging: the replies say the Tower Bridge as new, which the judge, seeing it in the unheard part,
reads as treating it as told.

**Delay.** From the end of the barge-in turn's speech to its first audio: median 1.13 s off and 1.13 s on (means 1.26 s
and 1.48 s, 42 and 37 turns). The trim's waits: the read-back, a median of 2 ms and one of 3.6 s, and the truncate's
answer.

**One voice** (measured on the previous round's 15 calls per shape; this round changed only where the cut falls).
`voice/research/live/one_voice.py` embeds replies with microsoft/wavlm-base-plus-sv (a speaker
verification head, on the CPU) and compares them by cosine similarity. The stopped reply against the reply after the
barge-in: median 0.975 (0.890 to 0.990, 45 calls) off, 0.969 (0.895 to 0.988, 45 calls) on. For scale, stopped replies
of different calls against each other, one voice saying other words: 0.912 to 0.993; stopped replies against the same
lines in the TTS's other voice: 0.28 to 0.43. No reply after a trim moved toward a second voice: the trim changes no
instruction and no voice setting, and the session is never replaced. The lowest pairs in both arms are the shortest
replies ("Spiders have eight legs."), which embed less steadily.

**The gate.** The default gate with the flag off is green (eight PASS, `follow-up` XFAIL, `news-permission` and
`short-yes` XPASS; `$VOICE_HOME/evidence/scenarios/trim3-default`). With `--server-arg=--trim-unheard=on
--server-arg=--llama-server=...`, `speak-over --spread 5` passed 5 of 5 (`trim3-speak-over`); each call's stop came from
the client's report, and the caller talked over a relayed T3 status line, a kept line that asks nothing, so it stayed
whole and was not given back. No scenario checks the trim's effect, which is a judged count across calls.

**What is open.**

- **"Go on".** With the heard point now where the caller stopped, "go on" is about as good with the trim as without:
  most cuts fall in the opening, and the voice gives the content from its first step either way. The judge's "goes on"
  question marks such a reply no, so its count understates both arms.
- **The list question.** Asked about the part it never reached, the voice goes on with the list rather than saying it
  had not got there. No note change was tried.
- **Markdown.** A reply whose text holds markdown its audio does not say (`**Ingredients**`) puts the position's
  reading a few words late, by up to 11 measured.
- The patch keeps at most 8 responses' checkpoints; a reply older than that cannot be trimmed, and the truncate is
  refused.

**Build.** Unpack the b12428 source (`$VOICE_HOME/research/llamacpp-asrtick/src.tar.gz`, commit b4c0e00) into
`$VOICE_HOME/llamacpp-truncate/src`, apply `realtime-truncate-llamacpp.patch` there (`patch -p1`), configure with
CUDA (sm86, shared libs, `GGML_CUDA_FA_ALL_QUANTS=OFF`) and build only `llama-server`. The prebuilt web UI that the
source asks Hugging Face for has changed since b12428 and fails its own asset check; copy a matching `dist/` into
`src/tools/ui/dist`. Put the CUDA 13 runtime DLLs beside the binary. The installed build is not touched. `LLAMA_RT_REWIND_CUT_ONLY=1` makes the patched
server do option (a) instead, for the measurement only.

## What it does not do yet

- **Speakers.** Echo is held to the voiced length like speech, so with speakers and the echo canceller off the reply would still stop itself after 600 ms. The transcript-stage echo check only catches short echoes. Speakers need issue #33's canceller.
- **Another person or a TV.** Sustained speech from someone else is a barge-in, and with no reply playing it starts a turn. Telling it apart needs a level or speaker cue, calibrated on a recording that has those sounds. No such recording exists yet.
- **Latency.** Stopping takes about 0.7 s from onset (measured, median), against 0.08 s before. The duck at about 0.1 s is the immediate cue. Lowering `--confirm-ms` trades that latency against stopping for "okay".
- **Long backchannels.** A backchannel over 600 ms voiced ("yeah, yeah, totally") stops the reply.
- **The note has no measured effect.** It ships off. The record of what was heard is kept on every barge-in. Turning the note on waits on the paired, seeded test described above.
