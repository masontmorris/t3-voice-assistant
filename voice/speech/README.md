# How to run the Stage 1 speech path

These scripts run LFM3-Audio-2B locally. `llamacpp_rt.py` drives llama.cpp's `/v1/realtime` WebSocket server, which is the runtime Stage 1 recommends. The other scripts load the model in Transformers as the reference implementation and benchmark it. The measured results are in [stage1-speech-path.md](../../docs/voice/stage1-speech-path.md).

Stage 1 ran on Windows 11 with an RTX 3060 12 GB. The llama.cpp steps use the Windows CUDA 12.4 build. The commands below are for Git Bash. In PowerShell, write `$env:VOICE_HOME` for `$VOICE_HOME`.

## Set `VOICE_HOME`

Everything the scripts read or write outside the repo lives under one directory, `VOICE_HOME`. If the variable is unset, the scripts use `~/voice`. Set it before every step:

```bash
export VOICE_HOME="$HOME/voice"
```

| Path under `$VOICE_HOME` | Contents |
| --- | --- |
| `venv-lfm3/` | The Python 3.13 venv |
| `hf/` | The Hugging Face cache. `lfm.py` sets `HF_HOME` to this directory and `HF_HUB_OFFLINE=1`. |
| `llamacpp/bin/` | The llama.cpp b12428 binaries, including `llama-server.exe` |
| `gguf/` | The two GGUF files and their LICENSE |
| `evidence/stage1/` | Run output: JSON, server logs, and reply WAVs |
| `evidence/stage1/audio/` | The synthetic test clips |

Keep model files, audio, and run output under `$VOICE_HOME`. Never commit them.

## Create the venv

```bash
py -3.13 -m venv "$VOICE_HOME/venv-lfm3"
source "$VOICE_HOME/venv-lfm3/Scripts/activate"
pip install torch==2.15.0+cu132 --index-url https://download.pytorch.org/whl/cu132
pip install transformers==5.20.2 accelerate==1.15.0 librosa==1.0.1 soundfile==0.14.0 websockets==17.4
```

`llamacpp_rt.py` imports `lfm.py`, so the llama.cpp demo also needs torch and transformers.

## Download the llama.cpp server

Download both zips from [llama.cpp release b12428](https://github.com/ggml-org/llama.cpp/releases/tag/b12428), check them, and unzip them into one directory:

```bash
mkdir -p "$VOICE_HOME/llamacpp/bin" && cd "$VOICE_HOME/llamacpp"
curl -LO https://github.com/ggml-org/llama.cpp/releases/download/b12428/llama-b12428-bin-win-cuda-12.4-x64.zip
curl -LO https://github.com/ggml-org/llama.cpp/releases/download/b12428/cudart-llama-bin-win-cuda-12.4-x64.zip
sha256sum -c - <<'EOF'
f16b22ade49824faba82f8d9a95e670fad9a8b420cacf2bbfbd692fdc7d7ff12  llama-b12428-bin-win-cuda-12.4-x64.zip
8c79a9b226de4b3cacfd1f83d24f962d0773be79f1e7b75c6af4ded7e32ae1d6  cudart-llama-bin-win-cuda-12.4-x64.zip
EOF
unzip -o llama-b12428-bin-win-cuda-12.4-x64.zip -d bin
unzip -o cudart-llama-bin-win-cuda-12.4-x64.zip -d bin
rm llama-b12428-bin-win-cuda-12.4-x64.zip cudart-llama-bin-win-cuda-12.4-x64.zip
```

The digests are the ones GitHub publishes for the release assets. If `sha256sum -c` prints anything other than `OK` for each file, delete the zips and stop.

## Download the GGUF files

The GGUF files come from `LiquidAI/LFM3-Audio-2B-GGUF` at revision `39670cb5c42e9c4bfe046519ba7ffbe47f9552ad`:

```bash
mkdir -p "$VOICE_HOME/gguf" && cd "$VOICE_HOME/gguf"
REV=39670cb5c42e9c4bfe046519ba7ffbe47f9552ad
for f in LFM3-Audio-2B-Q4_K_M.gguf mmproj-LFM3-Audio-2B-Q8_0.gguf LICENSE; do
	curl -LO "https://huggingface.co/LiquidAI/LFM3-Audio-2B-GGUF/resolve/$REV/$f"
done
sha256sum -c - <<'EOF'
9a773289bd321ed879cd3a76545f6aca506e403851a45fb315420aee45625be2  LFM3-Audio-2B-Q4_K_M.gguf
2b6648b1fd48af16aa0877b0592b144d15070783673e038a379fdbc51d40e365  mmproj-LFM3-Audio-2B-Q8_0.gguf
4d28ca14dedc0b3d0fcc2b3339f0e79931faa33874f3d24f522183a8fc70068c  LICENSE
EOF
```

The two GGUF digests equal the LFS object ids the Hub lists for that revision. The files are 1.10 GB and 304 MB.

## Download the Transformers snapshot

Only the Transformers scripts need this step. The snapshot is about 4.2 GB.

```bash
HF_HOME="$VOICE_HOME/hf" hf download LiquidAI/LFM3-Audio-2B --revision 2d6c1f99c129b3ef07a8d168bc0ff08e2bdb93f1
cd "$VOICE_HOME/hf/hub/models--LiquidAI--LFM3-Audio-2B/snapshots/2d6c1f99c129b3ef07a8d168bc0ff08e2bdb93f1"
sha256sum *.py
```

`lfm.py` loads this snapshot with `trust_remote_code=True`, which runs its nine Python modules. Compare each digest with the table in [Remote-code review](../../docs/voice/stage1-speech-path.md#remote-code-review) before the first load.

## Make the test clips

```bash
powershell -ExecutionPolicy Bypass -File voice/speech/make_audio.ps1
```

The script uses the built-in Windows SAPI voices to write `single`, `two_request`, `correction`, and `long` as 16 kHz mono WAVs, each with a `.txt` reference, to `$VOICE_HOME/evidence/stage1/audio/`.

## Run the llama.cpp demo

From the repo root, with the venv active:

```bash
python voice/speech/llamacpp_rt.py 1 gpu concurrent
```

The script starts `llama-server.exe` on `127.0.0.1:8093` with every layer on the GPU and waits for `/health`. Every clip streams in at real-time pace. The script runs the tool-call probe on `two_request.wav` and prints the tool call, the reply to the injected result, and the cancelled reply. It runs a live-transcription probe on `correction.wav`, one warm-up, and one timed round with the voice and transcription sessions open at once. It stops the server on exit. Output goes to `$VOICE_HOME/evidence/stage1/`: `llamacpp_rt_gpu_concurrent.json`, the reply in `llamacpp_reply_gpu_concurrent.wav`, and the server log.

To run without the GPU, pass `cpu` and hide the GPU:

```bash
CUDA_VISIBLE_DEVICES= python voice/speech/llamacpp_rt.py 1 cpu concurrent
```

Run `python voice/speech/llamacpp_rt.py --help` for the arguments. To reproduce every Stage 1 table, follow the steps in [Reproduce](../../docs/voice/stage1-speech-path.md#reproduce).
