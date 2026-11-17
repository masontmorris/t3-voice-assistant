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

## Development defaults

- LFM3 Audio runs locally for development.
- The decision model is Liquid AI's hosted `d1.2`, called over the System One decisions API. The key comes from
  `LIQUID_API_KEY` and is never committed. See [liquid-decider.md](liquid-decider.md).
- `perplexity/pplx-decider-flash-v1.3:free` on OpenRouter stays selectable with `--decider cloud`. Its key comes from
  `OPENROUTER_API_KEY`.
