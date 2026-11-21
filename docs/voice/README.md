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
python C:/Users/mason/.gpu/gpu_lease.py run --holder <name> --purpose "whole-call gate" --max-minutes 30 -- \
  python -m voice.live.scenarios gate
```

It starts a sandbox T3 of its own (a copy of a template home, so no thread outlives a run), starts the live server on it,
and holds one synthetic call per scenario. Each scenario is a caller who picks its next line from what the assistant did,
and states what must be true afterwards. It is scored from the turns' records and the T3's state, and a model answers a
stated question only about a free reply. It prints one row per scenario and exits non-zero when one fails. `--long` adds
the long set, `--only ID` runs one, and `--spread N` runs every scenario N times to measure its spread.
`python -m voice.live.scenarios list` shows the scenarios, their pass rules and what each covers.

A check that reproduces a fault nobody has fixed is listed in `KNOWN` in `voice/live/scenarios.py` with the fault. Its
failure is XFAIL and does not turn the gate red. When the fix lands the row says XPASS: delete the mark. Do not add a
mark without saying what the fault is. How long it takes, what it spends, and which scenarios pass today are in the PR
that added it; what it cannot catch is in the docstring of `voice/live/scenarios.py`.

## Development defaults

- LFM3 Audio runs locally for development.
- The decision model is Liquid AI's hosted `d1.2`, called over the System One decisions API. The key comes from
  `LIQUID_API_KEY` and is never committed. See [liquid-decider.md](liquid-decider.md).
- `perplexity/pplx-decider-flash-v1.3:free` on OpenRouter stays selectable with `--decider cloud`. Its key comes from
  `OPENROUTER_API_KEY`.
