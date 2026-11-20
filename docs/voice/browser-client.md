# Talk to the voice assistant from a browser

Use the browser client to talk to the live server from a computer or phone on your tailnet.

## Start the live server

Set up the model and environment as described in the [live demo runbook](live-demo.md#runbook). From Git Bash on the machine that runs the model, start the server on loopback.

```bash
python ~/.gpu/gpu_lease.py run --holder voice-live --purpose "browser voice call" --max-minutes 30 -- \
  "$VOICE_HOME/venv-lfm3/Scripts/python" -m voice.live.server --host 127.0.0.1 --max-minutes 25
```

On that machine, open `http://127.0.0.1:8770/`. Paste the secret from `$VOICE_HOME/live/secret` and select **Save**. The browser stores the secret for this origin.

## Open the page on your phone

Phones require HTTPS for microphone access. On the server machine, run:

```bash
bash voice/live/serve-tailscale.sh 8770 8446
```

Tailscale Serve prints the HTTPS address. Open it on your phone while connected to the tailnet. If Tailscale already uses the port, pick a free HTTPS port.

Copy the secret from `$VOICE_HOME/live/secret` on the Desktop into an item in your password manager. On your phone, copy it from the password manager, paste it into the page, and select **Save**. You only need to do this once per browser and address. Keep the secret out of the URL.

Select **Start** and grant microphone access. Speak when the status shows listening. To control when speech starts and stops, select **PTT** before starting the call, then hold **Hold to talk** while speaking. Select **Stop** to end the call.

Wear headphones to keep the reply out of the mic. The browser requests echo cancellation, noise suppression, and automatic gain control. Echo cancellation through a phone speaker still needs a hardware check. The server saves the same turn records as it does for the Python client.

## Find the recording

Each call is also recorded in its own `call-NNN` folder inside the server's records folder (the `records:` path the server prints), next to the `turn_NNN` files. A call that reconnects in the same server run gets the next number.

- `conversation.wav` is a 16 kHz mono mix of both sides on one timeline. The assistant side is the audio your browser played, so a reply you cut off ends where you cut it, and a line the assistant spoke unprompted sits where you heard it.
- `microphone.wav` and `assistant-playback-001.wav` are the two tracks. `manifest.json` lists them, the turn numbers the call covers, and how it ended.

The page sends the audio it played back to the server over the same connection as the call. Nothing goes anywhere else. A call that ends without **Stop**, such as a closed tab or a dropped connection, is still mixed and its manifest says `"partial": true`, because the last moments the page played may be missing. An unprompted line the assistant speaks is also saved as `turn_NNN_t3_update.wav` with the turn's other audio.

To record nothing, start the server with `--call-recording off`.

## Turn off tailnet access

Run the undo command printed by the script. For port 8446, use:

```bash
tailscale serve --https=8446 off
```
