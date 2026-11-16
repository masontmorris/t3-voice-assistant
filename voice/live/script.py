"""Renders a scripted conversation to WAV clips with the running server's own TTS, for a live `--wav` run.

    python -m voice.live.script --turns turns.txt --out DIR [--server http://127.0.0.1:8095]

One utterance per line of `--turns`; blank lines and lines starting with `#` are skipped. Each line becomes
`turn_001.wav`, `turn_002.wav` and so on in `--out`, in the order a client should send them, plus `manifest.json`
with the text of each clip. The clips are the model's own voice, so a scripted conversation tests the same path a
microphone does without borrowing anyone's audio.

The server must already be running with LFM3 loaded (it is started by `voice.live.server`). llama.cpp has taken the
TTS request and its voice names in more than one shape across versions, so the first line is tried against each shape
and each voice name `GET /props` offers, and the pair that returns audio is used for the rest.
"""

import argparse
import io
import json
import sys
import urllib.error
import urllib.request
import wave
from pathlib import Path

TEMPLATES = {
    "/v1/audio/speech": {"input": "{text}", "voice": "{voice}"},
    "/tts": {"input": "{text}", "voice": "{voice}"},
}
SHAPES = list(TEMPLATES.items())
RAW_RATE = 24_000


def candidates(endpoint=None):
    return [(endpoint, TEMPLATES.get(endpoint, TEMPLATES["/v1/audio/speech"]))] if endpoint else SHAPES


def voices(server):
    """The voice names the server offers, from GET /props; empty when it names none."""
    try:
        with urllib.request.urlopen(server + "/props", timeout=30) as response:
            props = json.loads(response.read())
    except (urllib.error.URLError, ValueError):
        return []
    found = props.get("voices")
    return [v if isinstance(v, str) else v.get("name") for v in found] if isinstance(found, list) else []


def post(url, payload, timeout=120):
    body = json.dumps(payload).encode()
    request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def sounds_like_audio(blob):
    return len(blob) > 1000 and (blob[:4] == b"RIFF" or b"WAVE" in blob[:64])


def render(server, text, voices, shapes):
    """The first (voice, shape) that returns audio for `text`."""
    complaints = []
    for voice in voices:
        for endpoint, template in shapes:
            payload = {key: value.format(text=text, voice=voice) for key, value in template.items()}
            try:
                blob = post(server + endpoint, payload)
            except urllib.error.HTTPError as exc:
                complaints.append(f"{endpoint} voice {voice!r}: HTTP {exc.code} {exc.read()[:160]!r}")
                continue
            except urllib.error.URLError as exc:
                raise SystemExit(f"no server at {server}: {exc}")
            if sounds_like_audio(blob):
                return endpoint, voice, blob
            complaints.append(f"{endpoint} voice {voice!r}: {len(blob)} bytes that are not audio")
    for line in complaints:
        print(line, file=sys.stderr, flush=True)
    raise SystemExit("no TTS shape returned audio against this llama.cpp build; try --endpoint or --voice")


def frames(blob):
    """(rate, seconds) of a RIFF WAV, or of raw PCM16 at RAW_RATE when the body carries no header."""
    if blob[:4] != b"RIFF":
        return RAW_RATE, len(blob) / 2 / RAW_RATE
    with wave.open(io.BytesIO(blob)) as wav:
        return wav.getframerate(), wav.getnframes() / wav.getframerate()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--turns", type=Path, required=True, help="one utterance per line")
    parser.add_argument("--out", type=Path, required=True, help="directory for the clips and the manifest")
    parser.add_argument("--server", default="http://127.0.0.1:8095")
    parser.add_argument("--endpoint", help="use this TTS path instead of probing")
    parser.add_argument("--voice", help="the server's voice name; the first one in GET /props when left out")
    args = parser.parse_args()
    lines = [line.strip() for line in args.turns.read_text(encoding="utf-8").splitlines()]
    lines = [line for line in lines if line and not line.startswith("#")]
    if not lines:
        parser.error(f"{args.turns} has no utterances")
    offered = voices(args.server)
    voice = args.voice or (offered[0] if offered else "female")
    print(f"voice {voice!r} of {offered or 'no names in /props'}", flush=True)
    shapes = candidates(args.endpoint)
    args.out.mkdir(parents=True, exist_ok=True)
    manifest = []
    for index, text in enumerate(lines, start=1):
        endpoint, voice, blob = render(args.server, text, [voice, *(v for v in offered if v != voice)],
                                       shapes)
        shapes = candidates(endpoint)
        path = args.out / f"turn_{index:03d}.wav"
        path.write_bytes(blob)
        rate, seconds = frames(blob)
        manifest.append({"turn": index, "file": path.name, "text": text, "endpoint": endpoint, "voice": voice,
                         "rate": rate, "audio_s": round(seconds, 3)})
        print(f"{path.name} {seconds:.2f} s {text!r}", flush=True)
    (args.out / "manifest.json").write_text(json.dumps({"server": args.server, "voice": voice, "turns": manifest},
                                                       indent=1) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
