# Answering a thread's question by voice

A coding thread can stop and ask the caller something: one or several questions, each with options and sometimes room for the caller's own words. The voice tells the caller the question and every option, takes the answer by voice, says back what it is about to give and to which thread, and gives it to T3 only on a yes. A wrong or stale answer steers a coding agent on a spoken word, so each step below refuses where it could have guessed.

Approvals are the neighbouring path ([live-reply-check.md](live-reply-check.md)). This one reuses its shape: a seam write idempotent by request id, a confirmation that names what will be sent, and a yes that answers only the request it named.

## The seam

`apps/server/src/voiceSeam/answer.ts` serves the wire operation `answer_question`, whose seam-config name is `answer`. It is off unless the config's `operations` lists `answer` (listing `respond` does not turn it on: an answer reaches the agent as the user's words, so it has its own switch). Its input is the thread, the pending request id (`pending.requestId` of a read) and one answer per question, `{questionId, kind: "option" | "own_words", text}`. It answers with what T3 recorded for each question.

- **Only what is still pending, on a visible thread.** The request must be pending on the thread and be a user-input request the voice can answer (below). A thread outside the caller's projects is `not_found`; an approval, a card and a request that cannot be resumed are refused.
- **The ceiling.** A thread running above the seam's `maxRuntimeMode` is refused (`above_ceiling`) before any command, and the command carries the ceiling as T3's per-command mode limit, which the orchestrator rechecks under its lock, as for `send`. `respond` has no such check: approving starts no work beyond what the thread runs at. An answer is a message to the agent, and in a message-mode request T3 dispatches it as a user message.
- **Validated against the request.** Every question must be answered once, each question id must exist, an option must be a label the question offers, and the caller's own words only go to a question that takes them (`allowCustomAnswer` is not `false`) and no longer than 500 characters. An option goes in as the web client sends it: its `value`, else its label.
- **Idempotent by request id, bound to its payload.** The request id fixes the command id. A receipt does not hold the answers, so a replay is checked against T3's record of the resolved request: it answers only when T3 stored exactly the answers this call would send. The same request id with other answers, or for another request, is `invalid_request`, and so is a concurrent copy that the orchestrator replayed from another payload's command.
- **The outcome comes from T3.** After a fresh answer the seam reads the resolved request back and reports its stored answers. An answer T3 does not show as recorded as sent is `unavailable`, never reported as given.

### What crosses

A pending question carries `questions` (id, text, options with label and description, and whether own words are taken) only when the voice can ask and answer all of them well. Otherwise it is relayed as before (`prompt`, no options) and the voice says it has to be answered in the app. A request is answered in the app when it has more than 4 questions, a question of more than 300 characters, more than 6 options, an option label of more than 80 characters, a duplicate or empty label, no option and no room for words, or any multi-select question. Descriptions are cut at 160 characters and are context for the choice, never read out.

**Multi-select goes to the app.** The decider chooses one label among the options with shares. A set read from speech ("the first two", "everything but Postgres") has no such choice to score, and a wrong set is a wrong answer to the agent. One call per option would give independent yes or no shares with no measured bar. So the voice does not take a multi-select question, and says so.

## On the call

1. **Telling it.** `need_act` gives a question with options as an `ask_question` act (question, every option, whether own words are taken, `number` of `count` for a request with several questions). The writer words it and the line check refuses a line that leaves an option out, as it refuses one that leaves out a thread candidate. A question without options is relayed as before.
2. **Taking the answer.** The decider's `t3_answer` tool is offered only while a thread waits on a question the voice can ask, with its text and options in the tool's line. A turn that picks it reads the thread, then one typed decider choice (`Decider.chooses`, voice/t3/verify.py) decides which option the caller's words choose: each option is a label with its description, with `own_words` (only where the question takes them), `no_answer` (it asks something else or declines) and `unclear`. An option or the caller's own words counts only at 0.7 of the choice or more. No pattern or word list reads the reply.
3. **Not sure.** A reply that fits no option, or that the decider is unsure of, asks the question again once with the options, and the second miss says it has to be answered in the app. A reply that answers nothing drops the question. With no decider, nothing is chosen. Words of more than 240 characters are too long to say back and are not sent.
4. **Several questions.** They are asked one at a time, and nothing is sent until the last is answered and the whole set is confirmed back.
5. **Confirming.** A `confirm_answer` act names the thread and every answer. Only a yes (read by the same typed reading that reads every confirmation) sends it, in one command.
6. **A yes answers only what it named.** It re-reads the thread. If the request is another one, or has other questions or options, nothing is sent and the question is asked again from the start (`restated: changed`); if the thread waits on nothing, or on something that is not a question, the caller is told; the seam then refuses a request that was answered between that read and the send.
7. **Reporting.** `answered` names the answers the caller confirmed, after the seam reported T3 recorded the same ones. A refusal is said as T3's, and a dead seam as unreachable. After a restart an unconfirmed answer is never sent.

The answer tool is a side effect whatever the decider says (`T3_ANSWER`, voice/segmentation/tasks.py): it dispatches only on the final transcript, never on words still being spoken.

## Measured

Hosted d1.2, the decider calls use, unless a row says otherwise. Every case is made up; no call content is in the repo.

