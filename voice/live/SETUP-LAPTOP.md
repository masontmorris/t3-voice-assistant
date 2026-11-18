# Set up the live voice client on the laptop

The laptop is a thin client. It sends the microphone to the Desktop over Tailscale and plays the reply. Everything else runs on the Desktop.

The zip holds `client.py`, `recording.py`, `requirements-client.txt`, this file, `config.json` with the Desktop's Tailscale address, and `secret`, the shared secret the server checks. Keep `secret` private. Anyone with it and a tailnet connection can talk to the server.

## 1. Python

The pinned `numpy 2.5.5` needs Python 3.12 or later. The client was tested on Python 3.14.6 on the Desktop and 3.14.10 on the laptop (measured). Use 3.12 or later.

Check what is installed, in PowerShell:

```powershell
py -0
```

If no 3.12 or later shows up, install one:

```powershell
winget install -e --id Python.Python.3.14
```

## 2. Unpack and install

The Desktop sends the zip with `tailscale file cp`. On Windows it usually lands in Downloads. If `voice-live-client.zip` is not there, fetch it from the Tailscale inbox with `tailscale file get $HOME\Downloads`. Then unpack and install:

```powershell
Expand-Archive $HOME\Downloads\voice-live-client.zip -DestinationPath $HOME\voice-live-client -Force
cd $HOME\voice-live-client
py -3 -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements-client.txt
```

## 3. Pick the devices

For a speaker call, use the built-in microphone. The client uses the microphone driver's effects by default, including echo cancellation when the driver provides it.

```powershell
.\.venv\Scripts\python client.py --list-devices
```

Note the index or part of the name of the microphone and speakers or headphones. Without `--input` and `--output` the client uses the Windows defaults.

## 4. Run

Start the server on the Desktop first (see `docs/voice/live-demo.md`). Then:

```powershell
.\.venv\Scripts\python client.py --input "Microphone" --output "Speakers"
```

For a headphone call, select your headphones as the output. Add `--raw-mic` if you want to bypass the microphone driver's effects.

It prints `connected` when the server accepts it. Talk, then pause. About 0.7 s of silence ends your turn. The client prints what the server heard and what it said. Talking while it replies interrupts the reply. The client also prints the folder it records to, and records every run by default.

If silence detection cuts you off or never ends your turn, use push-to-talk. Press Enter to start talking and Enter again to stop:

```powershell
.\.venv\Scripts\python client.py --ptt
```

`config.json` names the server as the Desktop's Tailscale name on port 8770. Override it with `--server ws://<desktop-tailscale-name>:8770`.

Stop with Ctrl+C. The client closes the recording and writes its manifest before it exits.

## 5. Find the recording

Each run gets its own new folder under `$HOME\voice\live-client\`. A second run in the same second gets a `-01`, `-02` suffix, so a run never overwrites an earlier one. The folder holds:

- `microphone.wav` is the 16 kHz microphone audio the client sent.
- `assistant-playback-001.wav` is the reply audio the output device actually played. A later file with a higher number holds a playback epoch at a different rate.
- `conversation.wav` is a 16 kHz mono mix of both, on one clock. Pauses and overlap stay where they happened.
- `turn_NNN_reply_*.wav` holds each reply as the server sent it, as before.
- `client.jsonl` holds the client's control log and the transcripts the server returned.
- `manifest.json` holds the session start, both clocks, each track's rate, length and offset, and any audio the recorder had to drop.

The microphone track holds what the client sent, not what the server chose to keep. In push-to-talk mode that includes frames sent while you are not holding Enter. With `--wav`, it holds the simulated input rather than a microphone. A barge-in stops playback, so the audio you never heard is not in the playback track or the conversation. Audio the recorder dropped under disk pressure is counted in the manifest.

Recording is local. Nothing is uploaded. To run without it, add `--no-record`.

## If it does not connect

- `refused` or a timeout: the server is not running, or Windows Firewall on the Desktop blocks the port. The fix is on the Desktop (see the runbook).
- HTTP 401: `secret` does not match the Desktop's. Fetch a fresh zip.
- Closed with code 1013: another client is already connected.
