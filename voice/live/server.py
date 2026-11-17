"""Live mode: microphone audio streams in over the tailnet, the prototype's pipeline runs each turn, and the reply
audio streams back.

    python -m voice.live.server [--host IP] [--port 8770] [--prompt FILE] [--silence-ms 700] [--max-minutes 55]

One client at a time connects over WebSocket with the shared secret in the X-Voice-Secret header. The secret is made
once in $VOICE_HOME/live/secret, read in-process, and never printed. The server binds to this machine's Tailscale
IPv4 address (`tailscale ip -4`) and refuses any address outside 100.64.0.0/10 other than loopback.

Wire format. Binary frames are PCM16 mono: 16 kHz up from the client, LFM3's output rate down, 40 ms per frame
down. Text frames are JSON control messages:
  client -> server  hello {mode: vad|ptt}, ping {t}, ptt {down}
  server -> client  ready, pong {t}, turn_start, turn_end, reply_start {rate}, reply_end, duck, unduck, barge_in,
                    turn_done

Each turn runs `voice.prototype.run.run_clip` with the task gate and LFM3's end-of-utterance authority (injection (c),
the hybrid segmenter, the `gated` default from `voice/main`): live ASR, the segmenter and decider, the task service
with real weather and time lookups, the snapshot, and the reply. The endpointer in endpoint.py ends a turn after
--silence-ms of silence.

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

A reply is checked before it is heard. `voice/live/facts.py` reads the days, dates, times, numbers and places in the
reply and asks whether the person said them in this call or a lookup gave them; the assistant's own earlier replies
do not count. A reply that fails, that talks about the note, or that speaks of the person or of itself in the third
person, is not sent: the turn says so in its record, sends one short note naming what was wrong, and asks again once.
In note mode a second failure is silence, recorded as blocked. No fixed line is played, because a line rendered by
the TTS endpoint is a different voice from the call's (measured: turns 22 and 26 of the recorded call, after which
the person asked why it sounded different). When the registry has a general worker (`voice/tools/agent.py`,
VOICE_AGENT=on), a reply that states a fact nothing gave it starts a lookup for the person's words in place of
the retry. The older modes keep their fixed lines, `FALLBACK_LINE` and `ASK_INSTEAD_LINE`.

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
import secrets
import subprocess
import threading
import time
from contextlib import nullcontext
from datetime import datetime
from http import HTTPStatus
from pathlib import Path

from websockets.asyncio.server import serve

from voice.live import fallback
from voice.live.endpoint import RATE, Audio, End, Endpointer, Settings, Start
from voice.live.overlap import Overlap, OverlapSettings, Playback, Verdict, classify, transcribe
from voice.local_decider.client import decider as make_decider, default_decider
from voice.prototype.realtime import VOICE_HOME, Server, Session, write_pcm16
from voice.prototype.run import ASK_INSTEAD_LINE, FALLBACK_LINE, NOTE_PERSONA, PERSONA, run_clip
from voice.live.ledger import ConversationLedger
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


def authorize(secret):
    def check(connection, request):
        if request.headers.get("Origin"):
            return connection.respond(HTTPStatus.FORBIDDEN, "browser clients are refused\n")
        if not hmac.compare_digest(request.headers.get(HEADER, "").encode(), secret.encode()):
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


class MicTurn:
    """One turn's microphone audio, the source `run_clip` streams from, plus the turn's live timings."""

    rate = RATE
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
        self.out_rate, self.reply_started = None, False
        self.muted, self.cancelled, self.carried_s = False, None, 0.0
        self.task = self.decider = self.overlap = None
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
        }


class Conversation:
    """One connected client: its endpointer, its turns, and an outbox that keeps control and audio frames in order.

    The client connection is the call, so the conversation owns the call's one voice session and its transcript.
    `voice_turn` is the turn that currently has that session: the turn before it has finished, so its events and the
    new turn's cannot interleave."""

    def __init__(self, live, ws):
        self.live, self.ws = live, ws
        self.endpointer = Endpointer(live.settings)
        self.mode, self.ptt_down, self.turn = "vad", False, None
        self.outbox = asyncio.Queue()
        self.sender = asyncio.create_task(self._send_loop())
        self.voice, self.voice_turn, self.session_turns, self.out_rate = None, None, 0, None
        self.persona = self.prompt_source = None
        self.ledger = ConversationLedger()
        self.pending, self.started = set(), time.perf_counter()
        self.playback, self.overlap = Playback(), None

    def voice_clock(self):
        """The voice session's clock is the turn's clock: the session outlives the turn, but its events arrive while a
        turn is running, and run_clip stamps its own events from the same start."""
        return time.perf_counter() - self.started

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
                self.on_start(event.pcm)
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

    def on_start(self, pcm):
        """Speech while a reply is audible waits for a verdict, holding its audio; other speech starts a turn now. An
        overlap still being transcribed when speech resumes hands its audio to what comes next."""
        held = b""
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
        because closing it mid-response would cut the call's one session off."""
        prior, carry = self.turn, b""
        if barge_in:
            self.post({"type": "barge_in", "turn": self.playback.turn})
            self.playback.stop(time.perf_counter())
        if prior is not None:
            if prior.reply_started:
                if barge_in or not prior.task.done():
                    prior.mute("barge_in" if barge_in else "superseded")
            elif not prior.task.done():
                prior.cancel("continued")
                carry = bytes(prior.pcm)
        self.live.turns += 1
        turn = MicTurn(self.live.turns, carry + pcm)
        turn.carried_s = len(carry) / 2 / RATE
        turn.task = asyncio.create_task(self.run_turn(turn, prior.task if prior is not None else None))
        self.pending.add(turn)
        turn.task.add_done_callback(lambda _: self.pending.discard(turn))
        self.turn = turn
        self.post({"type": "turn_start", "turn": turn.n, "carried_audio_s": round(turn.carried_s, 3)})

    def end_turn(self, trailing_ms, forced):
        self.turn.end(trailing_ms)
        self.post({"type": "turn_end", "turn": self.turn.n, "trailing_ms": trailing_ms, "forced": forced})

    def on_voice_event(self, event):
        """Every event on the call's voice session belongs to the turn that has the session right now.

        Reply audio is not forwarded here. The turn collects it, checks the reply's facts, and hands what passed to
        `speak`, so a reply that names a day, date, time, number or place the call never gave is never heard."""
        turn = self.voice_turn
        if turn is None:
            return
        if event.get("type") == "session.created":
            self.out_rate = turn.out_rate = event["session"]["audio"]["output"]["format"]["rate"]

    def speak(self, turn, text, pcm):
        """Send one checked reply to the client: its start, its audio in 40 ms frames, and its end."""
        if turn.muted or not pcm:
            return
        turn.reply_started = True
        turn.first_audio = turn.first_audio or time.perf_counter()
        self.playback.add(len(pcm) / 2 / turn.out_rate, text, time.perf_counter(), turn.n)
        self.post({"type": "reply_start", "turn": turn.n, "rate": turn.out_rate}, turn)
        self.post_audio(turn, pcm)
        self.post({"type": "reply_end", "turn": turn.n}, turn)

    async def open_voice(self):
        """The call's one voice session, opened by its first turn. The persona is read here and kept for the call."""
        live = self.live
        noted = live.args.snapshot == "note"
        self.persona, self.prompt_source = load_persona(live.prompt_file, NOTE_PERSONA if noted else PERSONA)
        if not noted:
            self.persona += CALL_RULES
        self.voice = await Session.open(live.url, "voice", self.voice_clock, on_event=self.on_voice_event)
        created = await self.voice.wait_for("session.created")
        self.out_rate = created["session"]["audio"]["output"]["format"]["rate"]

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
            record = await run_clip(turn, "c", False, live.url, turn.decider, live.args.delay, live.out, partial,
                                    live.args.segmenter, live.tools, "gated", persona=self.persona,
                                    voice=self.voice, snapshot_mode=live.args.snapshot,
                                    prior_snapshot=self.ledger.snapshot,
                                    speak=lambda text, pcm: self.speak(turn, text, pcm),
                                    prior_utterances=tuple(turn_text["user"] for turn_text in self.ledger.conversation()
                                                           if "user" in turn_text),
                                    fallback_pcm=live.fallback.pcm(turn.out_rate), prior_tasks=tuple(self.ledger.tasks),
                                    ask_pcm=live.ask.pcm(turn.out_rate), asr_stagger=getattr(live.args, "asr_stagger", 1),
                                    simulate_tasks=getattr(live.args, "mock_tools", False), final_decider=True,
                                    recall=live.args.recall, context=self.ledger.context(),
                                    prior_results=tuple(self.ledger.results()))
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
                          "call": {"turn": len(self.ledger.turns), "session_turn": self.session_turns}}
        write_pcm16(live.out / f"{turn.name}_input.wav", bytes(turn.pcm), RATE)
        (live.out / f"{turn.name}.json").write_text(json.dumps(record, indent=1) + "\n", encoding="utf-8")
        done = {"type": "turn_done", "turn": turn.n, "transcript": record.get("final_transcript"),
                "replies": [r.get("text") for r in record.get("replies", [])], "error": record.get("error"),
                "cancelled": turn.cancelled,
                "metrics": {**record.get("metrics", {}), **turn.metrics()}}
        self.post(done)
        print(f"{turn.name} {json.dumps(done['metrics'])}", flush=True)

    async def close(self):
        if self.overlap is not None and self.overlap.task is not None:
            self.overlap.task.cancel()
        turns = list(self.pending)
        for turn in turns:
            turn.cancel("disconnected")
        await asyncio.gather(*(turn.task for turn in turns), return_exceptions=True)
        self.voice_turn = None
        if self.voice is not None:
            await self.voice.close()
        self.sender.cancel()


