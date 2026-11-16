# Licenses

Each row records a model or dependency the voice assistant uses or studies.

| Name | Version or revision | License | Source | Notable terms |
| --- | --- | --- | --- | --- |
| T3 Code | This fork's `main` at `eeae88de5c68b2d3c744ad5e7e4964e118856d3f`. The investigation also read the installed build `0.1.8-nightly.20261106.4275`, commit `6577c6b6185167c0e502fa01f9e8e8f2dee6ff25`. | MIT | https://github.com/pingdotgg/t3code | Copyright 2026 T3 Tools Inc. Keep the copyright and permission notice in copies and forks. This repository is a fork, and the notice stays in `LICENSE`. Bundled third-party licenses are listed in `third-party-licenses/` and in `apps/server/dist/client/third-party-licenses.json` in the app. |
| Perplexity Decider Flash V1.3 (free variant), `perplexity/pplx-decider-flash-v1.3:free` | Endpoint `perplexity/pplx-decider-flash-v1.3-20261112:free`, measured on 2026-11-15 | Not stated. The model page, the public endpoints API, and the OpenRouter Decisions and Extractions docs name no license and no data policy. | [OpenRouter endpoints API](https://openrouter.ai/api/v1/models/perplexity/pplx-decider-flash-v1.3:free/endpoints) | A hosted model with no weights to download. Use is governed by the OpenRouter terms of service and the provider's terms. Prompt and completion price 0. Requests count against the key's free-model daily limit (1,000 per day, measured). The paid id `perplexity/pplx-decider-flash-v1.3` must not be called. The bot heartbeat spike measured `usage.cost` 0 on every response on 2026-11-16 (measured). |
| DeepSeek V4.1 Flash, the draft author for `voice/delegation/`, called as `deepseek/deepseek-v4.1-flash`, `deepseek/deepseek-v4.1-flash-fast`, and `deepseek/deepseek-v4.1-flash-ultrafast` through Command Code (`https://api.commandcode.ai/provider/v1`) | Command Code lists the id as "DeepSeek V4.1 Flash (latest)" and returns no build stamp (`system_fingerprint` is empty), so the build it serves is unconfirmed. The weights cards read on 2026-11-16 are `deepseek-ai/DeepSeek-V4.1-Flash-1017` at `08c8657f02291e11b6f6b63e0e16014f8e748518` (created 2026-10-17) and `deepseek-ai/DeepSeek-V4.1-Flash` at `314113f8a7dadb388215396ba5fbee2c610a04d6`. | MIT (both cards' `license` field) | https://huggingface.co/deepseek-ai/DeepSeek-V4.1-Flash-1017 | Called on Mason's Command Code subscription, so Command Code's terms of service apply to the hosted calls. Most requests carry no reasoning parameter, so each id runs on its own default. The reasoning race sent `reasoning_effort: "off"`, the lowest value Command Code accepts, after it rejected `minimal`. Mason allowed his clip transcripts to go to it on 2026-11-16. The listing does not say whether the `-fast` and `-ultrafast` ids serve the same weights. |
| Python standard library | CPython 3.14.6 | Python Software Foundation License 2.0 | [python.org license](https://docs.python.org/3/license.html) | Stage 2 uses only the standard library (`http.client`, `json`, `unittest`, and others). No third-party packages. |
| T3 Tots | v1.4.34 | Not stated in the installed files (`README.md`, `manifest.json`). The source repository was not read. | https://github.com/Deepusleepy/t3-tots | `voice/bot` copies no T3 Tots code. It calls the same T3 RPCs that `t3ctl` and `t3listen` call, and it reads the T3 Tots token file at run time from `T3_TOTS_DIR`. |

## Stage 1 speech path

These rows cover `voice/speech/` and [stage1-speech-path.md](stage1-speech-path.md). Package versions and license strings come from each package's installed metadata (`importlib.metadata`) in `$VOICE_HOME/venv-lfm3`, measured on 2026-11-15.

| Name | Version or revision | License | Source | Notable terms |
| --- | --- | --- | --- | --- |
| `LiquidAI/LFM3-Audio-2B` | `2d6c1f99c129b3ef07a8d168bc0ff08e2bdb93f1` | LFM Open License v1.0 (`license: other`, `license_name: lfm1.0`) | https://huggingface.co/LiquidAI/LFM3-Audio-2B | Commercial use is licensed only while the user's legal entity has annual revenue under $10M (section 5). Redistribution needs the license text and change notices (section 4). The remote-code Python files also carry an Apache-2.0 header with "Copyright 2026 The HuggingFace Team" (measured, file headers). The card puts the whole repository under the LFM Open License, so treat those files as covered by both until Liquid AI says otherwise (inferred). |
| `LiquidAI/LFM3-Audio-2B-GGUF`, files `LFM3-Audio-2B-Q4_K_M.gguf` and `mmproj-LFM3-Audio-2B-Q8_0.gguf` | `39670cb5c42e9c4bfe046519ba7ffbe47f9552ad` | LFM Open License v1.0 | https://huggingface.co/LiquidAI/LFM3-Audio-2B-GGUF | Same terms as the model. Its LICENSE file is byte-identical to the model's (measured with `cmp`). |
| llama.cpp | Build b12428, commit `b4c0e00a4`, from `llama-b12428-bin-win-cuda-12.4-x64.zip` (SHA-256 `f16b22ade49824faba82f8d9a95e670fad9a8b420cacf2bbfbd692fdc7d7ff12`) | MIT | https://github.com/ggml-org/llama.cpp/releases/tag/b12428 | "Copyright (c) 2023-2026 The ggml authors". Keep the notice in copies. |
| CUDA runtime for llama.cpp, from `cudart-llama-bin-win-cuda-12.4-x64.zip` (SHA-256 `8c79a9b226de4b3cacfd1f83d24f962d0773be79f1e7b75c6af4ded7e32ae1d6`) | 12.4 | NVIDIA CUDA EULA | https://github.com/ggml-org/llama.cpp/releases/tag/b12428 | Covers `cudart64_12.dll`, `cublas64_12.dll`, and `cublasLt64_12.dll`. |
| LLVM OpenMP (`libomp.dll`) | Bundled with llama.cpp b12428 | Apache-2.0 with LLVM exceptions | https://github.com/ggml-org/llama.cpp/releases/tag/b12428 | The license is `LICENSE-LLVM-OpenMP` in the zip. |
| torch | 2.15.0+cu132 | BSD-3-Clause with bundled Apache-2.0, MIT, BSL-1.0, ISC, and Zlib parts | https://download.pytorch.org/whl/cu132 | The CUDA build bundles NVIDIA libraries in `torch/lib` (`cudart64_13.dll`, `cublas64_13.dll`, `cudnn64_9.dll`, and others, measured). Those fall under NVIDIA's CUDA and cuDNN license terms, not torch's. |
| transformers | 5.20.2 | Apache-2.0 | https://github.com/huggingface/transformers | |
| accelerate | 1.15.0 | Apache-2.0 | https://github.com/huggingface/accelerate | |
| huggingface_hub | 2.4.0 | Apache-2.0 | https://github.com/huggingface/huggingface_hub | |
| tokenizers | 0.23.3 | Apache-2.0 | https://github.com/huggingface/tokenizers | |
| safetensors | 0.8.0 | Apache-2.0 | https://github.com/huggingface/safetensors | |
| librosa | 1.0.1 | ISC | https://github.com/librosa/librosa | |
| soundfile | 0.14.0 | BSD-3-Clause | https://github.com/bastibe/python-soundfile | |
| soxr | 1.1.0 | LGPL-2.1-or-later | https://github.com/dofuuz/python-soxr | A librosa dependency for resampling. Dynamic linking from Python is the usual LGPL-compatible use, but check before you ship a frozen binary (inferred). |
| numpy | 2.5.5 | BSD-3-Clause with bundled 0BSD, MIT, Zlib, and CC0-1.0 parts | https://numpy.org | |
| websockets | 17.4 | BSD-3-Clause | https://github.com/python-websockets/websockets | |
| Windows SAPI voices (`System.Speech.Synthesis`, Microsoft Zira Desktop and Microsoft David Desktop) | Built into Windows 11 | Part of Windows | Windows | `make_audio.ps1` synthesizes the test clips locally. No real person's voice is recorded. The clips stay in `$VOICE_HOME/evidence/stage1/audio/` and are not committed. |

### LFM Open License v1.0, the terms that matter

The LICENSE file in the pinned snapshot is the source of every quote below. It is 10,574 bytes.

- **Who it covers.** Section 1 defines "Licensor" as Liquid AI, Inc. A "Legal Entity" includes every entity that controls, is controlled by, or is under common control with the acting entity. Revenue therefore counts across a corporate group.
- **The grant.** Section 2 grants "a perpetual, worldwide, non-exclusive, no-charge, royalty-free, irrevocable copyright license to reproduce, prepare Derivative Works of, publicly display, publicly perform, sublicense, and distribute the Work and such Derivative Works". The grant is "subject to ... the Commercial Use limitation set forth in Section 5".
- **Commercial use and the revenue threshold.** Section 1 defines "Commercial Use" as "any use of the Work for direct or indirect commercial advantage or monetary compensation" and "Threshold" as "annual revenue of 10 million United States dollars ($10,000,000) or more". Section 5(a) says "The rights granted under this License for Commercial Use are conditioned upon You or Your Legal Entity not exceeding the Threshold." Section 5(b) says "Any Commercial Use of the Work or a Derivative Work by a Legal Entity that exceeds the Threshold is not licensed under this Agreement." Section 5(c) exempts a Qualified Non-Profit Organization's non-commercial or research use from the threshold.
- **Reading of section 5 (inferred, not legal advice).** Personal use and research are licensed. Commercial use is licensed while the user's legal entity has annual revenue under $10M. The text uses "not exceeding" in 5(a) and "exceeds" in 5(b), while the Threshold is "$10,000,000 or more". Revenue of exactly $10M is ambiguous. Above the threshold, commercial use needs a separate agreement with Liquid AI.
- **Redistribution.** Section 4 allows redistribution of the Work or Derivative Works in any medium if you (a) give recipients a copy of the License, (b) mark modified files with prominent notices, (c) keep the copyright, patent, trademark, and attribution notices in source form, and (d) carry the NOTICE attribution if a NOTICE file exists. The pinned snapshot has no NOTICE file (measured, directory listing).
- **Attribution and trademarks.** Section 7 grants no trademark rights "except for the reasonable and customary use in describing the origin of the Work".
- **Patents.** Section 3 grants a patent license that ends if you sue anyone alleging the Work infringes a patent.
- **Termination.** Section 11 says the License "will terminate automatically and immediately if You fail to comply with any of its terms", after which you "must cease all use ... and delete all copies".
- **Warranty.** Sections 8 and 9 disclaim warranty and liability. The Work is provided "AS IS".

## Local decider

These rows cover `voice/local_decider/` and [local-decider.md](local-decider.md), measured on 2026-11-16.

| Name | Version or revision | License | Source | Notable terms |
| --- | --- | --- | --- | --- |
| `LiquidAI/d1-2B-GGUF`, file `d1-2B-Q4_K_M.gguf` | `4a181094d1b6addfaa3a0956f78e7ab2aa406a0c`, file SHA-256 `adca5789ad30ae6027e8e936749bb9f19673097492ab26fa1c5b69643ca50b4b` (1,099,629,888 bytes) | LFM Open License v1.0 (`license: other`, `license_name: lfm1.0`) | https://huggingface.co/LiquidAI/d1-2B-GGUF | Same terms as LFM3-Audio above. Its LICENSE file is byte-identical to LFM3-Audio-2B's (measured with `cmp`). The file lives in `$VOICE_HOME/gguf/` and is not committed. |
| `LiquidAI/d1-2B` (Transformers weights and remote code) | `652f920e544ea37f81e4b0ce89b81d4ed0da90b1` | LFM Open License v1.0 | https://huggingface.co/LiquidAI/d1-2B | Read from decision-lab's Hugging Face cache for one comparison run, not downloaded by this lane and not a dependency of the voice code. |
| torch, transformers (decision-lab's venv) | torch 2.11.0+cu128, transformers 5.19.0 | BSD-3-Clause, Apache-2.0 | https://pytorch.org, https://github.com/huggingface/transformers | Used read-only by `transformers_bench.py` for that comparison. Not installed by this lane. |
| mistral.rs | Not installed | MIT | https://github.com/EricLBuehler/mistral.rs | Evaluated on paper only. See the blocked option in [local-decider.md](local-decider.md). |

The local decider client, the reuse layer, and the bench scripts use only the Python standard library, plus numpy, soundfile and websockets from the LFM3 venv for the barge-in bench.

## Real read-only tools

These rows cover `voice/tools/` and [real-tools.md](real-tools.md). Package versions and license strings come from each package's installed metadata (`importlib.metadata`) in `$VOICE_HOME/venv-mcp-time`, measured on 2026-11-16. The Open-Meteo terms come from its [terms page](https://open-meteo.com/en/terms) and [licence page](https://open-meteo.com/en/licence), read on 2026-11-16.

| Name | Version or revision | License | Source | Notable terms |
| --- | --- | --- | --- | --- |
| Open-Meteo Forecast API and Geocoding API | The free API at `api.open-meteo.com` and `geocoding-api.open-meteo.com`, called on 2026-11-16 | Data under CC BY 4.0. The service is governed by the Open-Meteo terms of use. | https://open-meteo.com | The free API is for non-commercial use only, and allows fewer than 10,000 calls per day, 5,000 per hour, and 600 per minute. No key. Attribution is required wherever the data is shown, with a link such as "Weather data by Open-Meteo.com" to https://open-meteo.com/. The voice replies carry no attribution yet, so a public demo needs one (inferred from the licence). The geocoding location data comes from GeoNames under CC BY. The recorded responses in `voice/tools/fixtures/recorded.json` are Open-Meteo data, so the same attribution applies to them. |
| `mcp-server-time` | 2026.11.11 | MIT | https://github.com/modelcontextprotocol/servers/tree/main/src/time | The reference MCP time server. It runs locally over stdio with no network. Keep the copyright and permission notice in copies. |
| `mcp` (the MCP Python SDK) and `mcp-types` | 2.3.0 | MIT | https://github.com/modelcontextprotocol/python-sdk | A dependency of `mcp-server-time`. `voice/tools/mcp.py` does not import it. The client speaks JSON-RPC with the standard library. |
| Other `mcp-server-time` dependencies | As installed | MIT: annotated-types 0.8.0, anyio 4.16.0, attrs 26.1.0, h11 0.16.0, jsonschema 4.26.0, jsonschema-specifications 2025.9.1, pydantic 2.14.3, pydantic_core 2.50.4, PyJWT 2.15.1, referencing 0.37.0, rpds-py 2026.9.1, truststore 0.10.5, typing-inspection 0.4.5, tzlocal 5.4.5. MIT-0: cffi 2.1.1. BSD-3-Clause: click 8.5.1, httpcore2 2.16.1, httpx2 2.16.1, idna 3.20, pycparser 3.12, sse-starlette 3.7.0, starlette 1.10.0, uvicorn 0.54.0. Apache-2.0: opentelemetry-api 1.45.2, python-multipart 0.0.33, tzdata 2026.6. Apache-2.0 OR BSD-3-Clause: cryptography 50.0.2. PSF: pywin32 312, typing_extensions 4.16.0. | PyPI | They are installed only in the server's venv. Nothing in the repo imports them. |
