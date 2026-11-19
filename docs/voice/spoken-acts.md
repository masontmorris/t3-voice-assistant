# Spoken acts: how the voice says T3 news

Nothing the voice says about T3 work is a hard-coded string (Mason, 2026-11-18). When he asks how a thread is going, he hears a short answer drawn from the thread's records, not the first two sentences of its last reply.

## The path

1. Code emits a typed act with the facts and evidence sequences from a T3 result, event, or read. For example, `confirm_tell` carries the thread title and the proposed message.
2. A separate text-only LFM3 realtime session drafts a short line. It gets instructions for the act kind, including an example that treats a thread title as the name of an existing conversation. Set `VOICE_RENDERER=particle-1` for the OpenRouter text model, `VOICE_PAID_TEXT_MODEL` to opt into a paid text model, or `VOICE_RENDERER=local` for a separate local chat server. [Local renderer probe](local-renderer.md)
3. Code checks the draft's form and named values. It rejects known thread or project titles and titles from prompt examples unless the act supplies them. A confirmation must be a question, and a line about a pending need must describe the need without reading out a command, path, or code. Liquid d1.2 then answers yes-or-no questions about the act's meaning, status, and factual claims. Both checks must pass.
4. A private text item hands the draft to the call's existing audio session, which says it. The call receives instructions to say the item's words and nothing else when the session opens, with no announcement, lead-in or closing line of its own. Those instructions must be in place before the first audio turn, because the realtime server then locks them.
5. The live server holds the call's audio until the complete reply text is the draft's words: the same words, up to case, punctuation, markdown and contractions, with no person flip, so "I asked" is never "you asked". The draft already passed both checks, so the same words need no second decider call, and any other words are not the checked line. The turn stops the response at the first word that leaves the draft, so a reply that goes off the words leaves only its start in the session: llama.cpp keeps everything a response generated and has no event that removes an item (`server-realtime.cpp`: a cancel ends the response at the token it reached, and the dispatch accepts only session, audio-buffer, item-create, response-create and response-cancel events). No note about the held reply is sent, because the one that told the model to set it aside was read as the newest text and answered. A reply that fails the check, has no audio, or is cut off leaves the act pending for the existing retry path, and the turn says nothing else about it: a turn with a T3 line has that line as its only reply. If the person starts talking while idle news is still being drafted, their turn can start without waiting for the draft. Confirmations that were never heard are asked again before a yes can authorize an action.

A yes authorizes one prompt. The question the voice asked belongs to the call it was asked in, and only a prompt whose audio was sent (`T3Tasks.spoke`) can be answered. The next person turn answers it or drops it: an unrelated reply, a turn that ends without answering, another line said after it, a hang-up, a reconnect and 90 s since it was heard (`CONFIRM_TTL_S`) all clear it, and a question is never asked a second time on the strength of a reply that answered nothing. A prompt the person never heard comes back to be asked again and acts on nothing. When a second prompt replaces the first, only the last one can be answered.

Reading the answer: code's own table is the floor (a no word is a no), and where it reads nothing the decider is asked what the reply does (`Decider.reads`, voice/t3/verify.py): yes, no, a question, another target, or nothing. A yes acts only when it is unconditional: the label is yes at 0.7 or more and no real share (over 0.2) goes to a question or to another target, so "yes, but in the other project" and "yes, only after the tests pass" are "another target", never a yes. A score that is not a number from 0 to 1 is no reading at all. A rising "please do" that a transcript ends with a question mark still confirms, with no new phrase in the table. On the recorded replies (`voice/t3/fixtures/reply_reads.json`, d1.2) 23 of 26 yes replies clear the line, and none of the 36 others does, among them 10 conditional or retargeting yeses; "okay" and "go ahead" still confirm through the table. The decider is a network call, so it is asked off the T3 lock, and the question's identity is checked again when the answer is back.

The launch carries everything the person said in the turn that asked, not the fragment that asked: the question is asked once, and its request is bound to the turn's final transcript.

When a launch names no project, the voice asks rather than states: with one project available it asks whether to start the thread there, and a yes starts it with the original request; with several it asks which one, naming them; with none it says so. Both questions are the ordinary pending ones, so a different project named in the answer, a no and an unrelated reply are read as they are for any launch confirmation.

## The relay wording, measured

