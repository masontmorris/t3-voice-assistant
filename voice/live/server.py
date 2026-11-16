"""Live mode: microphone audio streams in over the tailnet, the prototype's pipeline runs each turn, and the reply
audio streams back.

    python -m voice.live.server [--host IP] [--port 8770] [--prompt FILE] [--silence-ms 700] [--max-minutes 55]

One client at a time connects over WebSocket with the shared secret in the X-Voice-Secret header. The secret is made
once in $VOICE_HOME/live/secret, read in-process, and never printed. The server binds to this machine's Tailscale
IPv4 address (`tailscale ip -4`) and refuses any address outside 100.64.0.0/10 other than loopback.

Wire format. Binary frames are PCM16 mono: 16 kHz up from the client, LFM3's output rate down, 40 ms per frame
down. Text frames are JSON control messages:
  client -> server  hello {mode: vad|ptt}, ping {t}, ptt {down}
  server -> client  ready, pong {t}, turn_start, turn_end, reply_start {rate}, reply_end, barge_in, turn_done

Each turn runs `voice.prototype.run.run_clip` with the task gate and LFM3's end-of-utterance authority (injection (c),
the hybrid segmenter, the `gated` default from `voice/main`): live ASR, the segmenter and decider, the task service
with real weather and time lookups, the snapshot, and the reply. The endpointer in endpoint.py ends a turn after
--silence-ms of silence. Speech that starts while a reply plays cancels that turn (barge-in): the server stops sending
its audio, tells the client to drop what it buffered, and closes the turn's sessions. Speech that starts after a turn
ended but before any reply audio continues the turn: the new turn gets the old one's audio first.

The persona comes from --prompt (default $VOICE_HOME/live/prompt.txt), read again at each turn start, so a new prompt
takes effect on the next turn. When the file is missing the prototype's built-in persona is used.

Needs the GPU (run it inside the lease) and VOICE_ENV_FILE or OPENROUTER_API_KEY for the cloud decider. Writes one
JSON record and the turn's input and reply audio per turn under --out, all under $VOICE_HOME, never in the repo.
Stops on Ctrl+C, when $VOICE_HOME/live/STOP appears, or after --max-minutes.
"""

import argparse
import asyncio
import base64
import hashlib
import hmac
import ipaddress
import json
import secrets
import subprocess
import threading
import time
from contextlib import nullcontext
from datetime import datetime
from http import HTTPStatus
from pathlib import Path

from websockets.asyncio.server import serve

from voice.live.endpoint import RATE, Audio, End, Endpointer, Settings, Start
from voice.local_decider.client import decider as make_decider, default_decider
from voice.prototype.realtime import VOICE_HOME, Server, write_pcm16
from voice.prototype.run import PERSONA, run_clip
from voice.prototype.segmenter import SEGMENTERS, use_decider_cache
from voice.tools.registry import open_registry

LIVE_HOME = VOICE_HOME / "live"
SECRET_FILE = LIVE_HOME / "secret"
PROMPT_FILE = LIVE_HOME / "prompt.txt"
STOP_FILE = LIVE_HOME / "STOP"
EVIDENCE = VOICE_HOME / "evidence" / "live"
HEADER = "X-Voice-Secret"
TAILNET = ipaddress.ip_network("100.64.0.0/10")
UP_CHUNK_BYTES = RATE * 40 // 1000 * 2
DOWN_CHUNK_MS = 40


def shared_secret(path=SECRET_FILE):
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(path, "x", encoding="utf-8") as handle:
            handle.write(secrets.token_hex(32))
    except FileExistsError:
        pass
    return path.read_text(encoding="utf-8").strip()


def tailscale_ipv4():
    result = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True, check=True, timeout=10)
    return result.stdout.split()[0]


def check_bind(host):
    address = ipaddress.ip_address(host)
    if not (address in TAILNET or address.is_loopback):
        raise SystemExit(f"refusing to bind {host}: only a Tailscale address (100.64.0.0/10) or loopback")
    return host


def authorize(secret):
    def check(connection, request):
        if request.headers.get("Origin"):
            return connection.respond(HTTPStatus.FORBIDDEN, "browser clients are refused\n")
        if not hmac.compare_digest(request.headers.get(HEADER, "").encode(), secret.encode()):
            print(f"refused a connection from {connection.remote_address[0]}: bad or missing secret", flush=True)
            return connection.respond(HTTPStatus.UNAUTHORIZED, "bad or missing secret\n")
        return None
    return check


