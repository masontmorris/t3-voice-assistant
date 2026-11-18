# The decider picks the right T3 tool, but the gate holds half of the T3 requests

The free decider, `perplexity/pplx-decider-flash-v1.3:free`, picked the labeled tool for all 30 tool utterances over `t3_start`, `t3_ask`, `t3_tell`, `t3_stop`, `weather`, and `time` (measured, one run). The tool lines are good enough to ship behind `--t3 on`. The pick is not the bottleneck. In the same replay of a live end of turn, only 9 of the 20 T3 utterances would reach the T3 service. The intent and kind answers and the gate's side-effect bar stop the other 11 (measured). The integrated pass has to fix that before `--t3 on` can carry Mason's examples end to end.

## What ran

`voice/t3/tool_pick_eval.py` runs each case in `voice/t3/eval/tool_pick.jsonl` through `live_gate_eval.run_item`, the end of turn that live mode runs. The intent rules decide whether to ask the decider. The hybrid segmenter then asks its questions with the tool options that `T3Registry` offers under `--t3 on`. The pick is the tool answered for the utterance's first segment.

- The 33 cases are five utterances per tool plus three controls. They name made-up threads, and a person labeled them before the run (commit `17d0cdc70f`).
- One run, with one sample per case. The runner made 32 decider calls, and every answer reported a zero cost. A setup check made one more call, which the decider refused with a 400 (measured, ledger in `$VOICE_HOME/evals/t3-tool-pick`).
- The place and day questions are off. The free decider refuses their `text` type with a 400 (measured). Live mode asks them by default, and its default decider is the billed Liquid one, so this run does not test live mode's default configuration. A live run on `--decider cloud` likely needs `VOICE_DECIDER_ARGS=off` for the same reason (inferred from the 400, not run).

To rerun from the recorded answers with no calls, run `python -m voice.t3.tool_pick_eval` with `VOICE_ENV_FILE` set.

## The tool pick

| label \ pick | t3_start | t3_ask | t3_tell | t3_stop | weather | time | agent | not asked | right |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| t3_start | 5 | | | | | | | | 5 of 5 |
| t3_ask | | 5 | | | | | | | 5 of 5 |
| t3_tell | | | 5 | | | | | | 5 of 5 |
| t3_stop | | | | 5 | | | | | 5 of 5 |
| weather | | | | | 5 | | | | 5 of 5 |
| time | | | | | | 5 | | | 5 of 5 |
| none | | | | 1 | | | | 1 | 0 of 2 |
| agent | | | | | | | 1 | | 1 of 1 |

The near-miss probes landed on their labels. "Tell me the current time in Paris." picked `time`, not `t3_tell`. "Ask the PR thread to rebase on main." picked `t3_tell`, not `t3_ask`. "Is the release notes thread still working?" picked `t3_ask`, not `t3_stop` (all measured).

Both `none` controls miss on the table, and neither is a fault. The intent rules classed "Thanks, that's great." as chitchat, so the decider was never asked and no tool runs, which is the right outcome. "Stop, I was talking to someone else." picked `t3_stop` at 0.43, and the gate held the task (measured). Had it run, the voice would have asked "Stop the X thread?" first. A wrong `t3_stop` costs one question, never a stop (inferred from `voice/live/t3_tasks.py`).

## Why half of the T3 requests never reach T3

A pick reaches the T3 service only when the segmenter makes a task of it and the gate dispatches that task (`voice/segmentation/gate.py`, `PLAN_A`). Measured per label, from the recorded answers:

| label | reaches | held by the gate | no task (kind answered chitchat) |
| --- | --- | --- | --- |
| t3_start | 3 of 5 | 2, side effect 0.68 and 0.70 | 0 |
| t3_ask | 2 of 5 | 0 | 3, "How's the PR thread going?" among them |
| t3_tell | 3 of 5 | 2, "Tell it to also add a test." at ready 0.49 | 0 |
| t3_stop | 1 of 5 | 4, side effect 0.03 to 0.68 | 0 |
| weather | 5 of 5 | 0 | 0 |
| time | 3 of 5 | 0 | 2, "What time is it in Tokyo?" among them |

Two causes, both older than this unit:

- The gate gives every side effect a 0.80 bar on its weakest answer. A T3 start, follow-up, or stop is a side effect, so an uncertain side-effect or readiness answer holds the task. The final-decider turn that live mode runs opens no tool session that could run a held task later (inferred from `run_clip` in `voice/prototype/run.py`).
- The decider answers `kind` as chitchat for some status questions. The time tool loses the same 2 in 5, so the cause is not the T3 lines (measured).

Each T3 action already asks for an explicit yes before it acts. So the gate's side-effect hold protects nothing for these four tools, and it turns a request into silence. The smallest fix is to let a confident T3 tool pick through the gate the way a confident read-only tool pick already passes (`named_tool_threshold`). That is a gate change with its own eval, so it is left to the integrated pass (unit 10).

## What this eval cannot see

- One sample per case. The spread across samples is not measured.
- Single-utterance turns with no earlier turns, so "Tell it ..." and "Stop that thread." carry no context. The voice resolves "it" from its own journal, not from the decider (`T3Tasks._recent`).
- The place and day questions and the billed Liquid decider, live mode's default.
- The note that tells LFM3 to speak a T3 line (`task_note` in `voice/prototype/run.py`). Testing it needs LFM3 on the GPU, and this unit ran no GPU work.