class Live:
    def __init__(self, args, url, decider, tools, out, decider_lock=None):
        self.args, self.url, self.decider, self.tools, self.out = args, url, decider, tools, out
        self.settings = Settings(silence_ms=args.silence_ms, start_ms=args.start_ms, preroll_ms=args.preroll_ms,
                                 floor_dbfs=args.floor_dbfs, margin_db=args.margin_db, max_turn_s=args.max_turn_s)
        self.prompt_file, self.decider_lock = args.prompt, decider_lock or threading.Lock()
        self.active, self.turns, self.overlaps = None, 0, 0
        self.overlap_settings = OverlapSettings(confirm_ms=getattr(args, "confirm_ms", OverlapSettings.confirm_ms))
        self.fallback, self.ask = fallback.Clip(FALLBACK_LINE), fallback.Clip(ASK_INSTEAD_LINE)

    async def transcribe(self, pcm):
        return await transcribe(self.url, pcm, Session)

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

    def render_lines(self):
        """Render the fixed lines in the server's own TTS voice before any client connects, so no turn pays for it."""
        for clip in (self.fallback, self.ask):
            clip.render(f"http://127.0.0.1:{self.args.lfm_port}")

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


async def wait_for_stop(max_minutes, stop_file=STOP_FILE):
    deadline = time.monotonic() + max_minutes * 60
    while time.monotonic() < deadline:
        if stop_file.exists():
            print("STOP file found", flush=True)
            return
        await asyncio.sleep(1)
    print(f"stopping after --max-minutes {max_minutes}", flush=True)


