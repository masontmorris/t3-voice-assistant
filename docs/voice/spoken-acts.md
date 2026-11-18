# Spoken acts: how the voice says T3 news

Nothing the voice says about T3 work is a hard-coded string (Mason, 2026-11-18). When he asks how a thread is going, he hears a short answer drawn from the thread's records, not the first two sentences of its last reply.

## The path

1. Code emits a typed act with the facts and evidence sequences from a T3 result, event, or read. For example, `confirm_tell` carries the thread title and the proposed message.
2. A separate text-only LFM3 realtime session drafts a short line. It gets instructions for the act kind, including an example that treats a thread title as the name of an existing conversation. Set `VOICE_RENDERER=particle-1` for the OpenRouter text model, `VOICE_PAID_TEXT_MODEL` to opt into a paid text model, or `VOICE_RENDERER=local` for a separate local chat server. [Local renderer probe](local-renderer.md)
3. Code checks the draft's form and named values. It rejects known thread or project titles and titles from prompt examples unless the act supplies them. A confirmation must be a question, and a line about a pending need must describe the need without reading out a command, path, or code. Liquid d1.2 then answers yes-or-no questions about the act's meaning, status, and factual claims. Both checks must pass.
4. A private text item hands the draft to the call's existing audio session. The call receives instructions to say drafts as written when the session opens. Those instructions must be in place before the first audio turn, because the realtime server then locks them.
5. The live server holds the call's audio until the complete reply text passes both checks against the same acts. Only then does it send the audio through the ordinary reply transport. A failed check, a reply with no audio, or an interrupted send leaves the act pending for the existing retry path. If the person starts talking while idle news is still being drafted, their turn can start without waiting for the draft. Confirmations that were never heard are asked again before a yes can authorize an action.

## Summaries

The thread picker and the summarizer still use the configured text model. The separate LFM3 writer only drafts what the voice will say.

A summary is written only when the voice is about to speak about a thread: an answer to a `t3_ask`, a `done`, or a `needs_you`. `Speech.fill` reads the thread through the seam. The status and the pending need come from code. The summarizer gets the last reply and writes a one-sentence gist into the act's `summary` slot. The thread's title is given as a name only, not as evidence of what was done. A thread with no last reply is summarized from its status and need. The gist is cached per thread, read sequence, and question. A speculative gist from words still being spoken serves only a final question with the same words. When the decider marks a `t3_ask` while Mason is still talking, `T3TaskService.sync` starts the read and the summary at once, and the answer after his yes reuses them.

The seam limits what a summary can see. The read carries no tool runs, changed files or errors, and it cuts the last reply at 1,200 characters (`apps/server/src/voiceSeam/outcome.ts`). Giving the summarizer more means changing the seam's read.

## Guarding it

`python voice/ci/check_spoken.py` fails on any string literal on the T3 speaking path that reads as speech, on an act built with wording in a slot, and on a string literal handed to a T3 speaking entry point anywhere under `voice/`, tests aside. Its tests plant each kind of line to show it is caught.

## What the check cannot see

Both checks use the act as their source of facts. They do not read the original records behind a summary. A gist can overclaim ("it covered the tests" for a thread that was about to write them), and only the eval catches that. The decider's answers are noisy near its 0.5 line: the same request has drawn 0.51 and 0.67 on one question (measured). A borderline line can pass or fail on different calls. The checks evaluate the reply text, so they cannot detect a difference between the text and the generated audio.
