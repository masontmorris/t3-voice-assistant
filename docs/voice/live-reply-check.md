# Free replies checked against T3, approvals by voice, withdrawn questions

Three behaviours that keep a caller from being left with a wrong belief or an action they cannot take:

- A free reply (the voice model's own words, nothing drafted or checked) that claims something about a thread is checked against T3 and corrected.
- A pending permission request can be approved or denied by voice, with the caller told what an approval would do.
- A question the caller heard and then dropped without answering is not repeated.

Each section says what the mechanism is and what was measured. What is not proven is at the end.

## Check a free reply that claims something about a thread (`voice/live/replycheck.py`)

The reply is spoken as before, with nothing held. When its text is complete (`response.done`), the local d1-2B (the summary gate's server, on a connection of its own) reads one typed choice, `Decider.claims` in `voice/t3/verify.py`: what the reply says about a coding thread, against what T3 holds for the thread this call last spoke about. That is the thread's state and pending request, the end of its last reply, and what the voice itself sent to it or stopped (the seam's read carries one reply and no message history, so the voice's own journal stands for "what was sent"). `unsupported` at 0.5 or more flags the reply. With no thread in view, nothing is checked. The log-only fact check ([live-fact-check.md](live-fact-check.md)) is unchanged and covers days, dates, times, numbers and places.

### When the text is there, and why the check is not started on partial text

Measured on 12 free replies of one recorded live call (event timings only): the last text delta came a median 0.68 s after `response.create` (0.07 to 0.88), `response.done` and the text's `output_text.done` together a median 1.11 s (0.31 to 1.36), and the audio played for a median 6.16 s (1.84 to 8.32). So the complete text exists 0.97 s or more before the audio ends, a median 5.27 s. The last text delta comes about 0.3 s before `response.done` (0.61 against 0.91 s on one reply), but nothing marks it as the last until `output_text.done`. A check on partial text could start about 0.3 to 0.4 s earlier, against a check of about 44 ms and a verdict that lands 0.05 to 0.5 s after the text, so it was not built.

### What the caller hears

| Where the caller is when the verdict lands                                               | What happens                                                                                                                                                                                                                                                                                                                          |
| ---------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| The reply is still playing, the caller is quiet                                          | The rest of its audio is dropped through the barge-in path (`Conversation.stop_playback`, the same call `start_turn` makes), a short holding line is spoken, then the correction                                                                                                                                                      |
| The reply has finished playing, the caller is quiet                                      | The holding line and the correction are spoken as a follow-up                                                                                                                                                                                                                                                                         |
| The caller is talking, the reply was cut off by them, or another turn took over playback | Nothing is spoken over them. The correction waits for a quiet moment, like thread news (`hold_news`), and only if what they heard before the cut still held an unsupported claim: the check runs again on the words that played (the share of the reply's audio heard, as words). A reply cut off before its claim is never corrected |

The correction is read from T3 and, for a cut-off reply, the heard part is judged again: both are awaits during which the caller can interrupt. So playback is read once more after the last await (muted, playing, who owns playback, whether the caller is speaking) and acted on with no await between that read and the action. A correction is never held for a claim that was cut off before it was heard, and playback is never stopped from a measurement taken before an interruption (`ReplyCheck._check`, tested in `test_replycheck.py`).

The correction is a typed act, `correction` (thread, status, what it waits on), worded by the text model, slot-checked and decider-checked, and said in the call voice like every T3 line. The holding line covers the time that takes. It is a free reply to a private note (`run.TRANSITION_NOTE`) that carries only the meaning, so the voice words it each time. If the correction is already ready when the flag lands, nothing changes: it never is, because the correction's draft starts at the flag, so the holding line is always said first and the draft is made while it plays (`say_acts(lead=...)`). The holding line is not itself checked against T3, and the voice has made up a fact in it once in 12 trials of one wording (below).

### The holding line's wording (GPU, 12 trials each, fresh session, one audio question then the call's own free reply, then the note)

| Note                                                                                                              | Followed the note | Said something else                                                                       |
| ----------------------------------------------------------------------------------------------------------------- | ----------------- | ----------------------------------------------------------------------------------------- |
| Only asks for the holding line                                                                                    | 0 of 12           | 12 of 12 answered the audio again, with a new claim about the thread                      |
| States the situation ("your last reply about their thread was wrong, take it back, say you need to double check") | 12 of 12          | 6 of 12 asked a question and 2 more offered help; 1 of 12 made up a fact about the thread |
| The same with "say nothing else about the thread, and ask them nothing" (shipped)                                 | 12 of 12          | 1 of 12 asked a question, 3 of 12 offered more help                                       |

The first note's failure is the voice model answering the newest audio. The relay's "what they just said is already handled, do not answer it" did not fix it: no reply said it was taking anything back (most answered the audio again, 3 of 12 said they were checking the thread). In the rehearsal the shipped holding line asked a question in 2 of 3 corrected replies ("What should I do next?"). That is open.

### Numbers

On made-up cases (`voice/t3/eval/claim_cases.py`, scored from `voice/t3/fixtures/claims.json`, the local d1-2B):

| Set                                                        | Wrong replies flagged | Replies that were not wrong, flagged |
| ---------------------------------------------------------- | --------------------- | ------------------------------------ |
| Seen (20, the prompt and the 0.5 bar were chosen on these) | 7 of 10               | 0 of 10                              |
| Held out (20, written before its first run)                | 8 of 9                | 1 of 11                              |

These are from the committed recording, made on the CPU. A first recording, on the GPU, had 8 of 10 and 0 of 10 on the seen set and 8 of 9 and 2 of 11 on the held-out set. It was redone because some made-up cases were reworded, and because the GPU was held by another job. On the 35 requests that did not change, CPU and GPU unsupported shares differ by at most 0.022 (mean 0.005), and one case moves across the bar (a time of day: 0.51 on the GPU, 0.49 on the CPU), which is the difference between 2 and 1 held-out false flags. The held-out set was in effect scored twice. The held-out false flag is a joke about wishing it could clear a request (0.58). A false flag interrupts a correct reply and then says it was wrong, so this is the error that matters.

The missed wrong replies were a vague "it's done" (0.43), "I stopped it for you" (0.31) and a reply that the thread would carry on (0.46) on the seen set, and "I passed your follow-up along to it just now" (0.25) on the held-out one. The first prompt (two yes/no questions) flagged 0 of 10 wrong replies on the seen set at its natural bar; the choice question with the stricter context was the second arm tried. A check takes a median 40.6 ms (p95 45.5 ms) on the 40 recorded cases, and a median 43.5 ms (max 68 ms) over the 59 rehearsal checks.

On the rehearsal (`python -m voice.live.rehearse drive --scenario claims`, the real server, five calls, scripted T3, `--decider d1`; the free replies below were read by hand): 59 free replies were checked, 19 had no thread in view and were not, 38 were not flagged and 2 were. Of the replies I read as unsupported (about 5) the check flagged 2 and missed 3 at 0.44, 0.37 and 0.19. The two flags were corrected after the caller had heard 1.07 s of a 7.28 s reply and 0.98 s of a 5.84 s one (the verdict lands 0.05 to 0.19 s after the text), so the correction beat the end of playback 2 of 2 and about 15% of the audio had played. Flag to first audio of the holding line: 0.25 and 0.26 s (2 samples). Flag to first audio of the correction: 2.05 s once, and once the correction line was refused twice and never spoken (see unproven). The verdict lands a median 0.095 s after the text (p90 0.50 s).

First audio on ordinary chat turns, from speech end, measured on the same chat lines across the three rehearsal scenarios: unchanged code 1.321 s median (p90 1.604 s, 67 turns), this code 1.344 s (p90 1.701 s, 57 turns). The check starts after the text is complete, so it cannot delay first audio; the 23 ms is the spread of two runs, not shown to be an effect.

## Answer a pending permission request by voice

`respond` is a seam operation (`apps/server/src/voiceSeam/respond.ts`): it answers one pending approval of a visible thread with T3's `runtime-request.respond` command, the web client's own path (`respondToThreadApproval`), as `accept` or `decline` and never "for the session" or "always". The request id the voice mints fixes the command and a retry answers from its receipt. The request must still be pending on that thread and be an approval: a question, a card and a request only the app can take are refused. It is off unless the seam's config lists `respond` in `operations` (the sandbox's does; a real `voice-seam.json` does not until its owner adds it).

**A replay is bound to its payload.** A receipt does not hold what was asked, so a replay is checked against T3's own record: it answers as the first call did only when the named request was resolved with this call's decision. The same request id with another request or another decision is refused, and so is a concurrent copy that the orchestrator replayed from another payload's command (`respond.test.ts`).

**The caller is told what an approval would do.** T3 stores a prompt or a reason for most approvals, but not all. The seam says which in the thread read (`pendingDetailed`, true only when T3 stored a prompt, a reason or a question for the request; a seam that does not say is taken as false). The confirmation then carries a description of this request, written by the thread summariser from the thread's own records with the last reply left out and asked "what would approving this request let the thread do?", and gated by the summary gate (the local d1-2B's check of the gist against the request, `GATE_QUESTION_PENDING`). The line's check refuses a confirmation that leaves the description out, the way a status line that drops its summary is refused (`a1_summary`). Never a command line, flag, path or code.

Approving is refused for voice, and the voice says through a typed act (`needs_the_app`) that this one has to be approved in the app, when T3 stored nothing that says what the request does, or when the description is missing or fails its gate. Nothing is asked then, so there is no yes to give. Denying is allowed without a description: nothing runs when a request is denied, and the confirmation names the thread. That was a choice.

The decider chooses with two tools, `t3_approve` and `t3_deny`, offered only while a thread waits on the person for an approval, and told which (`T3Registry.approvals`). A yes sends the answer once, and the line afterwards says what T3 returned: its answer, its refusal, or that it could not be reached. A yes never answers a request it did not name: it re-reads the thread, and if the request changed (to another approval, to a question, or to nothing) it asks about the new one or says so. After a restart an unanswered confirmation is never replayed.

More than one pending request: one is answered at a time. With several threads waiting the voice asks which, offering only the threads that wait; with one, there is nothing to choose; a thread's read carries one pending request, and the next is asked about afresh. A thread whose title another thread shares is not offered (the voice says so, as for a stop). "Approve all" is not supported.

Tool choice on 26 made-up cases (`python -m voice.t3.eval.tool_choice`), once per decider:

| Decider                                                          | Approve | Deny   | Status question | Chat, claim-bait, withdraw | Approve or deny said with nothing waiting |
| ---------------------------------------------------------------- | ------- | ------ | --------------- | -------------------------- | ----------------------------------------- |
| Hosted d1.2 (the calls' decider)                                 | 5 of 5  | 4 of 4 | 4 of 4          | 11 of 11                   | 2 of 2                                    |
| Local d1-2B, from the final rehearsal's picks (not the 26 cases) | 3 of 4  | 0 of 1 | 4 of 4          | not counted                | not run                                   |

Without the sentence added to the request criterion in `voice/segmentation/hybrid.py` (`T3_APPROVAL`), hosted routed "Go ahead and allow it." to `t3_approve` and `t3_ask` both (4 of 5 approve), so it stays; it, and the examples in the two tool lines, were first added for the local decider and were not ablated one at a time on the hosted one. Putting the waiting list in the shared decider state moved the local d1-2B's read of ordinary status questions to chat (5 of 5), so it is not there.

The confirmation line is checked by the local d1-2B for claiming the request is already approved. The first wording read "Should I approve X running its tests?" as a claim at 0.61 and refused it twice. With the question reworded ("as something that has happened") and the bar for that one claim at 0.75, on 8 made-up lines (4 good confirmations, 4 that claim an answer) 7 were right: no good line refused, and one false "I've approved X running its tests, right?" passed (it still needs a yes to act). That sample is too small to set a bar on.

Rehearsal (`--scenario approve`, five calls, local d1-2B as the decider, scripted T3), the answers the scripted T3 received: unchanged code 0 of 5 calls, with free replies such as "I'll proceed with the approval"; the code before the description requirement 3 of 5 (calls whose approve was picked and confirmed), each answered with the right decision. The description requirement was added after that run and was not rehearsed: the confirmation line now also carries a description, and tests cover it with scripted summaries only. Not working on the local decider: a deny read as chat and answered "I will not allow that" in free text (and was not checked, because the status line of that call had been refused, so no thread was in view), and "I approve." with two threads waiting read as a status question.

## A withdrawn question stops being asked

The voice session holds a relayed question's words as plain history, and llama.cpp cannot remove an item. A turn that drops a question without a line can have the voice model answer freely and list the candidates again. A turn that drops a question the caller had heard, without answering it, now says so to the voice model once, as a private note after the audio (`WITHDRAWN_NOTE`): they did not answer your question and moved on, answer what they said now, do not ask it again. The note names no choice. `T3Tasks.take_withdrawn` reports it once (an answered question, a question nobody heard, and one from another call do not).

Not shown to help: the repeat did not reproduce on the unchanged code in the rehearsal (`--scenario withdraw`, five calls): 0 of 15 free replies after "never mind" named an offered thread. With the note, 4 of 4 dropped questions sent it and 0 of 15 named one, so there is no before and after. The one case where the voice asked "which thread" again after a "no, never mind" was a question that had never been heard, which the design asks again on purpose.

## The `same_title` stop line

A stop for a title two threads share is refused and the voice says so. Its line was refused twice ("call reply missing audio or incomplete"). Not the third-person "they" in the draft: the voice opened its reply with a refusal of its own ("I can't help", "There are multiple threads") on that line in any person, and the response stops at its first word off the line (`relay_eval --set same_title`, 8 trials of each): "which one they mean" 1 of 8 recited first and 4 of 8 on the retry, "which one you mean" 1 and 4, the example's own shape 2 and 5. A line that opens on what was done ("I didn't stop anything, because more than one thread is called X.") recited 8 of 8, one that opens "I can't tell which X thread to stop" 8 of 8, and "Two threads share the name X, so I didn't stop either one" 8 of 8, first relay and retry (`--set same_title_forms`). The writer copies the shape of its example, so the example is the first, and the direction to the writer no longer says "they". The repo's copies of these lines carry a made-up title and were not run again after it was changed.

## The notes' length instruction

The holding line for a running lookup ended on "in a few words", which the voice once said as content. The lookup, result and dropped notes, the ask-instead note and the interrupted note ended on a length instruction too. They no longer do; the persona already asks for one or two short sentences. Not reproduced: `note_eval` (12 trials per note, a fresh session) had no reply say a length phrase on the old wording either (0 of 12 each), and the median words per reply were 6, 16 and 7 for the old notes and 7, 15 and 9 for the new, so the change is unproven.

## Unproven

- The correction line was refused twice and never spoken in one rehearsal (the writer's two drafts failed the check), so that turn's wrong claim was left standing and given back for a quiet moment. Not diagnosed.
- A withdrawn question's repeat did not reproduce on the unchanged code, so the note's effect is not measured; only that it fires on each dropped question.
- The check misses wrong replies (about 3 of 5 in the rehearsal, 1 of 9 and 3 of 10 on the made-up sets) and falsely flags some (1 of 11 held out on the CPU recording, 2 of 11 on the GPU one).
- Approve and deny were exercised against a scripted seam and a fake T3 service only, and the TypeScript operation with scripted services (`respond.test.ts`), not against a live T3 with a pending approval. The description requirement was not run end to end with a real summariser.
- A reply that claims something about a thread with no thread in view is not checked.
- Rehearsals ran the local d1-2B as the call's decider, which is not what calls use; the hosted decider was measured on tool choice only.