def load_persona(path):
    """The persona text and a label for the record: the file's name and hash, or the built-in default."""
    if path.is_file():
        text = path.read_text(encoding="utf-8").strip()
        if text:
            return text, f"{path.name} sha256:{hashlib.sha256(text.encode()).hexdigest()[:12]}"
    return PERSONA, "built-in default"


class TurnCancelled(RuntimeError):
    pass


class TurnDecider:
    """The shared decider, one call at a time across turns. A cancelled turn's segmenter thread stops at its next call."""

    def __init__(self, inner, lock):
        self.inner, self.lock, self.closed = inner, lock, False

    def call(self, route, body, label=""):
        with self.lock:
            if self.closed:
                raise TurnCancelled("turn cancelled")
            return self.inner.call(route, body, label=label)

    def __getattr__(self, name):
        return getattr(self.inner, name)


class MicTurn:
    """One turn's microphone audio, the source `run_clip` streams from, plus the turn's live timings."""

    rate = RATE

    def __init__(self, n, pcm):
        self.n, self.name = n, f"turn_{n:03d}"
        self.stem = self.name
        self.queue, self.pcm, self.audio_s = asyncio.Queue(), bytearray(), None
        self.started = time.perf_counter()
        self.speech_end = self.turn_end = self.first_audio = None
        self.out_rate, self.in_reply, self.reply_started = None, False, False
        self.muted, self.cancelled, self.carried_s = False, None, 0.0
        self.task = self.decider = None
        self.feed(pcm)

    def feed(self, pcm):
        self.pcm += pcm
        self.queue.put_nowait(pcm)

    def end(self, trailing_ms):
        self.turn_end = time.perf_counter()
        self.speech_end = self.turn_end - trailing_ms / 1000
        self.queue.put_nowait(None)

    def cancel(self, reason):
        self.muted, self.cancelled = True, reason
        if self.decider is not None:
            self.decider.closed = True
        self.task.cancel()

    async def stream(self, voice, asr):
        while (chunk := await self.queue.get()) is not None:
            for offset in range(0, len(chunk), UP_CHUNK_BYTES):
                audio = base64.b64encode(chunk[offset:offset + UP_CHUNK_BYTES]).decode()
                await asyncio.gather(voice.send(type="input_audio_buffer.append", audio=audio),
                                     asr.send(type="input_audio_buffer.append", audio=audio))
        self.audio_s = len(self.pcm) / 2 / self.rate
        return voice.clock()

    def metrics(self):
        def since(t, origin):
            return None if t is None or origin is None else round(t - origin, 4)
        return {
            "carried_audio_s": round(self.carried_s, 3),
            "detector_s": since(self.turn_end, self.speech_end),
            "speech_end_to_first_audio_sent_s": since(self.first_audio, self.speech_end),
            "turn_end_to_first_audio_sent_s": since(self.first_audio, self.turn_end),
            "cancelled": self.cancelled,
        }


