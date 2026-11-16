# Licenses

Each row records a model or dependency the voice assistant uses or studies.

| Name    | Version or revision                                                                                                                                                                                 | License | Source                              | Notable terms                                                                                                                                                                                                                                                                                      |
| ------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------- | ----------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| T3 Code | This fork's `main` at `eeae88de5c68b2d3c744ad5e7e4964e118856d3f`. The investigation also read the installed build `0.1.8-nightly.20261106.4275`, commit `6577c6b6185167c0e502fa01f9e8e8f2dee6ff25`. | MIT     | https://github.com/pingdotgg/t3code | Copyright 2026 T3 Tools Inc. Keep the copyright and permission notice in copies and forks. This repository is a fork, and the notice stays in `LICENSE`. Bundled third-party licenses are listed in `third-party-licenses/` and in `apps/server/dist/client/third-party-licenses.json` in the app. |

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