async def main_async(args):
    host = check_bind(args.host or tailscale_ipv4())
    secret = shared_secret()
    args.stop_file.unlink(missing_ok=True)
    use_decider_cache(args.decider_cache, args.decider_budget)
    out = args.out or EVIDENCE / datetime.now().strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    # One KV pool for both sessions. Split evenly, the voice session got a third of -c while the ASR session used a
    # few hundred tokens of its share (measured: n_ctx_slot = 11008 in the recorded call's llama-server log).
    lfm_args = ("-c", str(args.lfm_ctx), "-kvu")
    cloud_lock = threading.Lock()
    tools_context = nullcontext() if args.mock_tools else open_registry(lock=cloud_lock)
    with tools_context as tools, Server(args.lfm_port, out / "llama-server.log", lfm_args, 1 + args.asr_stagger) as server, \
            make_decider(args.decider, out) as decider:
        live = Live(args, f"ws://127.0.0.1:{args.lfm_port}/v1/realtime", decider, tools, out, cloud_lock)
        async with serve(live.handler, host, args.port, process_request=authorize(secret), max_size=2 ** 20):
            _, prompt_source = load_persona(args.prompt)
            print(f"llama-server pid {server.proc.pid} healthy after {server.startup_s:.1f} s", flush=True)
            if args.snapshot != "note":
                live.render_lines()
            print(f"live server on ws://{host}:{args.port}, prompt: {prompt_source}, records: {out}", flush=True)
            await wait_for_stop(args.max_minutes, args.stop_file)
            if live.active is not None:
                await live.active.ws.close(1001, "server stopping")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", help="bind address; this machine's Tailscale IPv4 when left out")
    parser.add_argument("--port", type=int, default=8770)
    parser.add_argument("--prompt", type=Path, default=PROMPT_FILE, help="persona file, read at each call start")
    parser.add_argument("--silence-ms", type=int, default=700, help="trailing silence that ends a turn")
    parser.add_argument("--start-ms", type=int, default=100, help="voiced audio that starts a turn")
    parser.add_argument("--preroll-ms", type=int, default=300, help="audio kept from before the turn started")
    parser.add_argument("--floor-dbfs", type=float, default=-50.0, help="a frame below this level is never speech")
    parser.add_argument("--margin-db", type=float, default=10.0, help="how far above the noise floor speech must be")
    parser.add_argument("--max-turn-s", type=float, default=30.0, help="end a turn that runs this long")
    parser.add_argument("--confirm-ms", type=int, default=OverlapSettings.confirm_ms,
                        help="voiced speech over a playing reply that stops it (default 600); shorter speech is "
                             "transcribed and a backchannel, noise or echo leaves the reply playing")
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
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
