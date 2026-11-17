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
6. **What the session hears.** A muted reply still finishes inside the call's one voice session, so the session would otherwise hold a reply the person never heard. After a barge-in the server reads back the part of the reply's audio the client had played (`played_part`, then `heard_prefix`) and puts one short note in front of the new turn's audio saying the person interrupted and heard only the reply's start. Nothing audible, or a read-back that matches none of the reply's words, sends no note. `--barge-in-note off` leaves it out for an A/B. The turn record keeps the note and the words the read-back matched, under `live.barge_in_note` and `live.barge_in_heard`.

The voiced count uses a -60 dBFS floor. Call 2's quiet short turns peak near -40 dBFS, and the endpointer's -50 dBFS floor counts only 260 to 300 ms of "Answer the question." and "Okay, thanks." as voiced (measured).

**Why 600 ms.** On Mason's 40 recorded turns, `voice/research/live/overlap_bench.py` shows every turn reaching 600 ms voiced within 680 to 980 ms of its onset, median 700 ms. One exception, "Can you hear me?", reached it only through the transcript stage. His lone "Okay." holds 540 ms voiced, so a shorter threshold would stop the reply for it (measured).

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

Two things are true of the first attempt and decide what shipped (measured, 2026-11-17). Quoting the words the person
heard made the model go on with the old reply, or read the quote back: with the quoted note, three answers continued
the interrupted reply and two more talked about the note itself. So the shipped note names no words. It says they
interrupted and heard only the reply's start; the words the read-back matched stay in the record for a person.

The measurement is the voice session alone, one LFM3 session per case, no decider and no task service
(`voice/research/live/bargein_note_session.py`, records under `$VOICE_HOME/evidence/live/bargein-note-session/`). The
nine cases are the interruption clips of the gate-1 run: five of Mason's turns from the 2026-11-16 call and four
synthetic commands. Each session runs the synthetic lighthouse request, its reply, then (with the note) the note, then
the interruption. The outcome per case comes from reading the saved reply text.

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

Measured: 3 of 9 answers continued or ignored the interruption with no note, 4 of 9 with the unquoted note, and 5 of 9
with the quoted note. The unquoted note is within noise of no note at n = 9, and it changed single cases in both
directions: "Hold on." was handled and "Stop." and "No, no." were not. So this sample does not show the note stopping
an answer that carries on the old one, and it does show that quoting the heard words is worse. Do not read the 3, 4
and 5 as rates. The quoted note ran the same session harness before the note text changed (`note/` in the records
directory).

The full live-server A/B on the same recordings is
`$VOICE_HOME/evidence/live/bargein-note/` (`voice/research/live/bargein_note_ab.py`). It could not answer the question:
the decider and the mock task service put their own text in front of the model, so both arms' replies were mostly the
same failed-lookup line and the arms did not even produce the same barge-in set (16 cases without the note, 10 with
it). Its records do show the shipped path end to end: the note is a text item in the call's session before the new
turn's audio, and each record carries the note and the read-back under `live.barge_in_note` and `live.barge_in_heard`.

## What it does not do yet

- **Speakers.** Echo is held to the voiced length like speech, so with speakers and the echo canceller off the reply would still stop itself after 600 ms. The transcript-stage echo check only catches short echoes. Speakers need issue #33's canceller.
- **Another person or a TV.** Sustained speech from someone else is a barge-in, and with no reply playing it starts a turn. Telling it apart needs a level or speaker cue, calibrated on a recording that has those sounds. No such recording exists yet.
- **Latency.** Stopping takes about 0.7 s from onset (measured, median), against 0.08 s before. The duck at about 0.1 s is the immediate cue. Lowering `--confirm-ms` trades that latency against stopping for "okay".
- **Long backchannels.** A backchannel over 600 ms voiced ("yeah, yeah, totally") stops the reply.
- **The note has no measured effect.** In the 9-case session comparison above, the shipped note did not stop answers
  that carry on the interrupted one, and no note at all scored within a case or two of it. The live-server A/B could
  not settle it. Retesting needs a configuration where the interrupted answer is a long answer with no task text after
  it, and more than one sample per case.
