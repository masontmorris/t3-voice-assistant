"""Thin client for the live voice demo: microphone up to the Desktop, reply audio back to the speakers.

    python client.py [--server ws://HOST:8770] [--input DEVICE] [--output DEVICE] [--ptt] [--wav CLIP.wav ...]
    python client.py --list-devices

Use speakers or headphones. The client cancels speaker echo from the microphone before it sends it, so the reply
audio the speakers play is not heard as speech and does not interrupt the reply. A person talking over the reply
still interrupts it. `--no-echo-cancel` sends the raw microphone, which works with headphones and was the earlier
behavior. The echo canceller models a 256 ms echo path by default; `--echo-tail-ms` raises it for a long delay.

The server decides when a turn ends (about 0.7 s of silence). With --ptt, press Enter to start talking and Enter
again to stop. Talking while a reply plays interrupts it: the server cancels the reply and this client drops the audio
it buffered. --wav feeds clips in place of the microphone, which is how the Desktop loopback test runs, and echo
cancellation is off for it unless `--echo-cancel` is given.

The server address and the shared secret come from config.json and the `secret` file beside this script unless
--server and --secret-file say otherwise. Each run records what this client sent and played, plus the metrics, the
reply audio and the control log, under a new timestamped directory in --log-dir ($VOICE_HOME/live-client by
default). `--no-record` turns the recording off. Stops on Ctrl+C, or after the last --wav clip's turn.
"""

import argparse
import asyncio
import json
import os
import statistics
import sys
import threading
import time
import wave
from pathlib import Path

import numpy as np
import websockets

if __package__:
    from voice.live.aec import EchoCanceller
    from voice.live.comms import CAPTURE, RENDER, default_name
    from voice.live.rawmic import open_raw
    from voice.live.recording import SessionRecorder, unique_session_dir
else:
    from aec import EchoCanceller
    from comms import CAPTURE, RENDER, default_name
    from rawmic import open_raw
    from recording import SessionRecorder, unique_session_dir

HERE = Path(__file__).resolve().parent
HEADER = "X-Voice-Secret"
RATE = 16_000
FRAME_MS = 20
FRAME_SAMPLES = RATE * FRAME_MS // 1000
LOG_DIR = Path(os.environ.get("VOICE_HOME", Path.home() / "voice")) / "live-client"


def read_wav_16k(path):
    import soundfile as sf
    samples, rate = sf.read(str(path), dtype="float32", always_2d=True)
    mono = samples.mean(axis=1)
    if rate != RATE:
        positions = np.arange(0, len(mono), rate / RATE)
        mono = np.interp(positions, np.arange(len(mono)), mono)
    return (np.clip(mono, -1, 1) * 32767).astype("<i2").tobytes()


def device(value):
    return int(value) if value is not None and value.isdigit() else value


def mme_output(sd, name):
    """The MME output device whose name begins Windows' `name` (MME cuts names at 31 characters), or None. MME
    converts the reply's rate to the device's, which a WASAPI shared stream would not."""
    apis = sd.query_hostapis()
    mme = next((i for i, api in enumerate(apis) if api["name"] == "MME"), None)
    matches = [i for i, d in enumerate(sd.query_devices())
               if d["hostapi"] == mme and d["max_output_channels"] > 0 and name.startswith(d["name"])]
    return max(matches, key=lambda i: len(sd.query_devices(i)["name"])) if matches else None


def auto_output():
    """Uses Windows' default communications output, the speakers or headset chosen for calls, and otherwise the
    current default output. Plugging in headphones normally makes them the default, so the reply follows them.
    Returns (device, description); device None means the default."""
    import sounddevice as sd
    try:
        name = default_name(RENDER)
        index = mme_output(sd, name) if name else None
        if index is not None:
            return index, f"Windows communications output: {name}"
    except Exception:
        pass
    try:
        apis = sd.query_hostapis()
        wasapi = next((a for a in apis if "WASAPI" in a["name"]), None)
        index = wasapi["default_output_device"] if wasapi else sd.default.device[1]
        name = sd.query_devices(index)["name"]
    except Exception:
        name = "unknown"
    return None, f"Windows default output: {name}"


