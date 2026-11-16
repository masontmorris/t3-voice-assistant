# Live demo fix: conversation is not a task

Mason's live demo of 2026-11-16 turned conversation into tasks. LFM3 heard "Okay, this is bad." and "What do you understand?" at the end of an utterance and called `delegate`, the mock worker "completed" the task, and the reply narrated it ("The task has been marked as completed..."). The goal is that LFM3 creates or delegates a task only for an actual request for something to be done or looked up, and answers everything else in conversation. Mason's design is unchanged: the task gate runs during speech, there is no decider call at the end, and LFM3's calls at the end are final.

## What changed

Three changes, all in what LFM3 is told or given at the end of the utterance. No decider call was added and no LFM3 call is re-gated.

1. **The tool session's instructions define work and conversation.** Work covers a fact the user wants to know, as a question or a wish, and something they want done. Conversation covers a question about the assistant, this conversation, or what it heard; an insult or a reaction; thanks; a greeting; and a statement of fact. The delegate tool description says the same.
2. **The snapshot carries what the decider read.** When the segmenter's last decision point classified any speech, the snapshot adds `decider: {"read": [{"text": ..., "kind": ...}], "contains_request": bool}`. This costs no decider call. It covers only the words the decision point read, so a request in the unreviewed tail is still possible.
3. **A no-task turn gets the transcript, not the note.** When the snapshot has no task and no clarification, the voice session receives the user's final transcript as a plain message instead of the JSON snapshot. The voice session already heard the audio; the plain text stops it from describing the note. With a task, it still receives the JSON snapshot.

The built-in persona was rewritten to speak in the first person about the user, never read the note out, and answer a no-task turn directly.

## Test method

All runs used the GPU lease, `quiet_run.py`, and no priority. The 10 test utterances were rendered by LFM3's own `/tts` (seed 11, female and male voices, 0.3 s lead and 0.7 s tail) and run through `voice.prototype.run` on the gated turn. Six are non-requests and four are requests, as the brief lists. A fifth request, "I wonder whether the coffee shop is open yet.", was rendered as a development clip for the implicit case and is not one of the ten.

Every decider request came from the cache except 5 live attempts, all HTTP 200 with a `:free` model id and no 429, against this round's cap of 80. The prompt and tool text were frozen on the synthetic clips before Mason's clips ran.

## Non-requests

Measured on the final text (`run-synthetic-8`). No task was created for any of the six. One call failed: a `run_task` with the invented id `acknowledge_bad_statement` on the first clip. The task service rejected it and the snapshot stayed empty.

| Utterance | Task | Reply |
| --- | --- | --- |
| "Okay, this is bad." | none | "It seems like you are expressing a negative sentiment. Could you please provide more details or clarify what you need help with?" |
| "You're dumb." | none | "I didn't hear what you said. Can you repeat yourself?" |
| "What do you understand?" | none | "I understand the question." |
| "Can you hear me?" | none | "Yes, I can hear you." |
| "That's not what I meant." | none | "It seems there was a misunderstanding. Could you clarify what you meant?" |
| "Thanks." | none | "You're welcome! Let me know if there's anything else I can help you with." |

The control, the tool text from `voice/main`, delegated four of these six (all but "You're dumb." and "Thanks.") in the same harness: measured in `lab-1`.

## Requests

Measured on the final text (`run-synthetic-8`). All five delegated a task with the right content. The ASR heard "Said a reminder for Wednesday at 3" for the reminder clip; the delegate instruction was "Remind me for Wednesday at 3".

| Utterance | Task created | Reply, first then the task update |
| --- | --- | --- |
| "What's the weather in Denver tomorrow?" | "What's the weather in Denver tomorrow?" | "What's the weather in Denver tomorrow?" then "The forecast for Denver, Colorado on Tuesday, November 17 is a high of 69 degrees Fahrenheit with a 0 percent chance of precipitation." |
| "What time is it in Tokyo right now?" | "What time is it in Tokyo right now?" | "The task service snapshot indicates that the query ... is currently running. However, it does not provide the specific time." then "The task service update confirms that the time in Tokyo is 11:44 PM on Monday, November 16." |
| "Email a summary of last week's sales report to Dana." | the same words, side effect | "I will email a summary of last week's sales report to Dana." then "The task of emailing the sales report to Dana is completed. The mock file search found Sales_Week45.xlsx." |
| "Set a reminder for Wednesday at three." | "Remind me for Wednesday at 3" | "I set a reminder for Wednesday at 3." then "The task is complete, but no real work was done." |
| "I wonder whether the coffee shop is open yet." (development clip) | the user's words | "The coffee shop is running." then "The coffee shop is open." |

The first two replies are weak. The weather reply repeats the question, and the time reply names the note. Both were already in the failure list before this change (measured, `$VOICE_HOME/evidence/eou/gate-and-reply-sample.md`). They are not fixed here.

## Mason's clips

The same frozen build ran Mason's 11 recordings and Stage 1's 4 synthetic clips. The scored set is 14 clips and 26 expected tasks, as in PR #12. Numbers come from `voice.prototype.eou_report` on the new run and the gated baseline.

| Measurement | New | Gated baseline |
| --- | --- | --- |
| Expected tasks correct | 18 of 26 | 18 of 26 |
| Clips passed | 7 of 14 | 7 of 14 |
| Premature dispatches | 0 | 0 |
| Extra tasks | 3 | 4 |
| LFM3 tool calls | 14 | 14 |
| Tasks LFM3 decided | 11 | 14 |
| Decider calls per utterance, mean | 2.36 | 2.36 |
| First reply audio after the audio ends (median / p95 / max) | 0.64 / 0.98 / 0.98 s | 0.62 / 0.86 / 0.86 s |

The per-clip composition moved. The new run gains 05's second request and swaps which 09 request passes; it loses nothing that the baseline had except in that swap. `06_not_a_request`, the only pure non-request clip in the set, stayed at 0 of 0 expected tasks; LFM3 made a failed `run_task` with an empty id, and no task was created.

The 11th recording, `11_tools`, is not in `expected.json`. LFM3 delegated the whole two-part utterance as one task; the reply gave the weather and did not mention Tokyo. That is a measured miss, and it matches the re-delegate-the-whole-utterance failure PR #12 describes.

## What this does not fix, and what to watch

- **Small prompt changes flip LFM3's calls.** In the variant lab, single-sentence edits moved a non-request from no call to `delegate`, or dropped a request, on the same audio (measured across 11 texts). The shipped text passed its frozen set twice, in-process and in the prototype process. Treat it as frozen; re-test before editing it.
- **The fix is LFM3's judgment, not a gate.** If LFM3 declines to delegate a real request, the voice answers conversationally and nothing retries it.
- **A no-task turn never mentions a task, but a task turn can still narrate the note** (measured: the Tokyo reply above).
- **The `decider` field covers only speech a decision point read.** On short utterances the decider often reads nothing, and there the tool instructions carry the whole decision.
- **The mock completion text is still spoken for real tasks** ("no real work was done"). That is the mock worker, not a real one.

## Files

- `voice/prototype/run.py`: the tool instructions, the persona, the delegate description, and the transcript-not-note injection.
- `voice/prototype/tasks.py`: the `decider` field in the snapshot.
- `voice/prototype/segmenter.py`, `voice/segmentation/hybrid.py`: the segmenter's read summary.
- `voice/prototype/tests/test_tasks.py`, `voice/segmentation/tests/test_hybrid.py`: the new tests.

Run outputs, audio and the variant lab stay under `$VOICE_HOME/evidence/live-requests/`, out of the repo.
