"""Live mode: microphone audio streams in over the tailnet, the prototype's pipeline runs each turn, and the reply
audio streams back.

    python -m voice.live.server [--host IP] [--port 8770] [--prompt FILE] [--silence-ms 700] [--max-minutes 55]

One client at a time connects over WebSocket with the shared secret in the X-Voice-Secret header. The secret is made
once in $VOICE_HOME/live/secret, read in-process, and never printed. The server binds to this machine's Tailscale
IPv4 address (`tailscale ip -4`) and refuses any address outside 100.64.0.0/10 other than loopback.

The browser client at voice/live/web/ is served from the same port: GET / (and its scripts) answers the page, and a
browser proves itself with `auth {secret}` as its first text frame, within AUTH_TIMEOUT_S of the handshake, since it
cannot set the header. The secret never goes in a URL, a log or the page.

Wire format. Binary frames are PCM16 mono: 16 kHz up from the client, LFM3's output rate down, 40 ms per frame
down. Text frames are JSON control messages:
  client -> server  hello {mode: vad|ptt}, ping {t}, ptt {down}
  server -> client  ready, pong {t}, turn_start, turn_end, reply_start {rate}, reply_end, duck, unduck, barge_in,
                    turn_done

Each turn runs `voice.prototype.run.run_clip` with the task gate and LFM3's end-of-utterance authority (injection (c),
the hybrid segmenter, the `gated` default from `voice/main`): live ASR, the segmenter and decider, the task service
with real weather and time lookups, the snapshot, and the reply. The endpointer in endpoint.py ends a turn after
--silence-ms of silence, or, with `--eou audio`, as soon as the local d1-Omni-2B says the person has finished
(voice/live/eou_audio.py), and never later than --silence-ms.

One client connection is one call, and one call is one LFM3 voice session. The first turn opens it and every later
turn adds its audio and its reply to the same conversation. That session is the call's memory: the model hears what
the person and the assistant said on earlier turns, and no text copy of the call is ever put in front of it. The ASR,
segmenter, task service and tools session still start fresh per turn. A turn's audio goes to the ASR as it arrives
and to the voice session once, when the turn ends, so a turn that is cancelled leaves nothing behind in the call.

Speech that starts while a reply is still playing on the client is an overlap (voice/live/overlap.py). The client
ducks the reply, and the overlap keeps its audio until a verdict. Enough voiced speech makes it a barge-in: the
server mutes the turn, tells the client to drop what it buffered, and lets the turn finish, so the call's one
response is never cut off mid-stream and the history stays whole. The overlap's audio opens the next turn, which
starts once the previous one finishes, on the same session. A shorter overlap is transcribed once: a backchannel,
noise or the reply's own echo is dismissed and the reply plays on at full volume, and anything else barges in. Speech
after the reply has played starts a turn at once and drops only a still-running turn's later audio. Speech that starts
after a turn ended but before any reply audio continues that turn: the new turn gets the old one's audio first.

Every barge-in, by voice or push-to-talk, records what the person heard, since the muted reply still finishes inside
the session: the server reads back the part of the turn's replies the client had played (`played_part`), and keeps the
words the read-back matched (`heard_prefix`), or all of them when the reply had finished playing. `--barge-in-note on`
also puts one short note before the new turn's audio saying the person interrupted and heard only the reply's start.
It ships off: a 9-case A/B found no gain (docs/voice/barge-in.md). The note does not quote the words, because quoting
them made the model go on with the old reply (measured).

--snapshot picks what machine text enters the voice session. "note", the default, sends one short private note
after the turn's audio, and only on a turn that has a lookup to speak about: that it is running, its result, or
that it could not be done (`voice/prototype/run.py`, `task_note`). A turn with no lookup sends nothing, so the model
answers the audio it just heard. The measured reason is the call recorded in 20261116-152653: every reply was
preceded by a JSON list of the person's earlier turns, and the model answered the last line of that list, which is
the previous turn, in place of the audio. llama.cpp cannot delete an item from a session (measured:
`conversation.item.delete` is an unknown event type), so what goes in stays for the call. The older modes remain
for rerunning earlier experiments: "reply-no-utterance" and "reply" send the task snapshot as JSON before the reply,
"before-audio" sends the call's task state before the turn's audio, and "off" sends nothing. They also take
--recall, the numbered `call_turns` list and the recall sentence, which note mode never sends.

The call's one voice session is never restarted and never rebuilt from text. The recorded call restarted it every
eight turns and seeded the new session with the call's own text, and the first answered turn after each of its three
restarts was wrong: turn 10 answered the first line of the seed, and turns 17 and 28 repeated the person's words
(measured: 20261116-152653). A session's instructions are fixed once it holds a message, so the persona is read
when the call's first turn starts and a changed prompt file applies from the next call. How many turns fit in the
slot is in docs/voice/live-call3-fix.md.

A reply is checked before it is heard, and heard while it is generated. `voice/live/facts.py` reads the days, dates,
times, numbers and places in the reply and asks whether the person said them in this call or a lookup gave them; the
assistant's own earlier replies do not count, except a place it chose when the person asked it to choose. The turn
screens the reply's text as it arrives and sends each block of audio once the text up to the block after it has
passed (`voice/prototype/release.py`), so the first audio goes out about 0.2 s after the model starts speaking instead
of after the whole reply. A reply that fails, that talks about the note, that speaks of the person or of itself in the
third person, or that repeats a line the assistant already said in this call, stops at the last block that passed;
a reply that could still turn into a repeat releases nothing until it is clear. The turn says so in its record,
sends one short note, and asks again once. A reply stopped part way is asked to go on from what the person heard,
without saying it again. In note mode a turn that has still said nothing gets one last ask for a short reply with no
facts in it, and only then is it silent, recorded as blocked. No fixed line is played, because a line rendered by the
TTS endpoint is a different voice from the call's (measured: turns 22 and 26 of the recorded call, after which the
person asked why it sounded different). When the registry has a general worker (`voice/tools/agent.py`,
VOICE_AGENT=on), a reply that states a fact nothing gave it starts a lookup for the person's words in place of the
retry.

With --t3 on, news from a T3 thread the voice started or sent to (it is working, its reply, it finished) is spoken
while the call is quiet, as its own short turn (`IdleTurn`). Quiet means no turn running, no reply playing, nobody
speaking, and IDLE_GRACE_S since the last of them. Every update carries typed acts. The writer drafts a line, then
the call's voice session says it. Both texts pass the same checks before the client gets any audio. The client can
duck or interrupt it, and the record keeps the act's evidence. A line that does not play goes back to wait.

The persona comes from --prompt (default $VOICE_HOME/live/prompt.txt), read when a call's first turn starts. When
the file is missing, note mode uses `NOTE_PERSONA` and the older modes the prototype's built-in persona.

Needs the GPU (run it inside the lease) and, for the decider, LIQUID_API_KEY (liquid, the default) or
OPENROUTER_API_KEY (cloud) from VOICE_ENV_FILE. Writes one JSON record and the turn's input and reply audio per turn
under --out, all under $VOICE_HOME, never in the repo. Stops on Ctrl+C, when $VOICE_HOME/live/STOP appears, or after
--max-minutes.
"""

