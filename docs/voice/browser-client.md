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

## Call from a T3 thread

The T3 web and desktop apps can open the same call from a thread's composer (see [Talk with a thread](../user/voice-input.md#talk-with-a-thread)). The app's hello names the thread, and the call starts with that thread in view, as if the voice had just spoken about it, and follows it for the call (`Follow.follow`): what it does after the call begins is spoken as news, and the follow ends with the call. When the call ends, however it ends, the server records its transcript on that thread once, through the seam's `record` operation (`fork_env up` adds it to an existing sandbox config), and logs only whether it was recorded and how many lines. Start the server with `--t3 on`. The hello also names the thread's T3 environment, and a seam that serves another environment ends the call with close code 4409, so a thread id that exists in both is never opened on the wrong one. A thread the seam's credential cannot read ends the call with 4404, an archived thread with 4410, and a server without `--t3 on` ends it with 4501. The app shows each.

When the decider picks the thread a request means, the call's thread is marked in its own option ("The person opened this call from this thread.", `CALL_MARK`), and a question it chooses the call's thread for is answered at once, with no "do you mean" first. Shown only as `last_discussed`, "What is this thread working on right now?" went to "unclear" and a follow-up "Is it waiting on anything from me?" to a similar title that was waiting. On recorded hosted d1.2 answers (`voice/t3/eval/record_call_picks.py`: 8 such requests over a listing with a similar title and one with a near twin, 3 samples each) the call's thread was chosen in 47 of 48 marked and 31 of 48 unmarked, and the 14 controls (the other title or another thread named, no thread listed, "it" after a line about another thread) were placed alike, 42 of 42 samples either way. Naming the call's thread by title in the state instead did not hold beside a near twin that waited on the person, and adding it to the mark pulled a request naming the twin to the call's thread. A message, stop or answer is still confirmed. The WAV client opens a call on a thread with `--thread` and `--environment` (`voice/live/client.py`), as the gate's `call-this-thread` and `call-follow-up` scenarios do.

The app's page is a different origin from this server's, which the server accepts the way it accepts any browser: the secret in the first frame. To accept browser calls only from known pages, pass `--allow-origin` once per origin: the scheme, host and port the app is served from. The server's own page always connects.

The server answers only this machine and the tailnet: a peer outside loopback and 100.64.0.0/10 gets a 403 before the secret is read. A request that reaches it through a proxy on this machine (an `X-Forwarded-*` or `Forwarded` header, or a `Host` that is not loopback) must carry `Tailscale-User-Login`, which Tailscale Serve adds for tailnet users, strips from what clients send, and leaves off for Funnel traffic and tagged devices. So the page over `serve-tailscale.sh` works from a person's own device, and the same port shared through Funnel, or opened from a tagged device, is refused.

Calls from the app never record, whatever `--call-recording` says, because the app does not ask for a recording.

## Find the recording

Recording is off unless the server is started with `--call-recording on`. With it on, each call is recorded in its own `call-NNN` folder inside the server's records folder (the `records:` path the server prints), next to the `turn_NNN` files. A call that reconnects in the same server run gets the next number.

- `conversation.wav` is a 16 kHz mono mix of both sides on one timeline. The assistant side is the audio your browser played, so a reply you cut off ends where you cut it, and a line the assistant spoke unprompted sits where you heard it.
- `microphone.wav` and `assistant-playback-001.wav` are the two tracks. `manifest.json` lists them, the turn numbers the call covers, and how it ended.

The page sends the audio it played back to the server over the same connection as the call. Nothing goes anywhere else. A call that ends without **Stop**, such as a closed tab or a dropped connection, is still mixed and its manifest says `"partial": true`, because the last moments the page played may be missing. An unprompted line the assistant speaks is also saved as `turn_NNN_t3_update.wav` with the turn's other audio.

## Turn off tailnet access

Run the undo command printed by the script. For port 8446, use:

```bash
tailscale serve --https=8446 off
```
