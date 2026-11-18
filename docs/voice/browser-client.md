# Browser client: talk from any browser on the tailnet

Mason opens a page, taps Start, speaks, and hears the reply. No install, no laptop client. The page replaces the laptop client for development testing.

## Start it

Start the live server as in the [live demo runbook](live-demo.md#runbook). It serves the page on the same port as the socket.

On the Desktop, open `http://127.0.0.1:8770/`. Paste the secret from `$VOICE_HOME/live/secret` once and press Save. The page keeps it in `localStorage`. Press Start, talk, press Stop when done.

## Open it on a phone

Phones grant the microphone only on a secure origin, so the page must come over HTTPS. `tailscale serve` wraps the loopback server in tailnet HTTPS:

```bash
voice/live/serve-tailscale.sh 8770 8446
```

Then open `https://<desktop-tailnet-name>:8446/` on the phone. It asks for the secret once, same as above. Nothing is public: serve stays inside the tailnet, and funnel is never used.

Turn serving off with the line the script prints:

```bash
tailscale serve --https=8446 off
```

That removes only this mount. Other serve mounts on the machine are untouched. `tailscale serve reset` would remove those too, so the script never calls it.

## Echo

Headphones are still the safe default. The page asks for echo cancellation, noise suppression and gain control, but a phone on speakers has not been tried at this head. If the reply comes back through the mic, the server hears its own voice as speech and the reply interrupts itself. Use headphones until a speaker run says otherwise.

## What the page does

- First frame after the socket opens is `auth {secret}`. It must arrive before any audio and within 5 s, or the server closes the socket. The secret never goes in a URL, a log or the page source.
- Audio up is 16 kHz mono PCM16, the same frames the Python client sends. Audio down plays at the `reply_start` rate through a jitter buffer.
- Talking over a reply ducks it. The server either stops it, and the page drops what it buffered, or finds a backchannel and turns the reply back up.
- Start and Stop buttons. AudioContext needs the tap, which iOS requires. A VAD or push-to-talk toggle. In push-to-talk, hold the talk button. A status line shows connected, listening, thinking or speaking, plus ping round trip. A level meter moves only while microphone audio flows.
- The browser records nothing. The server keeps recording turns as it does for the Python client.

## Checks

Python tests for the new server paths live beside the live tests:

```bash
python -m unittest voice.live.tests.test_browser
```

The resampler the page shares has its own JS tests, run with Node's built-in runner and no dependencies:

```bash
node --test voice/live/web/resample.test.js
```