import argparse
import asyncio
import base64
import hashlib
import hmac
import ipaddress
import json
import re
import secrets
import subprocess
import threading
import time
from contextlib import nullcontext
from dataclasses import asdict, dataclass, field
from datetime import datetime
from functools import partial
from http import HTTPStatus
from pathlib import Path

from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

from voice.live import eou_audio
from voice.live.endpoint import RATE, Audio, Check, End, Endpointer, Settings, Start, State
from voice.live.overlap import Overlap, OverlapSettings, Playback, Verdict, classify, played_part, transcribe
from voice.local_decider.client import decider as make_decider, default_decider
from voice.prototype.realtime import VOICE_HOME, Server, Session, write_pcm16
from voice.prototype.run import (FORMAT_LINES, NOTE_PERSONA, PERSONA, T3_RELAY, by_ear, create_item,
                                 heard_prefix, interrupted_note, plain_text, run_clip, say_acts, update_session)
from voice.live.ledger import ConversationLedger
from voice.t3.speech import speakable
from voice.live.t3_tasks import Poller, T3Registry, T3TaskService, T3Tasks, Words, default_journal, \
    default_model, default_origin, default_token_file
from voice.t3.verify import default_decider as default_speech_decider
from voice.prototype.segmenter import SEGMENTERS, use_decider_cache
from voice.tools.registry import open_registry

LIVE_HOME = VOICE_HOME / "live"
SECRET_FILE = LIVE_HOME / "secret"
PROMPT_FILE = LIVE_HOME / "prompt.txt"
STOP_FILE = LIVE_HOME / "STOP"
EVIDENCE = VOICE_HOME / "evidence" / "live"
HEADER = "X-Voice-Secret"
WEB_DIR = Path(__file__).resolve().parent / "web"
WEB_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/resample.js": ("resample.js", "text/javascript; charset=utf-8"),
    "/capture-worklet.js": ("capture-worklet.js", "text/javascript; charset=utf-8"),
    "/playback-worklet.js": ("playback-worklet.js", "text/javascript; charset=utf-8"),
}
# A browser cannot set X-Voice-Secret on its WebSocket handshake, so the page sends `auth {secret}` as its first
# text frame instead. The frame must arrive within this long after the socket opens, before any audio.
AUTH_TIMEOUT_S = 5.0
TAILNET = ipaddress.ip_network("100.64.0.0/10")
UP_CHUNK_BYTES = RATE * 40 // 1000 * 2
DOWN_CHUNK_MS = 40
# One slot is held for the whole call by the call's one voice session. A turn also needs a slot for its ASR and one
# for the reply it generates, and the previous turn's reply can still be generating while the next turn's ASR runs.
# With two slots all 39 llama-server tasks of the 2026-11-16 17:38 replay ran on the one free slot, one at a time, and
# five turns' final transcripts arrived 27 to 30 s after their audio, against 0.03 to 0.28 s in the recorded call's
# three-slot server (measured).
SLOTS = 3
PLAYED_ESTIMATE = ("the server's estimate from the audio it sent and its playback clock, not the client's report; how "
                   "far it can run past what the client played is not measured")
# The barge-in read-back runs beside the turns, never in front of them; past this bound the record says `heard: null`.
# A guess sized for a few seconds of reply audio through the ASR session, not a measured read-back time.
READBACK_TIMEOUT_S = 5.0
# Thread news is spoken only after the person and the assistant have both been quiet this long. A guess: longer than a
# breath between sentences, and short enough that news does not sit unsaid.
IDLE_GRACE_S = 1.5
# After the first piece of news, the rest of a burst gets this long to arrive, so a run's start, its reply and its end
# can be said in one short turn. A guess, not measured.
COALESCE_S = 1.0
QUIET_CHECK_S = 0.2


def slots(asr_stagger=1):
    """llama-server slots for one call: the voice session, one per ASR lane, and one for the reply being generated."""
    return SLOTS + asr_stagger - 1
CALL_RULES = (
    "\n\nThis is one ongoing call, so what you and the person said earlier in it is still here. Only the newest "
    "task snapshot is current: earlier snapshots belong to earlier turns and may be out of date, so never speak a "
    "task or a result from one unless the newest snapshot has it. A question about what was said earlier is answered "
    "from the snapshot's call_turns, which lists the person's own turns oldest first and numbered; the task list says "
    "only where tasks stand, not what the person asked."
)


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


