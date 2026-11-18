# Spoken acts: how the voice says T3 news

Nothing the voice says about T3 work is a hard-coded string (Mason, 2026-11-18). When he asks how a thread is going, he hears a short answer drawn from the thread's records, not the first two sentences of its last reply.

## The path

1. **Code emits typed acts.** Each act (`voice/t3/acts.py`) is a kind plus the facts that make it true, taken from a seam result, a stored event, or a read. Examples are `confirm_tell{thread, message}`, `done{thread, status, summary}` and `thread_status{thread, status, summary, pending, waiting_for}`. Every act keeps the sequences that are its evidence, as before.
2. **The voice's text model words them.** `T3Tasks.say` sends the acts and the call's last three turns to `particle-ai/particle-1:free` (`voice/t3/speech.py`). It runs on one lane: one request in flight for the picker, the renderer and the summaries, off the T3 lock, under a short timeout.
3. **Code checks only the named slots.** `acts.check` holds one rule per act:
   - A confirm act comes out as a question.
   - Each named slot (a project, a thread title, a message, a prompt) appears as given, ignoring case, punctuation and first versus second person.
   - The line makes no status claim the act forbids, such as "finished" for a running thread or "stopped" before a stop is confirmed. A negated claim ("it hasn't stopped yet") is not a claim.
   - The line has at most two sentences per act and is plain speech.
4. **The line is spoken word for word** through `say_verbatim`. LFM3 does not reword it, because reworded T3 lines made claims no record supports (integrated run 2, PR #60).
5. **No canned fallback.** If the text model fails, times out, or fails the check, LFM3 words the acts from a note, and its reply is held whole until the same check passes it (`say_acts` in `voice/prototype/run.py`). If that fails too, nothing plays. The act stays in the journal and is worded again at the next idle point or turn, with the earlier refusal in the prompt.

## Summaries

A summary is written only when the voice is about to speak about a thread: an answer to a `t3_ask`, a `done`, or a `needs_you`. `Speech.fill` reads the thread through the seam. The status and the pending need come from code. The summarizer gets the last reply and writes a one-sentence gist into the act's `summary` slot. The gist is cached per thread and read sequence. When the decider marks a `t3_ask` while Mason is still talking, `T3TaskService.sync` starts the read and the summary at once, and the answer after his yes reuses them.

The seam limits what a summary can see. The read carries no tool runs, changed files or errors, and it cuts the last reply at 1,200 characters (`apps/server/src/voiceSeam/outcome.ts`). Giving the summarizer more means changing the seam's read.

## Guarding it

`python voice/ci/check_spoken.py` fails on any string literal on the T3 speaking path that reads as speech, and on an act built with wording in a slot. Its test plants five kinds of line to show each is caught. On `origin/voice/main` before this change it found 59.

## What the check cannot see

The check never judges whether a summary is true. A gist can overclaim ("it covered the tests" for a thread that was about to write them), and only the eval catches that. The check also cannot tell when a named slot is unfit to say aloud, such as a long shell command in an approval's prompt.