**Tool choice** (`python -m voice.t3.eval.tool_choice liquid answer`, one run, 25 cases, nothing tuned on them). With a question waiting, an answer by name, by place, in other words or in the caller's own words reached `t3_answer` in 10 of 10, and in 3 of 3 with two threads waiting on questions. A status question reached `t3_ask` in 2 of 2, and chat, claim-bait and withdrawals reached nothing in 5 of 5. "Approve" or "deny" said with only a question waiting reached neither in 2 of 2 (the decider read "I approve." as an answer, which the next step treats as no answer). An answer-shaped reply with nothing waiting, or only an approval, never reached the answer tool in 4 of 4. In a live rehearsal "Go with the second one." with two threads waiting on questions reached `t3_ask` twice, though it reached the answer tool in this eval. Read as a choice among the threads, it only asks "do you mean the thread called X?", which sends nothing, and the script was then worded "Logarithmic, please."

**Option choice** (`voice/t3/eval/record_answers.py`, replayed by `voice/t3/test_answers.py`, 55 recorded replies over 7 questions). Every reply is chosen as its case says, and none is chosen as an option or as words it is not. 25 `SEEN` and 18 `HELD` replies were written before the first run and never used to change anything. A sure option scored 0.82 to 0.99. A rehearsal then showed a plain answer to a question with no options at 0.61 to 0.67 on one call, under the 0.7 bar for an option, so the caller's own words have their own bar of 0.5 (`WORDS_SURE`). The 12 `LATE` replies were written for that change before they were run (own words 0.58 to 0.90; no reply that answers nothing gave words above 0.11). The same reply scored 0.61 and 0.79 on two calls an hour apart, so shares are not stable to a second decimal. One `LATE` reply, "Call it something with the date in it." to a question offering "By date", was read as that option at 0.90, a fair reading, and is scored only for being an option or words.

**The line.** The slot check finds an option anywhere in the line, so "Strong" is found in "how strong should it be", and a rehearsal said "Light or Medium" past it. Each option of an `ask_question` and each answer of a `confirm_answer` and an `answered` is now one decider question, "does the line offer X?", and a no refuses the line. Probed on 9 lines (4 that drop an option or an answer, 5 that do not): 9 of 9 as wanted. The realtime writer with the local text model behind it, over 17 made-up acts (`voice/t3/eval/line_eval.py`, two drafts, the slot check and the hosted line check): 13 within two drafts, then 16, 15 and 15 as the examples and checks changed. The last run passed 15 of 17 on the first draft and within two (ask_question 6 of 8, confirm_answer 4 of 4, answered 3 of 3, answer_in_app 2 of 2); its two misses end a three-option question with a period. The examples are what moved it: the thread form ("The X thread is asking ..."), a three-option example (a two-option one made the writer drop the third), one for a question with no options and one for several answers. The questions a confirmation answers are not shown to the writer, which asked them again.

**Rehearsal** (`python -m voice.live.rehearse drive --scenario answer --calls 6 -- --decider liquid --local-text on`: the real live server and synthetic speech against a scripted T3 that logs what it receives). The six calls: an option by name, by place, the caller's own words, two questions answered one at a time, one thread of two waiting, and a call that says no to the confirmation and must send nothing. A call counts when the scripted T3 received the right answers, to the right thread, and nothing else:

| Run | Delivered the right answer | What the run showed                                                                          |
| --- | -------------------------- | -------------------------------------------------------------------------------------------- |
| 1   | 1 of 6                     | a status line that read a question out was refused, so no thread was in view                 |
| 2   | 5 of 6                     | "answered" lines refused as claiming the run ended; a wrong-thread answer found in a dry run |
| 3   | 4 of 6                     | an option dropped by the writer; a relay the call voice answered instead of reciting         |
| 4   | 4 of 6                     | the same, on the bare-title form of the line                                                 |
| 5   | 5 of 6                     | a confirmation of two answers refused three times                                            |
| 6   | 6 of 6                     | the code of this page                                                                        |

Each fix followed a run, so runs 2 to 5 are not held out, and run 6 is one run of six calls on a script that was reworded once. In run 6 the status line of the two-question call was not said (the transcript garbled the question), and the call still delivered.

Two faults a rehearsal and a dry run against the hosted decider found, now fixed and tested:

- A thread waiting on a question was read out as a status line that quoted the question, which the slot check refuses as an unrequested question, so the voice said nothing about it. It now says the question with its options.
- "The second one" said as an answer was handed to the thread picker, which took it as the second thread of the listing, and a yes sent the answer to the wrong thread. An answer's words never pick the thread any more: one thread waiting on a question is that one, with several it is the thread the call last spoke about, else the voice asks which.

## Unproven

- Nothing ran against a live T3 with a pending question. The seam operation ran against scripted services (`answer.test.ts`) and the voice against a scripted seam. The scripted dispatch stores the answers it is sent, as the orchestrator does for string answers (read from `Orchestrator.ts`), and uses the real error tags. A real thread asking a question through the sandbox T3 was not tried: another server held its port.
- Providers take an answer differently (a live session, or a message the orchestrator dispatches as the user's). Neither was exercised beyond the fixtures' shapes.
- The rehearsal is one run of six calls per configuration, with synthetic speech and a script that fits the code. It counts answers delivered, not how a caller finds the flow, and the hosted decider's shares move between calls.
- The local d1-2B was not measured on the option choice or the tool choice. Calls use the hosted decider for both.
- A label with "(Recommended)" in it, or a long one, is said as given and the line check holds the writer to those words. How that sounds was not heard.
- Descriptions of options are never read out. A question whose options are labelled "A" and "B", with the meaning in the descriptions, would be told without it.
- A caller who answers a question told many turns ago, and the relay of a question while the caller is mid-sentence, were not rehearsed.