class Conversation:
    """One connected client: its endpointer, its turns, and an outbox that keeps control and audio frames in order."""

    def __init__(self, live, ws):
        self.live, self.ws = live, ws
        self.endpointer = Endpointer(live.settings)
        self.mode, self.ptt_down, self.turn = "vad", False, None
        self.outbox = asyncio.Queue()
        self.sender = asyncio.create_task(self._send_loop())

    def post(self, message, turn=None):
        self.outbox.put_nowait((turn, json.dumps(message)))

    def post_audio(self, turn, pcm):
        step = turn.out_rate * DOWN_CHUNK_MS // 1000 * 2
        for offset in range(0, len(pcm), step):
            self.outbox.put_nowait((turn, pcm[offset:offset + step]))

    async def _send_loop(self):
        while True:
            turn, payload = await self.outbox.get()
            if isinstance(payload, bytes) and turn is not None and turn.muted:
                continue
            await self.ws.send(payload)

    async def run(self):
        async for message in self.ws:
            if isinstance(message, bytes):
                self.on_audio(message)
            else:
                await self.on_control(json.loads(message))

    def on_audio(self, pcm):
        if self.mode == "ptt":
            if self.ptt_down and self.turn is not None and self.turn.turn_end is None:
                self.turn.feed(pcm)
            return
        for event in self.endpointer.push(pcm):
            if isinstance(event, Start):
                self.start_turn(event.pcm)
            elif isinstance(event, Audio):
                self.turn.feed(event.pcm)
            elif isinstance(event, End):
                self.end_turn(event.trailing_ms, event.forced)

    async def on_control(self, message):
        kind = message.get("type")
        if kind == "hello":
            self.mode = "ptt" if message.get("mode") == "ptt" else "vad"
            await self.ws.send(json.dumps({"type": "ready", "mode": self.mode, **vars(self.live.settings)}))
        elif kind == "ping":
            await self.ws.send(json.dumps({"type": "pong", "t": message.get("t")}))
        elif kind == "ptt" and self.mode == "ptt":
            down = bool(message.get("down"))
            if down and not self.ptt_down:
                self.start_turn(b"")
            elif self.ptt_down and not down and self.turn is not None:
                self.end_turn(0, False)
            self.ptt_down = down

    def start_turn(self, pcm):
        prior, carry = self.turn, b""
        if prior is not None and not prior.task.done():
            if prior.reply_started:
                prior.cancel("barge_in")
                self.post({"type": "barge_in", "turn": prior.n})
            else:
                prior.cancel("continued")
                carry = bytes(prior.pcm)
        self.live.turns += 1
        turn = MicTurn(self.live.turns, carry + pcm)
        turn.carried_s = len(carry) / 2 / RATE
        turn.task = asyncio.create_task(self.run_turn(turn, prior.task if prior is not None else None))
        self.turn = turn
        self.post({"type": "turn_start", "turn": turn.n, "carried_audio_s": round(turn.carried_s, 3)})

    def end_turn(self, trailing_ms, forced):
        self.turn.end(trailing_ms)
        self.post({"type": "turn_end", "turn": self.turn.n, "trailing_ms": trailing_ms, "forced": forced})

    def on_voice_event(self, turn):
        def handle(event):
            kind = event.get("type")
            if kind == "session.created":
                turn.out_rate = event["session"]["audio"]["output"]["format"]["rate"]
            if turn.muted:
                return
            if kind == "response.output_audio.delta":
                if not turn.in_reply:
                    turn.in_reply = turn.reply_started = True
                    turn.first_audio = turn.first_audio or time.perf_counter()
                    self.post({"type": "reply_start", "turn": turn.n, "rate": turn.out_rate}, turn)
                self.post_audio(turn, base64.b64decode(event["delta"]))
            elif kind == "response.done" and turn.in_reply:
                turn.in_reply = False
                self.post({"type": "reply_end", "turn": turn.n}, turn)
        return handle

    async def run_turn(self, turn, prior):
        live = self.live
        persona, prompt_source = load_persona(live.prompt_file)
        turn.decider = TurnDecider(live.decider, live.decider_lock)
        partial = {}
        try:
            if prior is not None:
                await asyncio.gather(prior, return_exceptions=True)
            record = await run_clip(turn, "c", False, live.url, turn.decider, live.args.delay, live.out, partial,
                                    live.args.segmenter, live.tools, "gated", persona=persona,
                                    on_voice_event=self.on_voice_event(turn))
        except asyncio.CancelledError:
            record = {"clip": turn.name, "cancelled": turn.cancelled, **partial}
        except Exception as exc:
            record = {"clip": turn.name, "error": repr(exc), **partial}
        turn.decider.closed = True
        record["live"] = {**turn.metrics(), "prompt": prompt_source, "endpointer": vars(live.settings),
                          "mode": self.mode}
        write_pcm16(live.out / f"{turn.name}_input.wav", bytes(turn.pcm), RATE)
        (live.out / f"{turn.name}.json").write_text(json.dumps(record, indent=1) + "\n", encoding="utf-8")
        done = {"type": "turn_done", "turn": turn.n, "transcript": record.get("final_transcript"),
                "replies": [r.get("text") for r in record.get("replies", [])], "error": record.get("error"),
                "metrics": {**record.get("metrics", {}), **turn.metrics()}}
        self.post(done)
        print(f"{turn.name} {json.dumps(done['metrics'])}", flush=True)

    async def close(self):
        if self.turn is not None and not self.turn.task.done():
            self.turn.cancel("disconnected")
            await asyncio.gather(self.turn.task, return_exceptions=True)
        self.sender.cancel()


