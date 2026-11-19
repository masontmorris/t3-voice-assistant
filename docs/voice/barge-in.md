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
   - The server keeps each reply a turn speaks as its own text and audio, because a turn can speak an answer and then a task update. It reads back the audio the playback clock says had played (`played_part`) and keeps the replies' text, joined in the order they were spoken, up to the last word the read-back matched (`heard_prefix`).
   - A barge-in inside the 250 ms grace window, or one settled after the reply ended, heard the whole reply. The record says `whole: true` and no read-back runs. A reply still being released is never whole, even when its audio ran out, because its text runs ahead of its audio.
   - The read-back runs beside the turns and never holds one: the next turn's reply and `turn_done` do not wait for it. A turn that ends first writes `heard: null, pending: true` and rewrites its record when the read-back lands. A read-back that fails, or takes over 5 s (`READBACK_TIMEOUT_S`, a guess, not a measured read-back time), records `heard: null` and the reason.
   - `played_s` is the server's estimate from the audio it sent and its own clock, not the client's report. How far it can run past what the client played is not measured.
   - A turn that continues a barge-in turn, because the person kept talking before it answered, takes over its record along with its audio.
   - `--barge-in-note on` also puts one short note in front of the new turn's audio saying the person interrupted and heard only the reply's start. The record keeps the note and the session's answer to it (`note_item`). Nothing matched, or the whole reply heard, sends no note. With the note on, the turn waits for its read-back, up to the 5 s bound, before its audio goes in. The note ships off, because the measurement below found no gain.

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

**Cutting the unheard part out instead.** Removing the unheard tail from the session would make a note unnecessary. The
installed llama.cpp build (b12428) cannot do it. Its `llama-server-impl.dll` names none of `conversation.item.truncate`,
`conversation.item.delete`, `conversation.item.retrieve` or `response.cancel` (measured, string search). The realtime
dispatch in the llama.cpp source under `$VOICE_HOME/research/llamacpp-src` (`server-realtime.cpp`, `handle`) adds only
`response.cancel`, which stops generation and removes nothing the session already holds. Truncation needs a patch that
rewinds the session's KV cache to the heard point.

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

## What it does not do yet

- **Speakers.** Echo is held to the voiced length like speech, so with speakers and the echo canceller off the reply would still stop itself after 600 ms. The transcript-stage echo check only catches short echoes. Speakers need issue #33's canceller.
- **Another person or a TV.** Sustained speech from someone else is a barge-in, and with no reply playing it starts a turn. Telling it apart needs a level or speaker cue, calibrated on a recording that has those sounds. No such recording exists yet.
- **Latency.** Stopping takes about 0.7 s from onset (measured, median), against 0.08 s before. The duck at about 0.1 s is the immediate cue. Lowering `--confirm-ms` trades that latency against stopping for "okay".
- **Long backchannels.** A backchannel over 600 ms voiced ("yeah, yeah, totally") stops the reply.
- **The note has no measured effect.** It ships off. The record of what was heard is kept on every barge-in. Turning the note on waits on the paired, seeded test described above.
