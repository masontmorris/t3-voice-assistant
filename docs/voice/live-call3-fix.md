# Why the live call answered old turns, and what changed

The live call recorded on 2026-11-16 (37 turns of real microphone audio, records in
`$VOICE_HOME/evidence/live/20261116-152653/`) answered the previous turn, spoke its own instructions, and repeated the
person's words. This page states the causes found in those records, the experiments that separated them, and what
the branch `voice/live-fix-call3` changes. Every experiment's outputs are under
`$VOICE_HOME/evidence/live/callfix3/`.

## Causes

| Symptom in the call | Cause | Status | Turns |
| --- | --- | --- | --- |
| Replies answer an earlier turn; "I need to explain..." narration | The newest text before every reply was a JSON list of the person's earlier turns. The model answers the newest text it reads, in place of the audio. | measured | 5, 7, 10, 12, 19, 24, 27, 29 to 37 |
| The person's words repeated back; turn 10 answers turn 2 | The voice session was restarted every 8 turns and seeded with the call's text. The first answered turn after each of the three restarts is wrong. | measured | 10, 17, 28 |
| "Why do you sound different?" | Two fixed lines were rendered by the TTS endpoint, which is a different voice from the call's. | measured | 22, 26 |
| "Past utterances went through blank" | A turn joined to the next one is cancelled with no transcript, and the client printed `heard: None`. Its audio also stayed in the voice session, so the joined turn was heard twice. | measured | 8, 9, 10 |
| A question with no answer, a failed request, the wrong city | Only weather and time have a worker. Anything else fails, and a request that says "weather" with no place reran the earlier place. | measured | 2, 22, 25, 28 |
| A correct place refused | The fact check read a place only after a preposition, so "what about San Diego?" did not ground "in San Diego". | measured | 22 |
| Slow first speech | A reply is held until it is fully generated and checked. First audio follows 0.7 s of endpoint silence, the final decider call, and the whole generation. A refused reply doubles it. | measured | 2 and 5 took 3.9 s and 4.4 s |
| "Trailing off" at sentence ends | Not found. Reply audio in the records ends in silence on most turns. The judge could not take audio on its route. | open | 24 |

## Experiments

All on the recorded turn audio, voice path only, with the lookup states the recorded call had.

- **E, one session per call, no call text.** With the earlier-turn list gone and only a one-line lookup status after the
  audio, replies on the second call answered the current turn (measured, `probe1/E`).
- **H, a session per turn with the call as text in the instructions.** Worse: the model repeated lines of that text
  (measured, `probe1/H`). Text about the call, wherever it sits, is answered in place of the audio.
- **Note wording.** A bare status line ("[helper] looking up weather for Seattle") was read aloud. A note that says
  what to do with the state ("Tell them you're checking, in a few words.") was not (measured, `probe2`).
- **Persona wording.** A persona that describes the scene ("a live call with one person") made the model narrate
  itself in the third person from turn 1. The persona kept is `NOTE_PERSONA` in `voice/prototype/run.py`.
- **Item deletion.** llama.cpp answers `conversation.item.delete` with `unknown event type`, so a session is
  append-only and injecting less is the only way to keep it clean (measured, `probe_delete`).

## What changed

- Live mode defaults to `--snapshot note`. The voice session holds the call's audio and replies, plus one short
  private note after the audio on a turn that has a lookup. A turn with no lookup sends no text.
- The session is never restarted and never rebuilt from text. `--call-max-turns` and `--call-keep-turns` are gone.
- llama-server runs three slots with a unified KV pool (`-kvu`), so the voice session can use all of `--lfm-ctx`.
  One slot is held by the call's voice session for the whole call; the other two carry the turn's ASR and the reply it
  generates at the same time. Two slots left one free, and every task ran on it one at a time (measured: all 39 of the
  2026-11-16 17:38 replay's llama-server tasks ran on slot 1, and five turns' final transcripts arrived 27 to 30 s
  after their audio, against 0.03 to 0.28 s in the recorded call's three-slot server).
- The voice session gets a turn's audio once, when the turn ends.
- No fixed line is played. A reply is spoken as the model makes it, and the fact check only logs it
  ([live-fact-check.md](live-fact-check.md)); as of 2026-11-18 nothing is held, cut or asked again.
- The fact check accepts a place the person named without a preposition, and logs a reply that narrates "the
  assistant" or reads the lookup note's own words.
- A general lookup worker exists (`voice/tools/agent.py`) and is off by default. See "Open" below.

## Headroom

Measured by looping the recorded turns into one session until llama-server stopped (`probe_ctx.py`). A turn here
averages 4.4 s of the person's audio, 6.8 s of reply audio and 90 characters of note.

| Voice slot | Turns held | Tokens per turn |
| --- | --- | --- |
| 4,096 tokens | 20 | about 200 |
| 16,384 tokens (the old split of `-c 32768` over two slots) | between 70 and 79 | about 210 |
| 32,768 tokens (`-c 32768 -kvu`, the default now) | 162 | about 200 |

VRAM in use was 3,132 MiB with the model loaded at `-c 32768 -kvu`, of which about 775 MiB was in use before the
server started (measured with nvidia-smi).

When the slot is full, llama-server logs `the conversation does not fit in the context` and then fails an
assertion and exits (measured at all three sizes). A call that reaches the limit therefore ends. Nothing in the
branch handles that: the choice of what to do at the limit is open.

## Open

- **The worker has not run.** Its one live call drew a 429 whose body tripped the billing stop
  (`voice/segmentation/BILLING_STOP`), so no model was measured and the default model id is a guess. With it off, a
  question that needs a fact the model was not given is refused or goes unanswered, and "pick a city" still reruns the
  earlier city.
- **First-audio latency did not improve.** Median speech-end to first audio was 1.62 s in the recorded call and
  1.51 s and 1.77 s in two replays of the fix. The reply is still held until it is fully generated.
- **The limit.** A call past about 160 turns ends when the slot fills.
