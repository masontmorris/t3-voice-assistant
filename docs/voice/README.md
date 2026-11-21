# T3 Voice Assistant

This fork of [pingdotgg/t3code](https://github.com/pingdotgg/t3code) explores a voice assistant built on T3 Code. LFM3 Audio provides the voice, a decision model orchestrates, and frontier agents do the work. The plan is in [plan.md](plan.md).

## Branches

- `main` mirrors upstream and gets no voice commits.
- `voice/main` is the voice integration branch.
- Each feature lands on its own `voice/*` branch through a PR into `voice/main`.

## Layout

`voice/` holds what the live call runs. `voice/research/` holds everything else.

- `voice/live` is the server and client a call runs. `voice/prototype`, `voice/segmentation`, `voice/local_decider`, `voice/liquid_decider` and `voice/tools` are the turn's pipeline, segmenter, deciders and lookups, which `voice/live/server.py` imports. `voice/bot` is the bot spike.
- `voice/research/` holds the experiments, offline evals and benchmarks those packages do not import, with the recorded answers and run results they read. `voice/research/speech` is the LFM3 and llama.cpp harness the live call's server flags came from. The tag `prototype-v0` keeps the history before the split.

Trace imports from `voice/live/server.py` and `voice/live/client.py` before moving a research package back into `voice/`.

## Before merging a change to the call

Run the whole-call gate before merging any change to `voice/live`, `voice/prototype` or `voice/t3`. It needs the GPU and
the decider key, so CI cannot run it. Run it under the lease:

```
python gpu_lease.py run --holder <name> --purpose "whole-call gate" --max-minutes 30 -- python -m voice.live.scenarios gate
```

It starts a sandbox T3 of its own (a copy of a template home, so no thread outlives a run) and the live server on it,
and holds one synthetic call per scenario. Each scenario is a caller who picks its next line from what the assistant did,
and states what must be true afterwards. It is scored from the turns' records and the T3's state, and a model answers a
stated question only about a free reply. It prints one row per scenario and exits non-zero when one fails. The default
set takes about 13 minutes and about 100 hosted decider calls (under a cent); a scenario whose first run fails gets
a second only when its rule allows it. `--long` adds three more scenarios (about 8 minutes more), `--only ID` runs some,
and `--spread N` runs each scenario N times unless it errors. `python -m voice.live.scenarios list` shows the rules.

| Scenario                      | Rule   | Baseline reason                                                                     |
| ----------------------------- | ------ | ----------------------------------------------------------------------------------- |
| `speak-over`                  | 1 of 2 | Whether the first reply stopped varied.                                             |
| `approval-claim`              | 1 of 1 | The approval checks and the silence check agreed across runs.                       |
| `thread-lines`                | 1 of 1 | The thread-status replies consistently used checked lines.                          |
| `follow-up`                   | 1 of 1 | The follow-up kept the city. The forecast-date fault is marked.                     |
| `withdrawn-question`          | 1 of 1 | The initial thread question was asked in all three runs. Only marked checks varied. |
| `news-permission`             | 1 of 2 | The known launch-dispatch fault can prevent the approval exchange from running.     |
| `short-yes`                   | 1 of 2 | Whether both requests got a confirmation question varied.                           |
| `news-done` (`--long`)        | 1 of 2 | Some runs never reached a finished thread or its news.                              |
| `short-yes-tell` (`--long`)   | 1 of 2 | Whether T3 started a new run with the message varied.                               |
| `withdrawn-launch` (`--long`) | 1 of 2 | Whether the assistant asked a launch question varied.                               |

A check that reproduces a fault nobody has fixed is listed in `KNOWN` in `voice/live/scenarios.py` with the fault. Its
failure is XFAIL and does not turn the gate red. XPASS means every marked check passed in the runs it made: confirm
with `--spread 3`, then delete the mark. An unmet precondition fails unless it explicitly names a failed marked check
that blocked it. The table counts turns that attempted a fallback. The scripted calls expect substantive replies, so
a fallback fails their reply check. A missing turn record, an unanswered judge question, or a startup, runtime, or teardown error makes the
scenario `ERROR` and the gate red. The pass rule counts completed runs with product-check outcomes. What the gate cannot
catch is in the docstring of `voice/live/scenarios.py`.

## Development defaults

- LFM3 Audio runs locally for development.
- The decision model is Liquid AI's hosted `d1.2`, called over the System One decisions API. The key comes from
  `LIQUID_API_KEY` and is never committed. See [liquid-decider.md](liquid-decider.md).
- `perplexity/pplx-decider-flash-v1.3:free` on OpenRouter stays selectable with `--decider cloud`. Its key comes from
  `OPENROUTER_API_KEY`.
