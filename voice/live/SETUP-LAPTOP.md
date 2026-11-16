# Set up the live voice client on the laptop

The laptop is a thin client. It sends the microphone to the Desktop over Tailscale and plays the reply. Everything else runs on the Desktop.

The zip holds `client.py`, `requirements-client.txt`, this file, `config.json` with the Desktop's Tailscale address, and `secret`, the shared secret the server checks. Keep `secret` private. Anyone with it and a tailnet connection can talk to the server.

## 1. Python

The client was tested on Python 3.14.6. Any Python from 3.11 on should work, because the four packages publish Windows wheels for it (inferred, only 3.14 was run).

Check what is installed, in PowerShell:

```powershell
py -0
```

If no 3.11 or later shows up, install one:

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

Plug in headphones first. There is no echo cancellation. Reply audio from the speakers reaches the microphone, the server hears it as speech, and the reply interrupts itself.

```powershell
.\.venv\Scripts\python client.py --list-devices
```

Note the index or part of the name of the microphone and of the headphones. Without `--input` and `--output` the client uses the Windows defaults.

## 4. Run

Start the server on the Desktop first (see `docs/voice/live-demo.md`). Then:

```powershell
.\.venv\Scripts\python client.py --input "Microphone" --output "Headphones"
```

It prints `connected` when the server accepts it. Talk, then pause. About 0.7 s of silence ends your turn. The client prints what the server heard and what it said. Talking while it replies interrupts the reply.

If silence detection cuts you off or never ends your turn, use push-to-talk. Press Enter to start talking and Enter again to stop:

```powershell
.\.venv\Scripts\python client.py --ptt
```

`config.json` names the server as the Desktop's Tailscale name on port 8770. Override it with `--server ws://<desktop-tailscale-name>:8770`.

Stop with Ctrl+C. Logs and the received reply audio go to `$HOME\voice\live-client\<time>\`.

## If it does not connect

- `refused` or a timeout: the server is not running, or Windows Firewall on the Desktop blocks the port. The fix is on the Desktop (see the runbook).
- HTTP 401: `secret` does not match the Desktop's. Fetch a fresh zip.
- Closed with code 1013: another client is already connected.