def serve_page(connection, asset, hosts, web_dir):
    name, content_type = asset
    response = connection.respond(HTTPStatus.OK, (web_dir / name).read_text(encoding="utf-8"))
    del response.headers["Content-Type"]
    response.headers["Content-Type"] = content_type
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    connect_src = "'self'"
    # Some browsers need explicit WebSocket sources even when the socket shares the page's host and port.
    if len(hosts) == 1 and re.fullmatch(r"[A-Za-z0-9.-]+(?::[0-9]{1,5})?", hosts[0]):
        connect_src += f" ws://{hosts[0]} wss://{hosts[0]}"
    response.headers["Content-Security-Policy"] = (
        "default-src 'none'; script-src 'self'; style-src 'self'; "
        f"connect-src {connect_src}; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    )
    return response


def authorize(secret, web_dir=WEB_DIR):
    def check(connection, request):
        if request.headers.get("Upgrade") is None and request.method == "GET":
            asset = WEB_FILES.get(request.path.partition("?")[0])
            if asset is not None:
                return serve_page(connection, asset, request.headers.get_all("Host"), web_dir)
            return connection.respond(HTTPStatus.NOT_FOUND, "not found\n")
        header = request.headers.get(HEADER, "")
        if request.headers.get("Origin") is not None:
            if header and hmac.compare_digest(header.encode(), secret.encode()):
                return connection.respond(HTTPStatus.FORBIDDEN, "header auth is not for browsers\n")
            return None
        if not hmac.compare_digest(header.encode(), secret.encode()):
            print(f"refused a connection from {connection.remote_address[0]}: bad or missing secret", flush=True)
            return connection.respond(HTTPStatus.UNAUTHORIZED, "bad or missing secret\n")
        return None
    return check


def load_persona(path, default=PERSONA):
    """The persona text and a label for the record: the file's name and hash, or the built-in default."""
    if path is not None and path.is_file():
        text = path.read_text(encoding="utf-8").strip()
        if text:
            return text, f"{path.name} sha256:{hashlib.sha256(text.encode()).hexdigest()[:12]}"
    return default, "built-in default"


class TurnCancelled(RuntimeError):
    pass


class TurnDecider:
    """The shared decider, one call at a time across turns. A cancelled turn's segmenter thread stops at its next
    call."""

    def __init__(self, inner, lock):
        self.inner, self.lock, self.closed = inner, lock, False

    def call(self, route, body, label=""):
        with self.lock:
            if self.closed:
                raise TurnCancelled("turn cancelled")
            return self.inner.call(route, body, label=label)

    def __getattr__(self, name):
        return getattr(self.inner, name)


@dataclass
class Reply:
    """One reply a turn spoke: the text its audio was released on, and that audio. A turn can speak twice, an answer
    and then a task update, so a read-back of what played is matched against each reply's own text."""

    text: str = ""
    pcm: bytearray = field(default_factory=bytearray)
    sent_bytes: int = 0
    first_sent: float | None = None


@dataclass
class BargeIn:
    """What a barge-in turn knows of the reply it stopped: the read-back of what had played (`heard_before`) and the
    note it put in the session, if any. A continued turn takes it over with the audio it carries."""

    heard: asyncio.Task
    note: str | None = None
    note_item: dict | None = None

    def result(self):
        return self.heard.result() if self.heard.done() and not self.heard.cancelled() else None

    def record(self):
        if not self.heard.done():
            heard = {"heard": None, "pending": True}
        elif self.heard.cancelled():
            heard = {"heard": None, "error": "read-back cancelled: the call ended"}
        else:
            heard = self.heard.result()
        return {**heard, "note": self.note, "note_item": self.note_item}


def note_for(heard):
    """The note for the session after a barge-in, or None: only when part of the reply was heard and part was not."""
    return interrupted_note() if heard and heard["heard"] and not heard["whole"] else None


@dataclass(frozen=True)
class AudioFrame:
    pcm: bytes
    reply: Reply


@dataclass(frozen=True)
class AudioDelivery:
    done: asyncio.Future[None]


class MicTurn:
    """One turn's microphone audio, the source `run_clip` streams from, plus the turn's live timings."""

    rate = RATE
    idle = False
    # The voice session gets the turn's audio once, when the turn ends. Streamed as it arrived, a turn that was
    # cancelled because the person kept talking left its audio in the session's buffer, and the turn that carried
    # that audio forward sent it again (measured: 20261116-152653 turns 8 to 10, 12 s of speech heard twice).
    voice_at_end = True

    def __init__(self, n, pcm):
        self.n, self.name = n, f"turn_{n:03d}"
        self.stem = self.name
        self.queue, self.pcm, self.audio_s = asyncio.Queue(), bytearray(), None
        self.started = time.perf_counter()
        self.speech_end = self.turn_end = self.first_audio = None
        self.out_rate, self.reply_started, self.speaking, self.replies = None, False, False, []
        self.muted, self.cancelled, self.carried_s = False, None, 0.0
        self.task = self.decider = self.overlap = self.barge_in = None
        self.checks, self.check = [], None
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

    def mute(self, reason):
        """Speech arrived over this turn's reply. The call's voice session is shared, so the turn is not torn down:
        its remaining audio is dropped and it runs to its end, which leaves its reply in the conversation."""
        self.muted, self.cancelled = True, reason

    async def stream(self, voice, asr):
        while (chunk := await self.queue.get()) is not None:
            for offset in range(0, len(chunk), UP_CHUNK_BYTES):
                audio = base64.b64encode(chunk[offset:offset + UP_CHUNK_BYTES]).decode()
                await asr.send(type="input_audio_buffer.append", audio=audio)
        self.audio_s = len(self.pcm) / 2 / self.rate
        return voice.clock()

    async def send_voice(self, voice):
        pcm, step = bytes(self.pcm), RATE * 2
        for offset in range(0, len(pcm), step):
            await voice.send(type="input_audio_buffer.append",
                             audio=base64.b64encode(pcm[offset:offset + step]).decode())

    def metrics(self):
        def since(t, origin):
            return None if t is None or origin is None else round(t - origin, 4)
        return {
            "carried_audio_s": round(self.carried_s, 3),
            "detector_s": since(self.turn_end, self.speech_end),
            "speech_end_to_first_audio_sent_s": since(self.first_audio, self.speech_end),
            "turn_end_to_first_audio_sent_s": since(self.first_audio, self.turn_end),
            "cancelled": self.cancelled,
            "overlap": self.overlap,
            "eou_checks": list(self.checks),
        }


class IdleTurn(MicTurn):
    """A turn the server starts while the call is quiet, to speak T3 news on the usual reply path."""

    idle = True

    def __init__(self, n):
        super().__init__(n, b"")
        self.turn_end = self.speech_end = self.started


class Conversation:
    """One connected client: its endpointer, its turns, and an outbox that keeps control and audio frames in order.

    The client connection is the call, so the conversation owns the call's one voice session and its transcript.
    `voice_turn` is the turn that currently has that session: the turn before it has finished, so its events and the
    new turn's cannot interleave."""

    def __init__(self, live, ws, secret):
        self.live, self.ws = live, ws
        self.sender: asyncio.Task | None = None
        self.secret = secret
        self.endpointer = Endpointer(live.settings)
        self.mode, self.ptt_down, self.turn = "vad", False, None
        self.outbox = asyncio.Queue()
        self.voice, self.voice_turn, self.session_turns, self.out_rate = None, None, 0, None
        self.persona = self.prompt_source = None
        self.ledger = ConversationLedger()
        self.pending, self.started = set(), time.perf_counter()
        self.playback, self.overlap = Playback(), None
        self.checks, self.read_backs = set(), set()
        self.heard_at, self.news = time.perf_counter(), None

    def voice_clock(self):
        """The voice session's clock is the turn's clock: the session outlives the turn, but its events arrive while a
        turn is running, and run_clip stamps its own events from the same start."""
        return time.perf_counter() - self.started

    def post(self, message, turn=None):
        self.outbox.put_nowait((turn, json.dumps(message)))

    def post_audio(self, turn, pcm, reply):
        step = turn.out_rate * DOWN_CHUNK_MS // 1000 * 2
        for offset in range(0, len(pcm), step):
            self.outbox.put_nowait((turn, AudioFrame(pcm[offset:offset + step], reply)))

    async def _send_loop(self):
        try:
            while True:
                turn, payload = await self.outbox.get()
                if isinstance(payload, AudioFrame):
                    if not turn.muted:
                        await self.ws.send(payload.pcm)
                        sent = time.perf_counter()
                        turn.first_audio = turn.first_audio or sent
                        payload.reply.first_sent = payload.reply.first_sent or sent
                        payload.reply.sent_bytes += len(payload.pcm)
                elif isinstance(payload, AudioDelivery):
                    payload.done.set_result(None)
                else:
                    await self.ws.send(payload)
        finally:
            while not self.outbox.empty():
                _, payload = self.outbox.get_nowait()
                if isinstance(payload, AudioDelivery):
                    payload.done.set_result(None)

    async def run(self):
        self.sender = sender = asyncio.create_task(self._send_loop())
        if self.live.t3 is not None:
            self.news = asyncio.create_task(self.speak_news())
        try:
            async for message in self.ws:
                if isinstance(message, bytes):
                    self.on_audio(message)
                else:
                    await self.on_control(json.loads(message))
        finally:
            try:
                await self.close()
            finally:
                sender.cancel()
                await asyncio.gather(sender, return_exceptions=True)

    async def browser_auth(self):
        """The page's first text frame carries the secret, since a browser cannot set the header. It must arrive
        before any audio and within AUTH_TIMEOUT_S, and the secret is never logged."""
        try:
            first = await asyncio.wait_for(self.ws.recv(), AUTH_TIMEOUT_S)
        except TimeoutError:
            await self.ws.close(1008, "auth did not arrive in time")
            return False
        except ConnectionClosed:
            return False
        if not (isinstance(first, str) and first.startswith("auth ")
                and hmac.compare_digest(first[5:].encode(), self.secret.encode())):
            print(f"refused a browser connection from {self.ws.remote_address[0]}: bad or missing auth",
                  flush=True)
            await self.ws.close(1008, "bad or missing auth")
            return False
        return True

    def on_audio(self, pcm):
        if self.mode == "ptt":
            if self.ptt_down and self.turn is not None and self.turn.turn_end is None:
                self.turn.feed(pcm)
            return
        for event in self.endpointer.push(pcm):
            if isinstance(event, Start):
                self.on_start(event.pcm)
            elif isinstance(event, Check):
                if self.overlap is None:
                    self.check_end(event.silent_ms)
            elif isinstance(event, Audio) and self.overlap is None:
                self.turn.feed(event.pcm)
            elif isinstance(event, Audio):
                self.on_overlap_frame(event)
            elif self.overlap is None:
                self.end_turn(event.trailing_ms, event.forced)
            else:
                self.on_overlap_end(event.trailing_ms)

    def reply_audible(self):
        return self.playback.playing(time.perf_counter(), self.live.overlap_settings.grace_ms / 1000)

    def quiet(self, now):
        """Nobody is talking or about to: no turn running, no reply playing, no speech the endpointer is tracking, and
        IDLE_GRACE_S since the person last spoke and since the last reply audio."""
        turn = self.turn
        return (self.overlap is None and not self.ptt_down and self.endpointer.state is State.IDLE
                and (turn is None or turn.task.done()) and not self.playback.playing(now, IDLE_GRACE_S)
                and now - self.heard_at >= IDLE_GRACE_S)

    async def speak_news(self):
        """Speaks T3 news while the call is quiet, as its own short turn. A burst within COALESCE_S is one turn, with
        one line per update. News waits while anyone talks, and lines a turn took but never spoke go back."""
        t3 = self.live.t3
        while True:
            if not t3.has_news(idle=True):
                await self.live.news.wait()
                self.live.news.clear()
                await asyncio.sleep(COALESCE_S)
            while not self.quiet(time.perf_counter()):
                await asyncio.sleep(QUIET_CHECK_S)
            if t3.has_news(idle=True):
                # The turn owns its reservation, even when the idle loop is cancelled.
                await asyncio.wait({self.start_idle().task})

    def start_idle(self):
        self.live.turns += 1
        turn = IdleTurn(self.live.turns)
        turn.task = asyncio.create_task(self.run_idle(turn))
        self.pending.add(turn)
        turn.task.add_done_callback(lambda _: self.pending.discard(turn))
        self.turn = turn
        return turn

    def on_start(self, pcm):
        """Speech while a reply is audible waits for a verdict, holding its audio; other speech starts a turn now. An
        overlap still being transcribed when speech resumes hands its audio to what comes next."""
        held = b""
        self.heard_at = time.perf_counter()
        if self.overlap is not None:
            held = bytes(self.overlap.pcm)
            if self.overlap.task is not None:
                self.overlap.task.cancel()
            self.overlap = None
        if self.turn is not None and self.reply_audible():
            self.overlap = Overlap(self.live.overlap_settings, bytearray(held + pcm), self.playback.turn)
            self.post({"type": "duck", "turn": self.playback.turn})
        else:
            self.start_turn(held + pcm)

    def on_overlap_frame(self, event):
        if self.overlap.feed(event.pcm, event.level_dbfs, self.endpointer.noise_dbfs):
            self.settle(Verdict.BARGE_IN, "voiced")
        elif not self.reply_audible():
            self.settle(None, "reply ended")

    def on_overlap_end(self, trailing_ms):
        overlap = self.overlap
        if overlap.voiced_ms < self.live.overlap_settings.min_voiced_ms:
            self.settle(Verdict.NOISE, "too short")
        else:
            overlap.task = asyncio.create_task(self.classify_overlap(overlap, trailing_ms))

    async def classify_overlap(self, overlap, trailing_ms):
        try:
            text = await self.live.transcribe(bytes(overlap.pcm))
            verdict, how = classify(text, self.playback.text), "transcript"
        except Exception as exc:
            text, verdict, how = None, Verdict.BARGE_IN, f"transcript failed: {exc!r}"
        if self.overlap is overlap:
            self.settle(verdict, how, text, trailing_ms)

    def settle(self, verdict, how, transcript=None, trailing_ms=None):
        """The overlap's verdict. A barge-in, or speech the reply finished under, becomes a turn that opens with all
        of the overlap's audio; a dismissed overlap is logged with its audio and the reply plays on."""
        overlap, self.overlap = self.overlap, None
        entry = overlap.record(verdict or "turn", how, time.perf_counter(), transcript)
        self.live.log_overlap(entry, None if verdict in (None, Verdict.BARGE_IN) else overlap.pcm)
        if verdict is not Verdict.BARGE_IN:
            self.post({"type": "unduck", "turn": overlap.turn, "verdict": str(verdict or "turn")})
        if verdict in (None, Verdict.BARGE_IN):
            self.start_turn(bytes(overlap.pcm), barge_in=verdict is Verdict.BARGE_IN)
            self.turn.overlap = entry
            if trailing_ms is not None:
                self.end_turn(trailing_ms, False)

    async def heard_before(self, turn, remaining_s, speaking):
        """What the person heard of `turn`'s replies before talking over them, or None if it never spoke.

        `heard` is the replies' text up to the last word a read-back of the played audio matched, the replies joined
        in the order they were spoken; all of it when nothing was left to play and the reply had ended (`whole`), as
        when the barge-in came in the grace window after the reply or was settled after it ended; "" when nothing
        matched; None when the read-back failed or took longer than READBACK_TIMEOUT_S. A reply still `speaking` was
        not heard whole even when its audio ran out: its text runs ahead of the audio released on it."""
        if turn is None or not turn.replies:
            return None
        sent = b"".join(reply.pcm for reply in turn.replies)
        sent_s = len(sent) / 2 / turn.out_rate
        whole = remaining_s <= 0 and not speaking
        heard = {"whole": whole, "played_s": round(max(0.0, sent_s - max(0.0, remaining_s)), 3),
                 "played_s_is": PLAYED_ESTIMATE}
        if whole:
            return {"heard": " ".join(reply.text for reply in turn.replies), **heard}
        pcm = played_part(sent, turn.out_rate, remaining_s)
        if not pcm:
            return {"heard": "", **heard}
        texts, start = [], 0
        for reply in turn.replies:
            if start >= len(pcm):
                break
            texts.append(reply.text)
            start += len(reply.pcm)
        try:
            transcript = await asyncio.wait_for(self.live.hear_reply(pcm, turn.out_rate), READBACK_TIMEOUT_S)
        except TimeoutError:
            return {"heard": None, "error": f"read-back timed out after {READBACK_TIMEOUT_S} s", **heard}
        except Exception as exc:
            return {"heard": None, "error": repr(exc), **heard}
        return {"heard": heard_prefix(" ".join(texts), transcript), **heard}

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
                self.start_turn(b"", barge_in=self.reply_audible())
            elif self.ptt_down and not down and self.turn is not None:
                self.end_turn(0, False)
            self.ptt_down = down

    def start_turn(self, pcm, barge_in=False):
        """A new turn starts. A barge-in stops the reply the client is playing. A turn that has spoken and is still
        running drops its later audio, such as a task update, rather than talk over the person; it keeps running,
        because closing it mid-response would cut the call's one session off.

        A barge-in turn records what the person heard of the reply it stopped; a turn that continues another takes
        that record, and any note already sent, along with the audio."""
        prior, carry, barge = self.turn, b"", None
        if barge_in:
            now = time.perf_counter()
            if prior is not None and prior.replies:
                barge = BargeIn(asyncio.create_task(self.heard_before(prior, self.playback.until - now,
                                                                      prior.speaking)))
                self.read_backs.add(barge.heard)
                barge.heard.add_done_callback(self.read_backs.discard)
            self.post({"type": "barge_in", "turn": self.playback.turn})
            self.playback.stop(now)
        if prior is not None:
            if prior.reply_started:
                if barge_in or not prior.task.done():
                    prior.mute("barge_in" if barge_in else "superseded")
            elif prior.idle:
                if not prior.task.done():
                    prior.mute("superseded")
            elif not prior.task.done():
                prior.cancel("continued")
                carry = bytes(prior.pcm)
                barge, prior.barge_in = barge or prior.barge_in, None
        self.live.turns += 1
        turn = MicTurn(self.live.turns, carry + pcm)
        turn.carried_s, turn.barge_in = len(carry) / 2 / RATE, barge
        turn.task = asyncio.create_task(self.run_turn(turn, prior.task if prior is not None else None))
        self.pending.add(turn)
        turn.task.add_done_callback(lambda _: self.pending.discard(turn))
        self.turn = turn
        self.post({"type": "turn_start", "turn": turn.n, "carried_audio_s": round(turn.carried_s, 3)})

    def check_end(self, silent_ms):
        """One ask of the audio model at a check point, unless the turn already ended or an answer is still coming."""
        turn = self.turn
        if self.live.eou is None or turn is None or turn.turn_end is not None:
            return
        if turn.check is not None and not turn.check.done():
            return
        turn.check = asyncio.create_task(self.ask_end(turn, silent_ms))
        self.checks.add(turn.check)
        turn.check.add_done_callback(self.checks.discard)

    async def ask_end(self, turn, silent_ms):
        """Ends the turn here when the model says the person has finished, and otherwise leaves the silence rule alone.

        The answer is bounded by `deadline_s`: past it the turn ends at `silence_ms` as it always did, so a slow or
        broken model costs no turn. An answer that arrives after the turn ended, or after the person spoke again,
        ends nothing: the answer is about the audio at the check point, and the point is stale by then.
        """
        policy = self.live.eou.policy
        deadline = policy.deadline_s(silent_ms, self.live.settings.silence_ms)
        tail = bytes(turn.pcm)[-int(policy.window_s * RATE) * 2:]
        started = time.perf_counter()
        try:
            answer = await asyncio.wait_for(asyncio.to_thread(self.live.eou.p_done, tail, deadline), deadline + 0.05)
        except Exception as exc:
            turn.checks.append({"silent_ms": silent_ms, "p": None, "error": type(exc).__name__,
                                "ms": round((time.perf_counter() - started) * 1000, 1), "ended": False})
            return
        ends = (answer.p_done >= policy.theta and turn is self.turn and turn.turn_end is None
                and self.endpointer.silent_ms >= silent_ms)
        turn.checks.append({"silent_ms": silent_ms, "p": round(answer.p_done, 4), "ms": answer.ms, "ended": ends})
        if ends:
            self.end_turn(silent_ms, False)

    def end_turn(self, trailing_ms, forced):
        self.heard_at = time.perf_counter()
        self.endpointer.finish(trailing_ms)
        self.turn.end(trailing_ms)
        self.post({"type": "turn_end", "turn": self.turn.n, "trailing_ms": trailing_ms, "forced": forced})

    def on_voice_event(self, event):
        """Every event on the call's voice session belongs to the turn that has the session right now.

        Reply audio is not forwarded here. The turn screens the reply's text as it arrives and hands `speak` only the
        audio whose text passed, so a reply that names a day, date, time, number or place the call never gave is never
        heard."""
        turn = self.voice_turn
        if turn is None:
            return
        if event.get("type") == "session.created":
            self.out_rate = turn.out_rate = event["session"]["audio"]["output"]["format"]["rate"]

    def speak(self, turn, text, pcm, done):
        """Send a reply to the client as its audio is released: `reply_start` before the first piece, each piece in
        40 ms frames, and `reply_end` once the reply is `done`. A muted turn sends no more audio but still ends the
        reply it started, so the client is not left waiting for the rest."""
        now = time.perf_counter()
        if pcm and not turn.muted:
            if not turn.speaking:
                turn.speaking, turn.reply_started = True, True
                turn.replies.append(Reply())
                self.post({"type": "reply_start", "turn": turn.n, "rate": turn.out_rate}, turn)
            reply = turn.replies[-1]
            new_text = text[len(reply.text):] if text.startswith(reply.text) else text
            reply.text = text
            reply.pcm += pcm
            self.playback.add(len(pcm) / 2 / turn.out_rate, new_text.strip(), now, turn.n)
            self.post_audio(turn, pcm, reply)
        if done and turn.speaking:
            turn.speaking = False
            self.post({"type": "reply_end", "turn": turn.n}, turn)

    async def speak_t3(self, turn, line, pcm):
        """Release a checked call reply and settle delivery from the frames the sender wrote."""
        text = speakable(line, "speak_t3").text
        if turn.muted or (self.sender is not None and self.sender.done()):
            return b"", None
        self.speak(turn, text, pcm, True)
        reply = turn.replies[-1]
        done = asyncio.get_running_loop().create_future()
        self.outbox.put_nowait((turn, AudioDelivery(done)))
        try:
            await asyncio.shield(done)
        except asyncio.CancelledError:
            # The sender may already have written frames. Settle using the actual byte count.
            await asyncio.shield(done)
        del reply.pcm[reply.sent_bytes:]
        return bytes(reply.pcm), None if reply.first_sent is None else reply.first_sent - self.started

    async def open_voice(self):
        """The call's one voice session, opened by its first turn. The persona is read here and kept for the call."""
        live = self.live
        noted = live.args.snapshot == "note"
        self.persona, self.prompt_source = load_persona(live.prompt_file, NOTE_PERSONA if noted else PERSONA)
        if not noted:
            self.persona += CALL_RULES
        if live.t3 is not None:
            self.persona += T3_RELAY
        voice = await Session.open(live.url, "voice", self.voice_clock, on_event=self.on_voice_event)
        try:
            created = await voice.wait_for("session.created")
            self.out_rate = created["session"]["audio"]["output"]["format"]["rate"]
            if live.t3 is not None:
                config = {"type": "realtime", "instructions": self.persona + FORMAT_LINES["c"],
                          "output_modalities": ["text", "audio"]}
                await update_session(voice, config)
                voice.config = config
        except BaseException:
            await voice.close()
            raise
        self.voice = voice

    async def run_turn(self, turn, prior):
        live = self.live
        turn.decider = TurnDecider(live.decider, live.decider_lock)
        partial = {}
        try:
            if prior is not None:
                await asyncio.gather(prior, return_exceptions=True)
            if self.voice is None:
                await self.open_voice()
            self.voice_turn, self.session_turns = turn, self.session_turns + 1
            turn.out_rate, self.started = self.out_rate, time.perf_counter()
            barge = turn.barge_in
            if barge is not None and live.barge_in_note and barge.note is None:
                # Only with the note on does a turn wait for its read-back, at most READBACK_TIMEOUT_S. asyncio.wait,
                # not await: cancelling a turn that is continued must not cancel the read-back it hands on.
                await asyncio.wait({barge.heard})
                barge.note = note_for(barge.result())
                if barge.note is not None:
                    barge.note_item = await create_item(self.voice, plain_text(barge.note))
            record = await run_clip(turn, "c", False, live.url, turn.decider, live.args.delay, live.out, partial,
                                    live.args.segmenter, live.tools, "gated", persona=self.persona,
                                    voice=self.voice, snapshot_mode=live.args.snapshot,
                                    prior_snapshot=self.ledger.snapshot,
                                    speak=lambda text, pcm, done: self.speak(turn, text, pcm, done),
                                    prior_utterances=tuple(turn_text["user"] for turn_text in self.ledger.conversation()
                                                           if "user" in turn_text),
                                    prior_tasks=tuple(self.ledger.tasks), asr_stagger=getattr(live.args, "asr_stagger", 1),
                                    simulate_tasks=getattr(live.args, "mock_tools", False), final_decider=True,
                                    recall=live.args.recall, context=self.ledger.context(),
                                    prior_results=tuple(self.ledger.results()),
                                    prior_replies=tuple(self.ledger.replies()),
                                    established=tuple(self.ledger.established()),
                                    task_service=live.task_service,
                                    send_checked=lambda line, pcm: self.speak_t3(turn, line, pcm))
        except asyncio.CancelledError:
            record = {"clip": turn.name, "cancelled": turn.cancelled, **partial}
        except Exception as exc:
            record = {"clip": turn.name, "error": repr(exc), **partial}
        finally:
            self.voice_turn = None
        turn.decider.closed = True
        self.ledger.record(record, muted=turn.muted)
        record["ledger"] = self.ledger.record_dict()
        record["live"] = {**turn.metrics(), "prompt": self.prompt_source, "endpointer": vars(live.settings),
                          "mode": self.mode, "snapshot": live.args.snapshot, "recall": live.args.recall,
                          "eou": getattr(live.args, "eou", "silence"),
                          "barge_in": None,
                          "call": {"turn": len(self.ledger.turns), "session_turn": self.session_turns}}
        write_pcm16(live.out / f"{turn.name}_input.wav", bytes(turn.pcm), RATE)
        self.write_record(turn, record)
        if turn.barge_in is not None and not turn.barge_in.heard.done():
            turn.barge_in.heard.add_done_callback(lambda _: self.write_record(turn, record))
        replies = [r for r in record.get("replies", []) if r.get("delivery", "sent") == "sent"]
        done = {"type": "turn_done", "turn": turn.n, "transcript": record.get("final_transcript"),
                "replies": [r.get("text") for r in replies], "error": record.get("error"),
                "cancelled": turn.cancelled,
                "rows": [r["row"] for r in replies if r.get("row")],
                "metrics": {**record.get("metrics", {}), **turn.metrics()}}
        self.post(done)
        print(f"{turn.name} {json.dumps(done['metrics'])}", flush=True)

    async def run_idle(self, turn):
        live, timeline = self.live, []
        updates = live.t3.reserve_news(idle=True)
        entries = [live.t3.news_entry(update, f"conv_{turn.stem}") for update in updates]
        unsent = {id(update): update for update in updates}
        of_entry = {id(entry): update for update, entry in zip(updates, entries)}
        words = Words(live.t3, lambda entry: of_entry[id(entry)])
        record = {"clip": turn.name, "idle": True, "final_transcript": None,
                  "t3_updates": [asdict(update) for update in updates], "task_updates": entries, "timeline": timeline,
                  "replies": []}

        def log(kind, **detail):
            timeline.append({"t": round(self.voice_clock(), 4), "kind": kind, **detail})

        try:
            if not turn.muted:
                if self.voice is None:
                    await self.open_voice()
                self.voice_turn, self.session_turns = turn, self.session_turns + 1
                turn.out_rate, self.started = self.out_rate, time.perf_counter()
                log("task_updates", updates=entries)
                for update, entry in zip(updates, entries):
                    def delivered(state):
                        if state == "sent":
                            live.t3.ack_news(unsent.pop(id(update)))

                    reply, _ = await say_acts(
                        self.voice, turn.out_rate, self.voice_clock(), entry, words, self.ledger.context(), log,
                        send_audio=lambda line, pcm: self.speak_t3(turn, line, pcm), record_delivery=delivered)
                    record["replies"].append({"kind": "t3_update", "row": update.row, **reply})
                    log("reply", reply="t3_update", text=reply["text"])
                    if turn.muted:
                        break
        except asyncio.CancelledError:
            turn.cancelled = turn.cancelled or "cancelled"
            record["cancelled"] = turn.cancelled
        except Exception as exc:
            record["error"] = repr(exc)
        finally:
            self.voice_turn = None
            if unsent:
                live.t3.retry_news(list(unsent.values()))
                record["given_back"] = True
        self.ledger.record(record, muted=turn.muted)
        record["live"] = {**turn.metrics(), "idle": True, "barge_in": None,
                          "call": {"turn": len(self.ledger.turns), "session_turn": self.session_turns}}
        self.write_record(turn, record)
        replies = [r for r in record["replies"] if r["delivery"] == "sent"]
        rows = [r["row"] for r in replies if r["row"]]
        self.post({"type": "turn_done", "turn": turn.n, "idle": True, "transcript": None,
                   "replies": [r["text"] for r in replies], "rows": rows, "error": record.get("error"),
                   "cancelled": turn.cancelled, "given_back": bool(record.get("given_back")),
                   "metrics": turn.metrics()})
        print(f"{turn.name} idle {json.dumps({'rows': rows, 'muted': turn.muted})}", flush=True)

    def write_record(self, turn, record):
        """The turn's record, written when the turn ends and again when a read-back still pending then lands."""
        if turn.barge_in is not None:
            record["live"]["barge_in"] = turn.barge_in.record()
        (self.live.out / f"{turn.name}.json").write_text(json.dumps(record, indent=1) + "\n", encoding="utf-8")

    async def close(self):
        if self.news is not None:
            self.news.cancel()
        if self.overlap is not None and self.overlap.task is not None:
            self.overlap.task.cancel()
        for task in (*self.checks, *self.read_backs):
            task.cancel()
        turns = list(self.pending)
        for turn in turns:
            turn.cancel("disconnected")
        if self.sender is not None:
            self.sender.cancel()
        await asyncio.gather(*(turn.task for turn in turns), return_exceptions=True)
        self.voice_turn = None
        if self.voice is not None:
            await self.voice.close()


class Live:
    def __init__(self, args, url, decider, tools, out, decider_lock=None, eou=None, *, secret):
        self.args, self.url, self.decider, self.tools, self.out = args, url, decider, tools, out
        self.task_service = self.t3 = None
        self.news = asyncio.Event()
        self.secret = secret
        self.eou = eou
        # With an audio decider the endpointer also names the points of silence at which the turn may end.
        check_ms = eou.policy.check_ms if eou is not None else ()
        self.settings = Settings(silence_ms=args.silence_ms, start_ms=args.start_ms, preroll_ms=args.preroll_ms,
                                 floor_dbfs=args.floor_dbfs, margin_db=args.margin_db, max_turn_s=args.max_turn_s,
                                 check_ms=check_ms)
        self.prompt_file, self.decider_lock = args.prompt, decider_lock or threading.Lock()
        self.active, self.turns, self.overlaps = None, 0, 0
        self.overlap_settings = OverlapSettings(confirm_ms=getattr(args, "confirm_ms", OverlapSettings.confirm_ms))
        self.barge_in_note = getattr(args, "barge_in_note", "off") == "on"

    async def transcribe(self, pcm):
        return await transcribe(self.url, pcm, Session)

    async def hear_reply(self, pcm, rate):
        """What a reply's played audio says, at the session's own rate: the read-back `heard_before` matches against the
        reply's text."""
        return await by_ear(self.url, rate)(pcm)

    def log_overlap(self, entry, pcm):
        """One line per overlap in overlaps.jsonl; a dismissed overlap's audio is kept beside it, since no turn holds
        it."""
        self.overlaps += 1
        entry["n"] = self.overlaps
        if pcm is not None:
            entry["audio"] = f"overlap_{self.overlaps:03d}.wav"
            write_pcm16(self.out / entry["audio"], bytes(pcm), RATE)
        with open(self.out / "overlaps.jsonl", "a", encoding="utf-8") as log:
            log.write(json.dumps(entry) + "\n")
        print(f"overlap {json.dumps(entry)}", flush=True)

    def header_authed(self, ws):
        """A Python client proves itself with the header before the handshake; the page proves itself later, with
        the auth frame `browser_auth` waits for."""
        header = ws.request.headers.get(HEADER, "") if ws.request is not None else ""
        return bool(header) and hmac.compare_digest(header.encode(), self.secret.encode())

    async def handler(self, ws):
        conversation = Conversation(self, ws, self.secret)
        if not self.header_authed(ws) and not await conversation.browser_auth():
            return
        if self.active is not None:
            await ws.close(1013, "another client is connected")
            return
        self.active = conversation
        print(f"client connected from {ws.remote_address[0]}", flush=True)
        try:
            await conversation.run()
        finally:
            self.active = None
            print("client disconnected", flush=True)


async def wait_for_stop(max_minutes, stop_file=STOP_FILE):
    deadline = time.monotonic() + max_minutes * 60
    while time.monotonic() < deadline:
        if stop_file.exists():
            print("STOP file found", flush=True)
            return
        await asyncio.sleep(1)
    print(f"stopping after --max-minutes {max_minutes}", flush=True)


async def watch_lag(limit_s=1.0):
    """Prints each stall of the event loop.

    The client's socket, the ASR's events and every reply share this loop, so a stall is the first thing to read for a
    turn that arrived late, a final transcript that timed out, or a client whose keepalive expired. The 2026-11-16
    17:38 replay's turn 38 held 11.9 s of the person's audio and never reached a transcript: the client closed the
    call on a keepalive ping timeout, and its records cannot say whether this loop was stalled."""
    woke = time.perf_counter()
    while True:
        await asyncio.sleep(0.2)
        now = time.perf_counter()
        if now - woke - 0.2 >= limit_s:
            print(f"event loop stalled for {now - woke - 0.2:.2f} s", flush=True)
        woke = now


async def main_async(args):
    host = check_bind(args.host or tailscale_ipv4())
    secret = shared_secret()
    args.stop_file.unlink(missing_ok=True)
    use_decider_cache(args.decider_cache, args.decider_budget)
    out = args.out or EVIDENCE / datetime.now().strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    url = f"ws://127.0.0.1:{args.lfm_port}/v1/realtime"
    # One KV pool for both sessions. Split evenly, the voice session got a third of -c while the ASR session used a
    # few hundred tokens of its share (measured: n_ctx_slot = 11008 in the recorded call's llama-server log).
    lfm_args = ("-c", str(args.lfm_ctx), "-kvu")
    cloud_lock = threading.Lock()
    tools_context = nullcontext() if args.mock_tools else open_registry(lock=cloud_lock)
    eou_mode, eou_port = getattr(args, "eou", "silence"), getattr(args, "eou_port", eou_audio.PORT)
    eou_context = eou_audio.server(eou_port, out / "eou-server.log") if eou_mode == "audio" else nullcontext()
    with tools_context as tools, Server(args.lfm_port, out / "llama-server.log", lfm_args, slots(getattr(args, "asr_stagger", 1))) as server, \
            make_decider(args.decider, out) as decider, eou_context as eou_server:
        eou = None
        if eou_server is not None:
            eou = eou_audio.Decider(f"http://127.0.0.1:{eou_port}",
                                    eou_audio.Policy(theta=getattr(args, "eou_theta", eou_audio.Policy.theta)))
            print(f"d1-Omni pid {eou_server.proc.pid} on port {eou_port} after {eou_server.startup_s:.1f} s: "
                  f"checks {eou.policy.check_ms}, theta {eou.policy.theta}", flush=True)
        t3 = poller = None
        loop, news = asyncio.get_running_loop(), asyncio.Event()
        if getattr(args, "t3", "off") == "on":
            t3 = T3Tasks.open(default_origin(), default_token_file(), default_journal(), default_model(url),
                              default_speech_decider())
            t3.resume(time.monotonic())
            poller = Poller(t3, notify=lambda: loop.call_soon_threadsafe(news.set)).start()
            tools = T3Registry(tools)
            print(f"t3 tasks on: {len(t3.follow.journal.live())} live threads and "
                  f"{len(t3.follow.journal.held())} held follow-ups resumed, thread picker model "
                  f"{'on' if t3.model else 'off'}", flush=True)
        live = Live(args, url, decider, tools, out, cloud_lock,
                    eou=eou, secret=secret)
        live.news = news
        if t3 is not None:
            live.t3, live.task_service = t3, partial(T3TaskService, t3=t3)
        watcher = asyncio.create_task(watch_lag())
        async with serve(live.handler, host, args.port, process_request=authorize(secret), max_size=2 ** 20):
            _, prompt_source = load_persona(args.prompt)
            print(f"llama-server pid {server.proc.pid} healthy after {server.startup_s:.1f} s", flush=True)
            print(f"live server on ws://{host}:{args.port}, prompt: {prompt_source}, records: {out}", flush=True)
            await wait_for_stop(args.max_minutes, args.stop_file)
            if live.active is not None:
                await live.active.ws.close(1001, "server stopping")
        watcher.cancel()
        if poller is not None:
            poller.stop()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", help="bind address; this machine's Tailscale IPv4 when left out")
    parser.add_argument("--port", type=int, default=8770)
    parser.add_argument("--prompt", type=Path, default=PROMPT_FILE, help="persona file, read at each call start")
    parser.add_argument("--silence-ms", type=int, default=700, help="trailing silence that ends a turn")
    parser.add_argument("--eou", choices=["silence", "audio"], default="silence",
                        help="what ends a turn: the trailing silence alone (default), or the local d1-Omni-2B asked "
                             "whether the person finished at 150, 300 and 500 ms of silence, falling back to "
                             "--silence-ms")
    parser.add_argument("--eou-theta", type=float, default=eou_audio.Policy.theta,
                        help=f"the P(done) that ends a turn early with --eou audio (default {eou_audio.Policy.theta})")
    parser.add_argument("--eou-port", type=int, default=eou_audio.PORT,
                        help=f"the audio model's llama-server port (default {eou_audio.PORT})")
    parser.add_argument("--start-ms", type=int, default=100, help="voiced audio that starts a turn")
    parser.add_argument("--preroll-ms", type=int, default=300, help="audio kept from before the turn started")
    parser.add_argument("--floor-dbfs", type=float, default=-50.0, help="a frame below this level is never speech")
    parser.add_argument("--margin-db", type=float, default=10.0, help="how far above the noise floor speech must be")
    parser.add_argument("--max-turn-s", type=float, default=30.0, help="end a turn that runs this long")
    parser.add_argument("--confirm-ms", type=int, default=OverlapSettings.confirm_ms,
                        help="voiced speech over a playing reply that stops it (default 600); shorter speech is "
                             "transcribed and a backchannel, noise or echo leaves the reply playing")
    parser.add_argument("--barge-in-note", choices=["on", "off"], default="off",
                        help="tell the voice session what the person heard of the reply a barge-in stops, before the "
                             "new turn's audio (default off: the A/B found no gain; the turn record keeps what was "
                             "heard either way)")
    parser.add_argument("--max-minutes", type=float, default=55.0, help="stop the server after this long")
    parser.add_argument("--stop-file", type=Path, default=STOP_FILE,
                        help="stop when this file appears (default $VOICE_HOME/live/STOP)")
    parser.add_argument("--segmenter", choices=sorted(SEGMENTERS), default="hybrid")
    parser.add_argument("--snapshot", choices=["note", "reply-no-utterance", "reply", "before-audio", "off"],
                        default="note",
                        help="what machine text enters the voice session: one short note after the audio on a turn "
                             "with a lookup (default), the task snapshot before the reply with the person's own "
                             "words dropped, the snapshot with everything, the snapshot before the turn's audio, or "
                             "nothing")
    parser.add_argument("--recall", choices=["off", "note"], default="note",
                        help="how a question about the call reaches the model: the numbered turns inside the "
                             "snapshot item (off), or those turns plus one plain sentence naming the turn the "
                             "question asks about, as the newest item before the reply (note, default)")
    parser.add_argument("--decider", choices=["liquid", "cloud", "d1"], default=default_decider())
    parser.add_argument("--delay", type=float, default=4.0, help="seconds a mock task takes (default 4)")
    parser.add_argument("--mock-tools", action="store_true", help="no real lookups: every task runs on the mock")
    parser.add_argument("--t3", choices=["off", "on"], default="off",
                        help="on: the decider may pick t3_start, t3_ask, t3_tell and t3_stop, which run on the fork's "
                             "T3 server through the voice seam (VOICE_T3_ORIGIN, VOICE_T3_TOKEN_FILE, "
                             "VOICE_T3_JOURNAL); off by default")
    parser.add_argument("--asr-stagger", type=int, default=1, choices=[1, 2, 3, 4, 6, 12],
                        help="transcription sessions fed the same audio, shifted by 0.96 s / N so the live "
                             "transcript updates N times per block (default 1: one update per 0.96 s)")
    parser.add_argument("--lfm-port", type=int, default=8095)
    parser.add_argument("--lfm-ctx", type=int, default=32768, help="LFM3's total context (default 32768)")
    parser.add_argument("--out", type=Path, help="records directory; a new one under $VOICE_HOME/evidence/live")
    parser.add_argument("--decider-cache", type=Path, default=EVIDENCE / "decider-cache")
    parser.add_argument("--decider-budget", type=int, default=200, help="stop when the ledger holds this many attempts")
    args = parser.parse_args()
    if args.segmenter != "hybrid":
        parser.error("the gated turn needs the hybrid segmenter")
    if args.t3 == "on" and args.mock_tools:
        parser.error("--t3 on needs the real registry: the decider's tool question offers the T3 tools")
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
