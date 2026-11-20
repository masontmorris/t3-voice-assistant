# T3 requests reach T3: the tool pick, the gate, and the kind question

With `--t3 on`, 19 of 20 labeled T3 requests now reach the T3 service, up from 9 of 20 (measured, 3 samples per case on the free decider, `perplexity/pplx-decider-flash-v1.3:free`). No chitchat control reached a T3 tool before or after. The weather and time cases route exactly as they did before (measured). Two changes did it. The gate lets a firm T3 tool pick through, and the request-versus-chat question names thread status questions as requests.

## What ran

`voice/t3/tool_pick_eval.py` runs each case through `live_gate_eval.run_item`, the end of turn live mode runs. The intent rules decide whether the decider is asked. Then the hybrid segmenter asks its questions with the tool options `T3Registry` offers under `--t3 on`. A case reaches its tool when a task with that tool was dispatched and its tool answer clears the registry's 0.30 bar, which is what routes it to the T3 service.

- **Cases.** `voice/t3/eval/tool_pick.jsonl` holds 20 T3 requests, 10 weather and time controls, and 15 controls whose right tool is none or the general lookup. A person labeled them before any run. The 12 newest chitchat controls sit near one T3 tool's words ("How's your day going?", "My coworker started a new thread about the outage this morning.", "I really need to stop staying up so late."), and were committed before the first call (`b19e12eb9a`). `--gate` adds the live gate eval's 14 weather and time items, each with the call context it carries.
- **Samples.** 3 per case. A case passes when more than half of its samples pass. The samples differ: 44 of 58 repeated call positions returned different answers after the change (measured).
- **Calls.** The before run made 130 decider calls and the after run 167, plus 7 for the first check on the failing cases. Every answer reported zero cost (measured, ledger in `$VOICE_HOME/evals/t3-tool-pick/ledger`).
- **Before.** `--text before` puts back the text and gate from before this change. The recorded answers prove it is the same text: a recorded answer is reused only when its request equals the body sent now, and #57's run 1 answers all matched.

```text
python -m voice.t3.tool_pick_eval --text before --gate
python -m voice.t3.tool_pick_eval --text after --gate
```

A rerun scores the recorded answers and calls nothing.

## Results

| label                            | before: cases (samples) | after: cases (samples)                   |
| -------------------------------- | ----------------------- | ---------------------------------------- |
| t3_start                         | 3 of 5 (9 of 15)        | 5 of 5 (15 of 15)                        |
| t3_ask                           | 2 of 5 (6 of 15)        | 5 of 5 (15 of 15)                        |
| t3_tell                          | 3 of 5 (9 of 15)        | 5 of 5 (15 of 15)                        |
| t3_stop                          | 1 of 5 (3 of 15)        | 4 of 5 (12 of 15)                        |
| weather                          | 5 of 5 (15 of 15)       | 5 of 5 (15 of 15)                        |
| time                             | 3 of 5 (9 of 15)        | 3 of 5 (8 of 15)                         |
| false T3 actions on 15 controls  | 0 samples               | 0 samples                                |
| live gate weather and time items | 10 of 14 route weather  | 10 of 14 route weather, the same samples |

The tool pick after the change, one row per sample:

| label \ pick | t3_start | t3_ask | t3_tell | t3_stop | weather | time | none | agent | no answer | not asked | right    |
| ------------ | -------- | ------ | ------- | ------- | ------- | ---- | ---- | ----- | --------- | --------- | -------- |
| t3_start     | 15       |        |         |         |         |      |      |       |           |           | 15 of 15 |
| t3_ask       |          | 15     |         |         |         |      |      |       |           |           | 15 of 15 |
| t3_tell      |          |        | 15      |         |         |      |      |       |           |           | 15 of 15 |
| t3_stop      |          |        |         | 12      |         |      |      |       | 3         |           | 12 of 15 |
| weather      |          |        |         |         | 15      |      |      |       |           |           | 15 of 15 |
| time         |          |        |         |         |         | 15   |      |       |           |           | 15 of 15 |
| none         |          |        |         | 3       |         |      | 36   |       |           | 3         | 36 of 42 |
| agent        |          |        |         |         |         |      |      | 3     |           |           | 3 of 3   |

The 3 `t3_stop` picks on `none` are "Stop, I was talking to someone else.", at 0.43 to 0.45. The gate holds them, because a T3 pick needs the 0.70 named-tool bar. Since 2026-11-20 a thread request held or left to clarify on the final transcript still goes to T3, as a lookup when its pick is under that bar ([spoken acts](spoken-acts.md#a-thread-question-the-decider-is-unsure-about)), so where the decider reads one of these as a request the voice says how a thread is going, and stops nothing. On the recorded answers that is 1 of the 45 control samples, against 0 before, and the T3 requests that reach their own tool rise from 56 to 60 of 60 samples: four firm picks the decider read as `clarify` now reach their confirmation (measured, no calls).

## What changed

- **The gate** (`voice/segmentation/gate.py`). On the final transcript, a firm pick of a T3 tool passes at the named-tool bar of 0.70, side effect or not. Every T3 action asks Mason for an explicit yes before it acts, and `t3_ask` only reads, so the side-effect bar protected nothing for these tools. It held 8 of the 20 requests, among them "Tell it to also add a test." The bar is unchanged for every other tool. On #57's recorded answers the gate alone moves the count from 9 to 17 (measured, no calls).
- **The kind question** (`voice/segmentation/hybrid.py`). When the T3 tools are offered, the request criterion adds that asking how a coding thread is going, or asking to start, tell or stop one, is external work. The chitchat criterion adds that a question about a thread's progress is not chitchat. It fixed the 3 status questions the decider had filed as chitchat: "How's the PR thread going?", "How is the toolchain upgrade coming along?" and "Is the release notes thread still working?" (measured, 3 of 3 samples each). With `--t3 off` the question is unchanged.

## The one T3 request that still misses

"Interrupt the clamp helper thread, it's going the wrong way." picks `t3_stop` at 0.99, but no task comes of it, in 3 of 3 samples (measured). The cause is in the segmenter, not the T3 text. On the final transcript the break answers flip the split at the comma on every call: one piece, then two, then one again. After its fourth drain the segmenter applies the pieces but keeps the decisions only for pieces that did not change, which is none, so the turn makes no task (measured on the 5 recorded calls of sample 1; `_point` in `voice/segmentation/hybrid.py`). The before text did not flip on this case, so the new sentence moved the break answer. Any utterance whose break answers oscillate on the final transcript meets the same silence. The fix belongs to the segmenter and has its own eval to run.

## What this eval cannot see

- Earlier turns. Every case is one utterance with no earlier turn. Follow-ups about the thread a call just spoke of have their own cases (`voice/t3/eval/record_followups.py`, [spoken acts](spoken-acts.md#a-follow-up-about-the-thread-just-discussed)).
- The place and day questions. The free decider refuses their `text` type with a 400, so they are off for it (`decider_arguments`).
- The billed Liquid decider, live mode's default.
- The voice. T3 lines are spoken word for word in the server's TTS voice, because LFM3 reworded them into claims no record makes in the integrated pass's runs 2 and 3. The seam unit 10 PR has those runs.
- The time misfile. "What time is it in Tokyo?" and "What time is it in New York?" are still answered as chitchat, before and after, which is out of scope here.
