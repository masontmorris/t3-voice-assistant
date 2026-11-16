# Licenses

Each row records a model or dependency the voice assistant uses or studies.

| Name | Version or revision | License | Source | Notable terms |
| --- | --- | --- | --- | --- |
| T3 Code | This fork's `main` at `eeae88de5c68b2d3c744ad5e7e4964e118856d3f`. The investigation also read the installed build `0.1.8-nightly.20261106.4275`, commit `6577c6b6185167c0e502fa01f9e8e8f2dee6ff25`. | MIT | https://github.com/pingdotgg/t3code | Copyright 2026 T3 Tools Inc. Keep the copyright and permission notice in copies and forks. This repository is a fork, and the notice stays in `LICENSE`. Bundled third-party licenses are listed in `third-party-licenses/` and in `apps/server/dist/client/third-party-licenses.json` in the app. |
| Perplexity Decider Flash V1.3 (free variant), `perplexity/pplx-decider-flash-v1.3:free` | Endpoint `perplexity/pplx-decider-flash-v1.3-20261112:free`, measured on 2026-11-15 | Not stated. The model page, the public endpoints API, and the OpenRouter Decisions and Extractions docs name no license and no data policy. | [OpenRouter endpoints API](https://openrouter.ai/api/v1/models/perplexity/pplx-decider-flash-v1.3:free/endpoints) | A hosted model with no weights to download. Use is governed by the OpenRouter terms of service and the provider's terms. Prompt and completion price 0. Requests count against the key's free-model daily limit (1,000 per day, measured). The paid id `perplexity/pplx-decider-flash-v1.3` must not be called. |
| Python standard library | CPython 3.14.6 | Python Software Foundation License 2.0 | [python.org license](https://docs.python.org/3/license.html) | Stage 2 uses only the standard library (`http.client`, `json`, `unittest`, and others). No third-party packages. |