class Live:
    def __init__(self, args, url, decider, tools, out):
        self.args, self.url, self.decider, self.tools, self.out = args, url, decider, tools, out
        self.settings = Settings(silence_ms=args.silence_ms, start_ms=args.start_ms, preroll_ms=args.preroll_ms,
                                 floor_dbfs=args.floor_dbfs, margin_db=args.margin_db, max_turn_s=args.max_turn_s)
        self.prompt_file, self.decider_lock = args.prompt, threading.Lock()
        self.active, self.turns = None, 0

    async def handler(self, ws):
        if self.active is not None:
            await ws.close(1013, "another client is connected")
            return
        conversation = self.active = Conversation(self, ws)
        print(f"client connected from {ws.remote_address[0]}", flush=True)
        try:
            await conversation.run()
        finally:
            await conversation.close()
            self.active = None
            print("client disconnected", flush=True)


async def wait_for_stop(max_minutes):
    deadline = time.monotonic() + max_minutes * 60
    while time.monotonic() < deadline:
        if STOP_FILE.exists():
            print("STOP file found", flush=True)
            return
        await asyncio.sleep(1)
    print(f"stopping after --max-minutes {max_minutes}", flush=True)


async def main_async(args):
    host = check_bind(args.host or tailscale_ipv4())
    secret = shared_secret()
    STOP_FILE.unlink(missing_ok=True)
    use_decider_cache(args.decider_cache, args.decider_budget)
    out = args.out or EVIDENCE / datetime.now().strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    lfm_args = ("-c", str(args.lfm_ctx))
    tools_context = nullcontext() if args.mock_tools else open_registry()
    with tools_context as tools, Server(args.lfm_port, out / "llama-server.log", lfm_args, 3) as server, \
            make_decider(args.decider, out) as decider:
        live = Live(args, f"ws://127.0.0.1:{args.lfm_port}/v1/realtime", decider, tools, out)
        async with serve(live.handler, host, args.port, process_request=authorize(secret), max_size=2 ** 20):
            _, prompt_source = load_persona(args.prompt)
            print(f"llama-server pid {server.proc.pid} healthy after {server.startup_s:.1f} s", flush=True)
            print(f"live server on ws://{host}:{args.port}, prompt: {prompt_source}, records: {out}", flush=True)
            await wait_for_stop(args.max_minutes)
            if live.active is not None:
                await live.active.ws.close(1001, "server stopping")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", help="bind address; this machine's Tailscale IPv4 when left out")
    parser.add_argument("--port", type=int, default=8770)
    parser.add_argument("--prompt", type=Path, default=PROMPT_FILE, help="persona file, read at each turn start")
    parser.add_argument("--silence-ms", type=int, default=700, help="trailing silence that ends a turn")
    parser.add_argument("--start-ms", type=int, default=100, help="voiced audio that starts a turn")
    parser.add_argument("--preroll-ms", type=int, default=300, help="audio kept from before the turn started")
    parser.add_argument("--floor-dbfs", type=float, default=-50.0, help="a frame below this level is never speech")
    parser.add_argument("--margin-db", type=float, default=10.0, help="how far above the noise floor speech must be")
    parser.add_argument("--max-turn-s", type=float, default=30.0, help="end a turn that runs this long")
    parser.add_argument("--max-minutes", type=float, default=55.0, help="stop the server after this long")
    parser.add_argument("--segmenter", choices=sorted(SEGMENTERS), default="hybrid")
    parser.add_argument("--decider", choices=["cloud", "d1"], default=default_decider())
    parser.add_argument("--delay", type=float, default=4.0, help="seconds a mock task takes (default 4)")
    parser.add_argument("--mock-tools", action="store_true", help="no real lookups: every task runs on the mock")
    parser.add_argument("--lfm-port", type=int, default=8095)
    parser.add_argument("--lfm-ctx", type=int, default=32768, help="LFM3's total context (default 32768)")
    parser.add_argument("--out", type=Path, help="records directory; a new one under $VOICE_HOME/evidence/live")
    parser.add_argument("--decider-cache", type=Path, default=EVIDENCE / "decider-cache")
    parser.add_argument("--decider-budget", type=int, default=200, help="stop when the ledger holds this many attempts")
    args = parser.parse_args()
    if args.segmenter != "hybrid":
        parser.error("the gated turn needs the hybrid segmenter")
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
