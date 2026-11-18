# Spoken acts: how the voice says T3 news

Nothing the voice says about T3 work is a hard-coded string (Mason, 2026-11-18). When he asks how a thread is going, he hears a short answer drawn from the thread's records, not the first two sentences of its last reply.

## The path

1. **Code emits typed acts.** Each act (`voice/t3/acts.py`) is a kind plus the facts that make it true, taken from a seam result, a stored event, or a read. Examples are `confirm_tell{thread, message}`, `done{thread, status, summary}` and `thread_status{thread, status, summary, need, pending, waiting_for}`. Every act keeps the sequences that are its evidence, as before.
2. **The voice's text model words them.** `T3Tasks.say` sends the acts and the call's last three turns to `particle-ai/particle-1:free` (`voice/t3/speech.py`). `$VOICE_RENDERER=local` points the same lane at a local llama-server instead; it failed its eval, so it is not the default ([local-renderer.md](local-renderer.md)). It runs on one lane: one request in flight for the picker, the renderer and the summaries, off the T3 lock, under a short timeout.
3. **Code checks only the named slots and the form.** `acts.check` holds one rule per act:
   - A confirmation or a request to choose between matches comes out as a question. Other acts reject a question mark outside the named slots.
   - Each named slot (a project, a thread title, a message) appears as given, ignoring case, punctuation and first versus second person.
   - The line has at most two sentences per act and is plain speech, with no ellipsis.
   - A need's prompt is context, never a slot. A line for `needs_you`, or a `thread_status` with a need, describes the need in words, from its kind (a command approval, a file change, a question, a link), and fails if it reads out a command, a flag, a path or code.
4. **The decider checks what the line means** (`voice/t3/verify.py`). Liquid's hosted d1.2 checks whether the line is about the right thread and conveys the act. It also checks for unrequested questions, forbidden status claims, and claims the facts do not support. Reply acts carry their run status and skip the separate check for whether the run ended. The line becomes a `RenderedLine` only after both checks pass. The decider runs off the T3 lock with one attempt, a 2 s timeout, and a $1 daily cap. A timeout, an error, or a spend stop fails the line.
5. **The line is spoken word for word** through `say_verbatim`. LFM3 does not reword it, because reworded T3 lines made claims no record supports (integrated run 2, PR #60). Every T3 speaking entry point (`say_verbatim`, the server's `speak_t3`) takes only a `RenderedLine` and raises on a plain string.
6. **Text-only fallback.** If the text model times out, errors, or fails either check, `say_acts` opens a separate LFM3 session with the act facts and wording instructions. LFM3 generates text, which goes through both checks before the voice sends it to speech synthesis. If the fallback also fails, the act stays in the journal. The voice tries again at the next idle point or turn with the earlier refusal in the prompt. A line leaves the journal once the voice sends its full audio. If playback is cut short, the voice retries once while the call is quiet, then waits for the person's next turn. Newer news about the same thread marks an older waiting act `superseded` and takes its place.

## Summaries

A summary is written only when the voice is about to speak about a thread: an answer to a `t3_ask`, a `done`, or a `needs_you`. `Speech.fill` reads the thread through the seam. The status and the pending need come from code. The summarizer gets the last reply and writes a one-sentence gist into the act's `summary` slot. The thread's title is given as a name only, not as evidence of what was done. A thread with no last reply is summarized from its status and need. The gist is cached per thread, read sequence and question, and a speculative gist from words still being spoken serves only a final question with the same words. When the decider marks a `t3_ask` while Mason is still talking, `T3TaskService.sync` starts the read and the summary at once, and the answer after his yes reuses them.

The seam limits what a summary can see. The read carries no tool runs, changed files or errors, and it cuts the last reply at 1,200 characters (`apps/server/src/voiceSeam/outcome.ts`). Giving the summarizer more means changing the seam's read.

## Guarding it

`python voice/ci/check_spoken.py` fails on any string literal on the T3 speaking path that reads as speech, on an act built with wording in a slot, and on a string literal handed to a T3 speaking entry point anywhere under `voice/`, tests aside. Its tests plant each kind of line to show it is caught. On `origin/voice/main` before this change it found 59.

## What the check cannot see

Neither check reads the thread's records, so neither judges whether a summary is true. A gist can overclaim ("it covered the tests" for a thread that was about to write them), and only the eval catches that. The decider's answers are noisy near its 0.5 line: the same request has drawn 0.51 and 0.67 on one question (measured). A borderline right line can be refused, which fails safe, and a borderline wrong line can pass, which the questions per act are there to make rare.
