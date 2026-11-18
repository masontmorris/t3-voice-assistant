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

## Turn off tailnet access

Run the undo command printed by the script. For port 8446, use:

```bash
tailscale serve --https=8446 off
```
