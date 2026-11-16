# T3 Voice Assistant

This fork of [pingdotgg/t3code](https://github.com/pingdotgg/t3code) explores a voice assistant built on T3 Code. LFM3 Audio provides the voice, a decision model orchestrates, and frontier agents do the work. The plan is in [plan.md](plan.md).

## Branches

- `main` mirrors upstream and gets no voice commits.
- `voice/main` is the voice integration branch.
- Each feature lands on its own `voice/*` branch through a PR into `voice/main`.

## Development defaults

- LFM3 Audio runs locally for development.
- The decision model starts as `perplexity/pplx-decider-flash-v1.3:free` on OpenRouter. The key comes from `OPENROUTER_API_KEY` and is never committed.