`python -m voice.t3.eval.relay_eval` sends 22 call-shaped relays per wording (two chat turns, a long hesitant request as audio, then the note) and scores each reply with the check the live turn uses. Measured on 2026-11-18, GPU, synthetic voice: the wording that shipped on voice/main said the draft's words and no others on 13 of 22 and, on a second run, 15 of 22 (run-to-run spread of 2); with four examples 20 of 22; with no examples 7 of 22; with one example 21 of 22. The failures of the old wording were the call's own: "The draft is: ...", a lead-in sentence, an appended closing line, and a reply that ignored the draft. The remaining failures of the shipped wording swap the draft's names for the person's request, which the exact-words check holds. A second pass sends 12 requests and counts ordinary replies that carry the relay's words: the old wording leaked an example line in 2 of 12, four examples in 2 of 12, one example in 0 of 12. A one-example persona is also why the voice stopped reading an example line back as its own reply in that pass; it still did once in 30 rehearsal turns (measured, `Should I stop Example Split Export Files?` as the answer to a bare greeting), so the example stays the first thing to remove if it recurs.

The checker needs what a thread-less act means. Launch acts carry no thread, and the decider's unsupported-claim question answered yes to "Should I start a new coding thread in Voice sandbox?" in 3 of 3 calls, so each such draft waited. The request is no longer a slot of `confirm_launch` and `launched` either: the writer copied its reader example's request instead of a 40-word hesitant one. An act with no thread now sends its meaning to the checker as `says` (voice/t3/verify.py).

## Starting a thread by voice, measured

`python -m voice.live.rehearse` is the score: threads started, of calls shaped like the 2026-11-18 one (two chat turns, a long hesitant request, a yes, a question about the thread, a yes), through the real live server with a scripted T3. Calls 1 to 5 are the visible half; 6 to 10 and 11 to 15 are two held-out halves, each written once. Measured on 2026-11-18, GPU, synthetic voice:

| Run | Visible | Held-out A | Held-out B | First breaks |
| --- | --- | --- | --- | --- |
| Baseline, the code of the first PR | 2 of 5 | 0 of 5 | not written | request not dispatched 4, launch draft refused 2, relay held 2, started 2 |
| After the dispatch, writer, one-confirmation, decider-yes and relay-note fixes | 4 of 5 | 3 of 5 | not run | request not dispatched 3 |
| After the dependency and correction wording | 5 of 5 | not re-run | 5 of 5 | none |

The dispatch cause: the decider's readiness question called a long hesitant "start a thread" request ambiguous because it names no project (15 of 30 replayed draws dispatched, 27 of 30 with `T3_READY`), and its dependency and correction questions read the fragments one request is cut into ("for that", "the part that writes slots") as needing or replacing each other (17 of 24 draws dispatched, 23 of 24 with `T3_DEPENDS` and `T3_CORRECTS`; `voice/t3/eval/dispatch_probe.py`). The first held-out half was looked at for causes before the dependency fix, so the second half is the clean one.

Smaller causes found on the way: the writer copied its example's reply with the example's project beside a long start request, 6 of 10 drafts, and with the new record labelled "New record" 0 of 10 (CPU); drafts asked for while two writer sessions were closing failed with "busy" instead of waiting; one request cut into several launch segments asked its confirmation more than once; the relay answered a one-word yes with "Great!" (2 of 12 relays, and 8 of the 10 held relays in the baseline), and with "What they just said is already handled" in its note 0 of 12 (GPU).

The person is never flipped in a line: the writer quotes a person's message as they said it, and the check no longer turns "my" into "your".

## Summaries

The thread picker and the summarizer still use the configured text model. The separate LFM3 writer only drafts what the voice will say.

A summary is written only when the voice is about to speak about a thread: an answer to a `t3_ask`, a `done`, or a `needs_you`. `Speech.fill` reads the thread through the seam. The status and the pending need come from code. The summarizer gets the last reply and writes a one-sentence gist into the act's `summary` slot. The thread's title is given as a name only, not as evidence of what was done. A thread with no last reply is summarized from its status and need. The gist is cached per thread, read sequence, and question. A speculative gist from words still being spoken serves only a final question with the same words. When the decider marks a `t3_ask` while Mason is still talking, `T3TaskService.sync` starts the read and the summary at once, and the answer after his yes reuses them.

The seam limits what a summary can see. The read carries no tool runs, changed files or errors, and it cuts the last reply at 1,200 characters (`apps/server/src/voiceSeam/outcome.ts`). Giving the summarizer more means changing the seam's read.

## Guarding it

`python voice/ci/check_spoken.py` fails on any string literal on the T3 speaking path that reads as speech, on an act built with wording in a slot, and on a string literal handed to a T3 speaking entry point anywhere under `voice/`, tests aside. Its tests plant each kind of line to show it is caught.

## What the check cannot see

The draft checks use the act as their source of facts. They do not read the original records behind a summary. A gist can overclaim ("it covered the tests" for a thread that was about to write them), and only the eval catches that. The decider's answers are noisy near its 0.5 line: the same request has drawn 0.51 and 0.67 on one question (measured). A borderline line can pass or fail on different calls. The reply check compares text, so it cannot detect a difference between the text and the generated audio.