def echo_canceller(args):
    """This run's microphone echo canceller, or None when the run does not use one.

    Off unless --echo-cancel is given: on real laptop echo it garbles the person's speech (issue #33), so use
    headphones until that is fixed."""
    if getattr(args, "echo_cancel", None) is None:
        enabled = False
    else:
        enabled = args.echo_cancel
    return EchoCanceller(tail_ms=getattr(args, "echo_tail_ms", 256)) if enabled else None


class Player:
    """Plays reply PCM16 through one output stream, after `prebuffer_ms` is queued or the reply ends."""

    def __init__(self, output, prebuffer_ms, recorder=None, canceller=None):
        self.output, self.prebuffer_ms = output, prebuffer_ms
        self.recorder, self.canceller = recorder, canceller
        self.stream, self.rate = None, None
        self.buffer, self.lock = bytearray(), threading.Lock()
        self.playing, self.reply_done = False, True
        self.first_play_t, self.underruns = None, 0

    def open(self, rate):
        """Opening a stream takes a while on Windows, so open it before the first reply and again only if the rate
        changes."""
        if self.rate == rate:
            return
        self.close()
        import sounddevice as sd
        self.rate = rate
        self.stream = sd.RawOutputStream(samplerate=rate, channels=1, dtype="int16", device=self.output,
                                         blocksize=rate * FRAME_MS // 1000, latency="low", callback=self._callback)
        self.stream.start()

    def start_reply(self, rate):
        self.open(rate)
        with self.lock:
            self.reply_done, self.first_play_t = False, None

    def feed(self, pcm):
        with self.lock:
            self.buffer += pcm
            if not self.playing and len(self.buffer) >= self.rate * self.prebuffer_ms // 1000 * 2:
                self.playing = True

    def end_reply(self):
        with self.lock:
            self.reply_done, self.playing = True, bool(self.buffer)

    def flush(self):
        with self.lock:
            self.buffer.clear()
            self.playing = False

    def _callback(self, outdata, frames, _time, _status):
        need = frames * 2
        now = time.perf_counter()
        with self.lock:
            if not self.playing:
                outdata[:] = b"\x00" * need
                return
            if self.first_play_t is None:
                self.first_play_t = time.perf_counter()
            chunk = bytes(self.buffer[:need])
            del self.buffer[:need]
            if len(chunk) < need and not self.reply_done:
                self.underruns += 1
            if not self.buffer:
                self.playing = False
            rate = self.rate
        outdata[:] = chunk + b"\x00" * (need - len(chunk))
        if chunk and self.canceller is not None:
            try:
                self.canceller.playback(chunk, rate, now)
            except Exception:
                self.canceller = None
        if chunk and self.recorder is not None:
            self.recorder.playback(chunk, rate, now)

    def close(self):
        if self.stream is not None:
            self.stream.stop()
            self.stream.close()
            self.stream = None


async def mic_frames(input_device, queue, raw=True):
    import sounddevice as sd
    loop = asyncio.get_running_loop()

    def on_frame(pcm):
        loop.call_soon_threadsafe(queue.put_nowait, (pcm, time.perf_counter()))

    communications = default_name(CAPTURE) if input_device is None else None
    stream, how = (open_raw(sd, input_device, RATE, FRAME_SAMPLES, on_frame, communications) if raw
                   else (None, "--no-raw-mic"))
    if stream is None:
        print(f"input: driver-processed capture ({how})", flush=True)
        stream = sd.RawInputStream(samplerate=RATE, channels=1, dtype="int16", blocksize=FRAME_SAMPLES,
                                   device=input_device, callback=lambda data, *_: on_frame(bytes(data)))
    else:
        print(f"input: {how}", flush=True)
    with stream:
        await asyncio.Event().wait()


async def wav_frames(paths, queue, next_clip, lead_s=0.5):
    """Clips in place of the microphone, at real-time pace with silence between. Each clip after the first waits for
    `next_clip`."""
    step, start, index = FRAME_SAMPLES * 2, time.perf_counter(), 0
    pending, clips = bytearray(b"\x00" * int(lead_s * RATE) * 2 + read_wav_16k(paths[0])), list(paths[1:])
    while True:
        if not pending and clips and next_clip.is_set():
            next_clip.clear()
            pending += read_wav_16k(clips.pop(0))
        frame = bytes(pending[:step])
        del pending[:step]
        queue.put_nowait((frame + b"\x00" * (step - len(frame)), time.perf_counter()))
        index += 1
        await asyncio.sleep(max(0.0, start + index * FRAME_MS / 1000 - time.perf_counter()))


class Client:
    def __init__(self, args):
        self.args = args
        self.log_dir = unique_session_dir(args.log_dir)
        self.recorder = None
        if getattr(args, "record", True):
            self.recorder = SessionRecorder(self.log_dir, mode="ptt" if args.ptt else "vad",
                                            input_source="wav" if args.wav else "mic", server=args.server)
        output = device(args.output)
        if output is None:
            output, chosen = auto_output()
            print(f"output: {chosen}", flush=True)
        self.echo = echo_canceller(args)
        if self.echo is not None:
            print(f"echo cancellation on: {self.echo.tail_ms} ms echo path", flush=True)
        self.player = Player(output, args.prebuffer_ms, recorder=self.recorder, canceller=self.echo)
        self.rtts, self.done_turns, self.latest_turn = [], 0, None
        self.clips_left = max(0, len(args.wav or []) - 1)
        self.next_clip = asyncio.Event()
        self.turn_end_rx, self.trailing_s, self.reply_first = None, 0.0, None
        self.reply_audio, self.reply_turn, self.reply_t0 = bytearray(), None, None
        self.log_file = open(self.log_dir / "client.jsonl", "a", encoding="utf-8")
        self.finished = asyncio.Event()

    def log(self, kind, **detail):
        self.log_file.write(json.dumps({"t": round(time.time(), 3), "kind": kind, **detail}) + "\n")
        self.log_file.flush()

    def echo_stats(self):
        return None if self.echo is None else self.echo.stats()

    async def send_audio(self, ws, frames):
        while True:
            pcm, t = await frames.get()
            echo = getattr(self, "echo", None)
            if echo is not None:
                try:
                    pcm = echo.process(pcm, t)
                except Exception as exc:
                    print(f"echo cancellation failed, sending the raw microphone: {exc}", file=sys.stderr,
                          flush=True)
                    self.echo = None
                    if hasattr(self, "log"):
                        self.log("echo_failed", error=repr(exc))
            await ws.send(pcm)
            if self.recorder is not None:
                self.recorder.mic(pcm, t)

    async def ping(self, ws):
        while True:
            await ws.send(json.dumps({"type": "ping", "t": time.perf_counter()}))
            await asyncio.sleep(2)

    def push_to_talk(self, ws, loop):
        down = False
        print("Push-to-talk: press Enter to talk, Enter again to stop.", flush=True)
        for _ in sys.stdin:
            down = not down
            print("talking..." if down else "sent.", flush=True)
            asyncio.run_coroutine_threadsafe(ws.send(json.dumps({"type": "ptt", "down": down})), loop)

    def save_reply(self):
        if self.reply_turn is None or not self.reply_audio:
            return
        path = self.log_dir / f"turn_{self.reply_turn:03d}_reply_{int(self.reply_t0)}.wav"
        with wave.open(str(path), "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(self.player.rate)
            out.writeframes(bytes(self.reply_audio))
        self.reply_audio.clear()

    def on_control(self, message):
        kind = message.get("type")
        now = time.perf_counter()
        if kind == "pong":
            self.rtts.append(now - message["t"])
        elif kind == "ready":
            print(f"connected, mode {message['mode']}, turn ends after {message['silence_ms']} ms of silence",
                  flush=True)
        elif kind == "turn_start":
            self.latest_turn = message["turn"]
            print(f"[turn {message['turn']}] listening", flush=True)
        elif kind == "turn_end":
            print(f"[turn {message['turn']}] thinking", flush=True)
            self.turn_end_rx, self.trailing_s = now, message["trailing_ms"] / 1000
            self.log("turn_end", **message)
        elif kind == "reply_start":
            self.player.start_reply(message["rate"])
            self.reply_turn, self.reply_t0, self.reply_first = message["turn"], time.time(), now
            e2e = None
            if self.turn_end_rx is not None and self.rtts:
                e2e = round(now - self.turn_end_rx + self.trailing_s + self.rtt_median() / 2, 4)
            self.turn_end_rx = None
            self.log("reply_start", turn=message["turn"], rate=message["rate"], speech_end_to_first_audio_rx_s=e2e,
                     output_latency_s=round(self.player.stream.latency, 4))
            if self.args.barge_in_ms is not None and self.clips_left:
                asyncio.get_running_loop().call_later(self.args.barge_in_ms / 1000, self.send_next_clip)
        elif kind == "reply_end":
            self.player.end_reply()
            self.save_reply()
            play_delay = None
            if self.player.first_play_t is not None:
                play_delay = round(self.player.first_play_t - self.reply_first, 4)
            self.log("reply_end", turn=message["turn"], first_audio_to_playback_s=play_delay,
                     underruns=self.player.underruns, echo=self.echo_stats())
        elif kind == "barge_in":
            self.player.flush()
            self.reply_audio.clear()
            print(f"[turn {message['turn']}] interrupted", flush=True)
            self.log("barge_in", **message)
        elif kind == "turn_done":
            self.done_turns += 1
            print(f"[turn {message['turn']}] heard: {message.get('transcript')!r}", flush=True)
            for text in message.get("replies") or []:
                print(f"[turn {message['turn']}] said: {text!r}", flush=True)
            self.log("turn_done", **message, rtt_median_s=self.rtt_median())
            settled = message["turn"] == self.latest_turn and not message["metrics"].get("cancelled")
            if self.args.wav and settled:
                if self.clips_left:
                    self.send_next_clip()
                else:
                    self.finished.set()

    def send_next_clip(self):
        if self.clips_left:
            self.clips_left -= 1
            self.next_clip.set()

    def rtt_median(self):
        return round(statistics.median(self.rtts), 4) if self.rtts else None

    async def receive(self, ws):
        async for message in ws:
            if isinstance(message, bytes):
                self.player.feed(message)
                self.reply_audio += message
            else:
                self.on_control(json.loads(message))

    async def connect(self, secret):
        deadline = time.monotonic() + self.args.wait_for_server
        while True:
            try:
                return await websockets.connect(self.args.server, additional_headers={HEADER: secret}, max_size=None)
            except OSError:
                if time.monotonic() > deadline:
                    raise
                await asyncio.sleep(2)

    def finalize_recording(self):
        if self.recorder is None:
            return
        try:
            self.recorder.finalize()
        except Exception as exc:
            print(f"recording was not finalized: {exc}", file=sys.stderr, flush=True)

    async def run(self):
        try:
            if self.recorder is not None:
                print(f"recording this session to {self.log_dir}", flush=True)
            secret = self.args.secret_file.read_text(encoding="utf-8").strip()
            frames = asyncio.Queue()
            self.player.open(self.args.output_rate)
            ws = await self.connect(secret)
            if self.args.wav:
                source = wav_frames(self.args.wav, frames, self.next_clip)
            else:
                source = mic_frames(device(self.args.input), frames, raw=getattr(self.args, "raw_mic", True))
            async with ws:
                await ws.send(json.dumps({"type": "hello", "mode": "ptt" if self.args.ptt else "vad"}))
                tasks = [asyncio.create_task(c) for c in (source, self.send_audio(ws, frames), self.ping(ws),
                                                          self.receive(ws), self.finished.wait())]
                if self.args.ptt:
                    threading.Thread(target=self.push_to_talk, args=(ws, asyncio.get_running_loop()),
                                     daemon=True).start()
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                for task in done:
                    if task.exception() is not None and not isinstance(task.exception(),
                                                                       websockets.ConnectionClosed):
                        raise task.exception()
            self.log("summary", rtt_median_s=self.rtt_median(), rtt_samples=len(self.rtts),
                     rtt_max_s=round(max(self.rtts), 4) if self.rtts else None, turns=self.done_turns,
                     echo=self.echo_stats())
            print(f"round trip over the socket: median {self.rtt_median()} s over {len(self.rtts)} pings",
                  flush=True)
        finally:
            try:
                self.player.close()
            except Exception as exc:
                print(f"output stream was not closed cleanly: {exc}", file=sys.stderr, flush=True)
            self.finalize_recording()
            try:
                self.log_file.close()
            except Exception as exc:
                print(f"client log was not closed cleanly: {exc}", file=sys.stderr, flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    config_path = HERE / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.is_file() else {}
    parser.add_argument("--server", default=config.get("server"), help="ws://HOST:PORT; config.json gives it")
    parser.add_argument("--secret-file", type=Path, default=HERE / "secret")
    parser.add_argument("--input", help="microphone: device index or part of its name (see --list-devices)")
    parser.add_argument("--output", help="speakers or headphones: device index or part of its name (default: the Windows default output)")
    parser.add_argument("--list-devices", action="store_true")
    parser.add_argument("--ptt", action="store_true", help="push-to-talk with Enter instead of silence detection")
    parser.add_argument("--wav", type=Path, nargs="+",
                        help="send these clips in place of the microphone, each after the previous turn is done, "
                             "and exit after the last")
    parser.add_argument("--barge-in-ms", type=int,
                        help="with --wav, send the next clip this long after a reply starts, to test barge-in")
    parser.add_argument("--prebuffer-ms", type=int, default=80, help="reply audio to queue before playback starts")
    parser.add_argument("--echo-cancel", dest="echo_cancel", action="store_true", default=None,
                        help="cancel speaker echo from the microphone (default: off; it garbles speech on real echo, issue #33)")
    parser.add_argument("--no-echo-cancel", dest="echo_cancel", action="store_false",
                        help="send the raw microphone, as with headphones")
    parser.add_argument("--no-raw-mic", dest="raw_mic", action="store_false",
                        help="take the microphone after the driver's own effects (its echo cancellation and noise "
                             "suppression) instead of the raw WASAPI stream")
    parser.add_argument("--echo-tail-ms", type=int, default=256,
                        help="echo path the canceller models, from output callback to microphone (default 256); "
                             "raise it for a Bluetooth speaker")
    parser.add_argument("--output-rate", type=int, default=24_000, help="rate to open the output at before a reply")
    parser.add_argument("--wait-for-server", type=float, default=0, help="keep retrying the connection this many s")
    parser.add_argument("--log-dir", type=Path, default=LOG_DIR)
    parser.add_argument("--no-record", dest="record", action="store_false",
                        help="do not record this session's microphone, playback and conversation")
    args = parser.parse_args()
    if args.list_devices:
        import sounddevice as sd
        print(sd.query_devices())
        return
    if not args.server:
        parser.error("no server: put config.json beside client.py or pass --server ws://HOST:8770")
    if args.echo_tail_ms <= 0:
        parser.error("--echo-tail-ms must be positive")
    try:
        asyncio.run(Client(args).run())
    except KeyboardInterrupt:
        pass
    except websockets.InvalidStatus as exc:
        sys.exit(f"the server refused the connection: HTTP {exc.response.status_code}")
    except OSError as exc:
        sys.exit(f"could not reach {args.server}: {exc}")


if __name__ == "__main__":
    main()
