import asyncio
import base64
import contextlib
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import websockets
from websockets.asyncio.server import serve

from voice.live import server
from voice.live.tests.test_endpoint import noise, tone
from voice.prototype.run import run_clip

SECRET = "s" * 64
OUT_RATE = 24_000
PERSONAS = []
CALLS = []
VOICES = []
SENT_BEFORE = []


class FakeSession:
    def __init__(self):
        self.audio_bytes = 0

    def clock(self):
        return 0.0

    async def send(self, **event):
        self.audio_bytes += len(event.get("audio", ""))


class FakeVoice:
    """The call's voice session, standing in for llama-server's realtime socket."""

    def __init__(self, name, on_event):
        self.name, self.on_event, self.config = name, on_event, None
        self.events, self.sent, self.audio = [], [], []
        self.closed = False

    @classmethod
    async def open(cls, url, name, clock, on_event=None):
        return cls(name, on_event)

    async def send(self, **event):
        self.sent.append(event)

    async def wait_for(self, *types, after=0, timeout=120):
        if "session.created" in types:
            return {"type": "session.created",
                    "session": {"audio": {"output": {"format": {"rate": OUT_RATE}}}}}
        return {"type": "conversation.item.added", "item": {}}

    async def close(self):
        self.closed = True


async def fake_run_clip(source, *args, persona, voice=None, speak=None, prior_utterances=(), **kwargs):
    """Streams the turn's input, then hands the turn one checked 2 s reply through the live server's own transport,
    the way `run_clip` does after the fact check."""
    PERSONAS.append(persona)
    CALLS.append((*args, kwargs))
    VOICES.append(voice)
    SENT_BEFORE.append(list(voice.sent))
    voice.on_event({"type": "session.created", "session": {"audio": {"output": {"format": {"rate": OUT_RATE}}}}})
    await source.stream(FakeSession(), FakeSession())
    await asyncio.sleep(0.3)
    speak(f"reply to {source.name}", b"\x01\x00" * (OUT_RATE * 2), True)
    return {"clip": source.name, "final_transcript": f"words in {source.name}",
            "replies": [{"text": f"reply to {source.name}"}], "metrics": {},
            "snapshot": {"final_transcript": f"state after {source.name}"}}


async def streaming_run_clip(source, *args, persona, voice=None, speak=None, **kwargs):
    """A turn whose 2 s reply is released in eight quarter-second pieces 0.2 s apart, as the release gate hands
    them over while the model generates."""
    VOICES.append(voice)
    voice.on_event({"type": "session.created", "session": {"audio": {"output": {"format": {"rate": OUT_RATE}}}}})
    await source.stream(FakeSession(), FakeSession())
    for _ in range(8):
        speak(f"reply to {source.name}", b"\x01\x00" * (OUT_RATE // 4), False)
        await asyncio.sleep(0.2)
    speak(f"reply to {source.name}", b"", True)
    return {"clip": source.name, "final_transcript": f"words in {source.name}",
            "replies": [{"text": f"reply to {source.name}"}], "metrics": {}}


class FakeDecider:
    """The shared decider, answering one utterance's decisions request from a canned answer set."""

    def __init__(self, answers=None, error=None, delay_s=0.0):
        self.answers, self.error, self.delay_s, self.calls = answers, error, delay_s, []

    def call(self, route, body, label="", fresh=False):
        self.calls.append({"route": route, "label": label, "fresh": fresh})
        if self.delay_s:
            time.sleep(self.delay_s)
        if self.error is not None:
            raise self.error
        return {"route": route, "label": label, "status": 200, "model": "test:free",
                "response": {"answers": self.answers or {
                    "s1_kind": {"type": "choice", "choice": "request", "confidence": 0.8},
                    "s1_ready": {"type": "choice", "choice": "dispatch_now", "confidence": 0.9},
                    "s1_side_effect": {"type": "noul", "noul": 0.0, "confidence": 0.0}}},
                "timing": {"ms": 1.0, "started_at": "x"}}


class SlowShadow(FakeDecider):
    """The shared decider, whose shadow request stays in flight until it is aborted, as on a slow backend. Its lane is
    itself, so a shadow that held the turns' lock while asking would hold it for as long as the request lasts."""

    def __init__(self):
        super().__init__()
        self.entered, self.aborted = threading.Event(), threading.Event()

    def lane(self, timeout_s):
        return self

    def abort(self):
        self.aborted.set()

    def call(self, route, body, label="", fresh=False):
        if label.startswith("shadow:"):
            self.entered.set()
            if not self.aborted.wait(5):
                raise AssertionError("the shadow's request was never ended")
            raise ConnectionAbortedError("the client was aborted")
        return super().call(route, body, label, fresh)


async def shadow_run_clip(source, *args, persona, voice=None, speak=None, shadow=False, **kwargs):
    """A turn with a fifth of a second of reply audio, and the shadow's request when the server asked for one."""
    voice.on_event({"type": "session.created", "session": {"audio": {"output": {"format": {"rate": OUT_RATE}}}}})
    await source.stream(FakeSession(), FakeSession())
    speak(f"reply to {source.name}", b"\x01\x00" * (OUT_RATE // 5 * 2), True)
    record = {"clip": source.name, "final_transcript": f"words in {source.name}",
              "replies": [{"text": f"reply to {source.name}"}], "metrics": {}, "tasks": []}
    if shadow:
        record["shadow_request"] = server.shadow_log.request(
            utterance=source.name, turn_record=f"{source.name}.json", authority="decider",
            body={"model": "test:free",
                  "state": {"transcript": record["final_transcript"], "transcript_is_final": True,
                            "segments": {"s1": record["final_transcript"]}, "still_being_spoken": None,
                            "tasks": [], "delegate_requests": {}},
                  "questions": {"s1_kind": {}}},
            lfm=[], outcomes=[], intent="request")
    return record


class LiveServerTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.frames = []
        PERSONAS.clear()
        CALLS.clear()
        VOICES.clear()
        SENT_BEFORE.clear()
        self.tmp = tempfile.TemporaryDirectory()
        args = SimpleNamespace(silence_ms=700, start_ms=100, preroll_ms=300, floor_dbfs=-50.0, margin_db=10.0,
                               max_turn_s=30.0, prompt=Path(self.tmp.name) / "prompt.txt", delay=4.0,
                               segmenter="hybrid", snapshot="reply", shadow="off",
                               recall="note")
        self.live = server.Live(args, "ws://unused", decider=None, tools=None, out=Path(self.tmp.name))
        self.patch = mock.patch.object(server, "run_clip", fake_run_clip)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        session_patch = mock.patch.object(server.Session, "open", FakeVoice.open)
        session_patch.start()
        self.addCleanup(session_patch.stop)
        self.heard("And one more thing.")
        self.ws_server = await serve(self.live.handler, "127.0.0.1", 0, process_request=server.authorize(SECRET))
        self.url = f"ws://127.0.0.1:{self.ws_server.sockets[0].getsockname()[1]}"

    async def asyncTearDown(self):
        self.ws_server.close()
        await self.ws_server.wait_closed()
        self.tmp.cleanup()

    async def connect(self, headers=None):
        ws = await websockets.connect(self.url, additional_headers=headers or {server.HEADER: SECRET})
        await ws.send(json.dumps({"type": "hello", "mode": "vad"}))
        self.assertEqual(json.loads(await ws.recv())["type"], "ready")
        return ws

    async def send(self, ws, pcm):
        for offset in range(0, len(pcm), 640):
            await ws.send(pcm[offset:offset + 640])

    async def until(self, ws, kind, log):
        while True:
            message = await asyncio.wait_for(ws.recv(), 10)
            log.append("audio" if isinstance(message, bytes) else json.loads(message)["type"])
            if isinstance(message, bytes):
                self.frames.append(len(message))
            if log[-1] == kind:
                return json.loads(message)

    async def test_a_missing_or_wrong_secret_is_refused(self):
        for headers in ({}, {server.HEADER: "wrong"}, {server.HEADER: SECRET, "Origin": "http://example.com"}):
            with self.assertRaises(websockets.InvalidStatus):
                await websockets.connect(self.url, additional_headers=headers)

    async def test_a_turn_streams_its_reply_back(self):
        ws = await self.connect()
        log = []
        await self.send(ws, noise(500) + tone(600) + noise(900, seed=1))
        start = await self.until(ws, "reply_start", log)
        self.assertEqual(start["rate"], OUT_RATE)
        done = await self.until(ws, "turn_done", log)
        self.assertEqual(log[:2], ["turn_start", "turn_end"])
        self.assertEqual(sum(self.frames), OUT_RATE * 2 * 2, "all 2 s of the reply")
        self.assertLessEqual(max(self.frames), OUT_RATE * 2 // 25, "no frame longer than 40 ms")
        self.assertIn("reply_end", log)
        self.assertIsNone(done["metrics"]["cancelled"])
        self.assertEqual(CALLS[0][9], "gated", "the live turn keeps the default gated pipeline")
        self.assertEqual(CALLS[0][0], "c", "gated injects the snapshot as in c")
        self.assertEqual(CALLS[0][7], "hybrid", "gated needs the hybrid segmenter")
        self.assertTrue((Path(self.tmp.name) / "turn_001.json").is_file())
        await ws.close()

    async def test_a_gated_turn_logs_the_shadow_after_its_reply(self):
        self.live.args.shadow = "on"
        self.live.shadow = server.shadow_log.Log(Path(self.tmp.name))
        self.live.decider = FakeDecider()
        with mock.patch.object(server, "run_clip", shadow_run_clip):
            ws = await self.connect()
            log = []
            await self.send(ws, noise(500) + tone(600) + noise(900, seed=1))
            done = await self.until(ws, "turn_done", log)
            self.assertEqual(done["turn"], 1, "the reply is the turn's, and it has played by the time the file is read")
            self.assertNotIn("shadow", log, "the shadow puts nothing on the wire")
            tasks = list(self.live.active.shadows)
            self.assertEqual(len(tasks), 1, "one call per utterance, booked after the turn's record")
            await asyncio.wait_for(asyncio.gather(*tasks), 10)
            await ws.close()
        lines = server.shadow_log.Log(Path(self.tmp.name)).lines()
        self.assertEqual(len(lines), 1)
        self.assertEqual((lines[0]["utterance"], lines[0]["turn_record"]), ("turn_001", "turn_001.json"))
        self.assertEqual(lines[0]["authority"], "decider")
        self.assertTrue(lines[0]["call"]["ok"], "one real call, timed by the record")
        self.assertEqual(lines[0]["decider"]["tasks"][0]["action"], "run")
        self.assertEqual(lines[0]["agreement"]["decider_only"], 1, "LFM3 made no call to agree with")

    async def test_shadow_off_makes_no_call_and_says_so(self):
        with mock.patch.object(server, "run_clip", shadow_run_clip):
            ws = await self.connect()
            await self.send(ws, noise(500) + tone(600) + noise(900, seed=1))
            await self.until(ws, "turn_done", [])
            await ws.close()
        self.assertEqual(list(self.live.active.shadows), [])
        self.assertFalse((Path(self.tmp.name) / "shadow.jsonl").exists())
        self.assertFalse(json.loads((Path(self.tmp.name) / "turn_001.json").read_text())["live"]["shadow"]["log"])
        self.assertFalse(json.loads((Path(self.tmp.name) / "turn_001.json").read_text())["live"]["shadow"]["booked"])

    async def test_a_failing_shadow_call_is_logged_and_leaves_the_turn_alone(self):
        self.live.args.shadow = "on"
        self.live.shadow = server.shadow_log.Log(Path(self.tmp.name))
        self.live.decider = FakeDecider(error=ConnectionError("decider down"))
        with mock.patch.object(server, "run_clip", shadow_run_clip):
            ws = await self.connect()
            log = []
            await self.send(ws, noise(500) + tone(600) + noise(900, seed=1))
            done = await self.until(ws, "turn_done", log)
            self.assertIn("reply_end", log)
            self.assertIsNone(done["error"])
            tasks = list(self.live.active.shadows)
            await asyncio.wait_for(asyncio.gather(*tasks), 10)
            await ws.close()
        line = server.shadow_log.Log(Path(self.tmp.name)).lines()[0]
        self.assertIn("decider down", line["error"])
        self.assertFalse(line["call"]["ok"])
        self.assertIsNone(json.loads((Path(self.tmp.name) / "turn_001.json").read_text()).get("error"))

    async def shadow_in_flight(self):
        """A connected call whose first turn's shadow request is in flight and stays there."""
        self.live.args.shadow = "on"
        self.live.shadow = server.shadow_log.Log(Path(self.tmp.name))
        self.live.decider = SlowShadow()
        ws = await self.connect()
        await self.send(ws, noise(500) + tone(600) + noise(900, seed=1))
        await self.until(ws, "turn_done", [])
        self.assertTrue(await asyncio.to_thread(self.live.decider.entered.wait, 5), "the shadow asked")
        return ws, list(self.live.active.shadows)

    async def test_a_turn_never_waits_on_a_shadow_call_in_flight(self):
        with mock.patch.object(server, "run_clip", shadow_run_clip):
            ws, shadows = await self.shadow_in_flight()
            turn = server.TurnDecider(self.live.decider, self.live.decider_lock)
            record = await asyncio.wait_for(asyncio.to_thread(turn.call, "decisions", {}, "turn_002:r1"), 2)
            self.assertEqual(record["status"], 200, "the turn's call went straight through")
            self.assertFalse(self.live.decider.aborted.is_set(), "and it did not need the shadow ended first")
            await ws.close()
            await asyncio.wait_for(asyncio.gather(*shadows), 10)

    async def test_a_new_utterance_ends_the_shadow_call_in_flight(self):
        with mock.patch.object(server, "run_clip", shadow_run_clip):
            ws, shadows = await self.shadow_in_flight()
            await self.send(ws, noise(500, seed=2) + tone(600) + noise(900, seed=3))
            await asyncio.wait_for(asyncio.gather(*shadows), 10)
            await ws.close()
        line = server.shadow_log.Log(Path(self.tmp.name)).lines()[0]
        self.assertEqual(line["abandoned"], "a new utterance started")
        self.assertFalse(line["call"]["ok"])

    async def test_hanging_up_ends_the_shadow_call_in_flight(self):
        with mock.patch.object(server, "run_clip", shadow_run_clip):
            ws, shadows = await self.shadow_in_flight()
            await ws.close()
            await asyncio.wait_for(asyncio.gather(*shadows), 10)
        self.assertEqual(server.shadow_log.Log(Path(self.tmp.name)).lines()[0]["abandoned"], "the call ended")
        self.assertFalse(self.live.shadow_lane.busy, "the next call starts with no shadow left in flight")

    async def test_the_shadow_does_not_ask_while_a_turn_runs_or_the_decider_is_busy(self):
        self.live.args.shadow = "on"
        self.live.shadow = server.shadow_log.Log(Path(self.tmp.name))
        self.live.decider = FakeDecider()
        ws = await self.connect()
        conversation = self.live.active
        asked = server.shadow_log.request("turn_001", "turn_001.json", "decider",
                                          {"state": {"transcript": "x"}, "questions": {}})
        await conversation.shadow_utterance(asked, 0.0, turn=object())
        with self.live.decider_lock:
            await conversation.shadow_utterance(asked, 0.0, turn=conversation.turn)
        await ws.close()
        self.assertEqual(self.live.decider.calls, [], "no request went out")
        self.assertEqual([line["skipped"] for line in server.shadow_log.Log(Path(self.tmp.name)).lines()],
                         ["a turn was running", "the decider was busy"])

    async def test_the_prompt_file_sets_the_turn_persona(self):
        (Path(self.tmp.name) / "prompt.txt").write_text("Be brief.\n", encoding="utf-8")
        ws = await self.connect()
        await self.send(ws, noise(500) + tone(600) + noise(900, seed=1))
        await self.until(ws, "turn_done", [])
        self.assertEqual(len(PERSONAS), 1)
        self.assertTrue(PERSONAS[0].startswith("Be brief."))
        self.assertIn(server.CALL_RULES, PERSONAS[0], "the call's rules follow the prompt")
        record = json.loads((Path(self.tmp.name) / "turn_001.json").read_text())
        self.assertTrue(record["live"]["prompt"].startswith("prompt.txt sha256:"))
        await ws.close()

    async def test_a_call_keeps_one_voice_session(self):
        ws = await self.connect()
        await self.send(ws, noise(500) + tone(600) + noise(900, seed=1))
        await self.until(ws, "turn_done", [])
        await self.send(ws, noise(500) + tone(600) + noise(900, seed=2))
        await self.until(ws, "turn_done", [])
        self.assertEqual(len(VOICES), 2)
        self.assertIs(VOICES[0], VOICES[1], "turn 2 adds to turn 1's session, it does not open one")
        self.assertFalse(VOICES[0].closed)
        first = json.loads((Path(self.tmp.name) / "turn_001.json").read_text())
        second = json.loads((Path(self.tmp.name) / "turn_002.json").read_text())
        self.assertEqual(second["live"]["call"], {"turn": 2, "session_turn": 2})
        self.assertNotEqual(first["live"]["call"], second["live"]["call"])
        await ws.close()

    async def test_the_snapshot_mode_and_the_last_snapshot_reach_the_turn(self):
        self.live.args.snapshot = "before-audio"
        ws = await self.connect()
        await self.send(ws, noise(500) + tone(600) + noise(900, seed=1))
        await self.until(ws, "turn_done", [])
        await self.send(ws, noise(500) + tone(600) + noise(900, seed=2))
        await self.until(ws, "turn_done", [])
        self.assertEqual([call[-1]["snapshot_mode"] for call in CALLS], ["before-audio", "before-audio"])
        self.assertIsNone(CALLS[0][-1]["prior_snapshot"], "the call's first turn has no task state yet")
        self.assertEqual(CALLS[1][-1]["prior_snapshot"], {"final_transcript": "state after turn_001"})
        record = json.loads((Path(self.tmp.name) / "turn_002.json").read_text())
        self.assertEqual(record["live"]["snapshot"], "before-audio", "the record says where the item went")
        await ws.close()

    async def test_a_long_call_stays_on_its_one_session(self):
        ws = await self.connect()
        for seed in range(1, 11):
            await self.send(ws, noise(500) + tone(600) + noise(900, seed=seed))
            await self.until(ws, "turn_done", [])
        self.assertEqual(len({id(voice) for voice in VOICES}), 1, "ten turns, one voice session, never restarted")
        self.assertFalse(VOICES[0].closed)
        self.assertNotIn("seed", CALLS[-1][-1], "nothing rebuilds the session from the call's text")
        record = json.loads((Path(self.tmp.name) / "turn_010.json").read_text())
        self.assertEqual(record["live"]["call"], {"turn": 10, "session_turn": 10})
        await ws.close()

    async def test_a_new_prompt_mid_call_waits_for_the_next_call(self):
        prompt = Path(self.tmp.name) / "prompt.txt"
        prompt.write_text("Be brief.\n", encoding="utf-8")
        ws = await self.connect()
        await self.send(ws, noise(500) + tone(600) + noise(900, seed=1))
        await self.until(ws, "turn_done", [])
        prompt.write_text("Be warm.\n", encoding="utf-8")
        await self.send(ws, noise(500) + tone(600) + noise(900, seed=2))
        await self.until(ws, "turn_done", [])
        self.assertIs(VOICES[0], VOICES[1], "the call keeps its session")
        self.assertTrue(PERSONAS[1].startswith("Be brief."), "and the persona it started with")
        await ws.close()

    def heard(self, text):
        async def transcribe(pcm):
            self.transcribed.append(len(pcm))
            if isinstance(text, Exception):
                raise text
            return text
        self.transcribed = []
        self.live.transcribe = transcribe

    def overlaps(self):
        path = Path(self.tmp.name) / "overlaps.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.is_file() else []

    async def reply_playing(self, ws):
        log = []
        await self.send(ws, noise(500) + tone(600) + noise(900, seed=1))
        await self.until(ws, "reply_start", log)
        return log

    async def test_speech_during_the_reply_stops_playback_and_keeps_the_call(self):
        self.heard("unused")
        ws = await self.connect()
        log = await self.reply_playing(ws)
        await self.send(ws, tone(800))
        barge = await self.until(ws, "barge_in", log)
        self.assertEqual(barge["turn"], 1, "the reply the person talked over is the one flushed")
        self.assertIn("duck", log, "the reply is turned down as soon as the speech starts")
        self.assertIn("reply_end", log, "the reply ended before the person spoke")
        after = []
        started = await self.until(ws, "turn_start", after)
        self.assertEqual(started["turn"], 2)
        await self.send(ws, noise(900, seed=2))
        await self.until(ws, "turn_end", after)
        while (done := await self.until(ws, "turn_done", after))["turn"] != 2:
            pass
        self.assertEqual(self.transcribed, [], "enough voiced speech needs no transcript")
        self.assertEqual((done["metrics"]["overlap"]["verdict"], done["metrics"]["overlap"]["how"]),
                         ("barge_in", "voiced"))
        self.assertFalse(VOICES[0].closed, "a barge-in keeps the call's session, and its history, open")
        self.assertIs(VOICES[0], VOICES[1], "the next turn continues on the same session")
        await ws.close()

    async def test_a_barge_in_during_a_streamed_reply_stops_its_audio_and_ends_it_once(self):
        self.heard("unused")
        with mock.patch.object(server, "run_clip", streaming_run_clip):
            ws = await self.connect()
            log = await self.reply_playing(ws)
            await self.send(ws, tone(800))
            await self.until(ws, "barge_in", log)
            sent_before = sum(self.frames)
            after = []
            while (await self.until(ws, "turn_done", after))["turn"] != 1:
                pass
            self.assertNotIn("audio", after, "nothing of the reply goes out once the person talks over it")
            self.assertEqual(after.count("reply_end"), 1, "the client still hears the reply end, once")
            self.assertEqual(log.count("reply_start"), 1, "one reply, however many pieces it was released in")
            self.assertLess(sent_before, OUT_RATE * 2 * 2, "the reply was cut before its 2 s were sent")
            self.assertFalse(VOICES[0].closed, "the call's one session stays open")
            await ws.close()

    async def test_the_barge_in_turn_keeps_the_words_that_caused_it(self):
        self.heard("unused")
        ws = await self.connect()
        log = await self.reply_playing(ws)
        await self.send(ws, tone(800) + noise(900, seed=2))
        while (await self.until(ws, "turn_done", log))["turn"] != 2:
            pass
        size = (Path(self.tmp.name) / "turn_002_input.wav").stat().st_size
        self.assertGreater(size, (800 + 700) * 32, "all 800 ms of speech and the trailing silence")
        await ws.close()

    async def test_a_backchannel_over_the_reply_leaves_it_playing(self):
        self.heard("Mm-hm.")
        ws = await self.connect()
        log = await self.reply_playing(ws)
        await self.send(ws, tone(300) + noise(900, seed=2))
        unduck = await self.until(ws, "unduck", log)
        self.assertEqual((unduck["verdict"], unduck["turn"]), ("backchannel", 1))
        self.assertNotIn("barge_in", log)
        self.assertEqual(log.count("turn_start"), 1, "a backchannel is not a turn")
        self.assertEqual(len(self.transcribed), 1)
        entry = self.overlaps()[0]
        self.assertEqual((entry["verdict"], entry["how"], entry["transcript"]), ("backchannel", "transcript", "Mm-hm."))
        self.assertTrue((Path(self.tmp.name) / entry["audio"]).is_file(), "a dismissed overlap keeps its audio")
        await ws.close()

    async def test_the_reply_coming_back_through_speakers_is_echo(self):
        self.heard("reply to turn")
        ws = await self.connect()
        log = await self.reply_playing(ws)
        await self.send(ws, tone(400) + noise(900, seed=2))
        unduck = await self.until(ws, "unduck", log)
        self.assertEqual(unduck["verdict"], "echo")
        self.assertNotIn("barge_in", log)
        await ws.close()

    async def test_a_short_command_over_the_reply_stops_it(self):
        self.heard("Stop.")
        ws = await self.connect()
        log = await self.reply_playing(ws)
        await self.send(ws, tone(300) + noise(900, seed=2))
        barge = await self.until(ws, "barge_in", log)
        self.assertEqual(barge["turn"], 1)
        started = await self.until(ws, "turn_start", log)
        self.assertEqual(started["turn"], 2)
        ended = await self.until(ws, "turn_end", log)
        self.assertEqual(ended["turn"], 2, "the endpointer already heard the command end")
        while (done := await self.until(ws, "turn_done", log))["turn"] != 2:
            pass
        self.assertEqual(done["metrics"]["overlap"]["transcript"], "Stop.")
        await ws.close()

    async def test_a_failed_transcript_keeps_the_words_as_a_turn(self):
        self.heard(ConnectionError("no slot"))
        ws = await self.connect()
        log = await self.reply_playing(ws)
        await self.send(ws, tone(300) + noise(900, seed=2))
        await self.until(ws, "barge_in", log)
        started = await self.until(ws, "turn_start", log)
        self.assertEqual(started["turn"], 2)
        await ws.close()

    async def test_a_blip_over_the_reply_is_noise_without_a_transcript(self):
        self.heard("unused")
        ws = await self.connect()
        log = await self.reply_playing(ws)
        await self.send(ws, tone(140) + noise(900, seed=2))
        unduck = await self.until(ws, "unduck", log)
        self.assertEqual(unduck["verdict"], "noise")
        self.assertEqual(self.transcribed, [])
        await ws.close()

    async def test_speech_the_reply_ends_under_becomes_a_plain_turn(self):
        self.heard("unused")
        ws = await self.connect()
        log = await self.reply_playing(ws)
        await self.send(ws, tone(200))
        await self.until(ws, "duck", log)
        await asyncio.sleep(2.3)
        await self.send(ws, tone(100))
        unduck = await self.until(ws, "unduck", log)
        self.assertEqual(unduck["verdict"], "turn")
        started = await self.until(ws, "turn_start", log)
        self.assertEqual(started["turn"], 2)
        self.assertNotIn("barge_in", log, "nothing was playing any more")
        await ws.close()

    async def test_speech_before_the_reply_continues_the_turn(self):
        ws = await self.connect()
        log = []
        await self.send(ws, noise(500) + tone(600) + noise(720, seed=1))
        await self.until(ws, "turn_end", log)
        await self.send(ws, tone(300))
        started = await self.until(ws, "turn_start", log)
        self.assertEqual(started["turn"], 2)
        self.assertGreater(started["carried_audio_s"], 1.0, "turn 2 starts with turn 1's audio")
        self.assertNotIn("barge_in", log)
        await ws.close()

    async def test_speech_after_the_reply_has_played_is_a_plain_turn(self):
        """Call 1 of 2026-11-16 15:26 logged 16 barge_in events; 15 came 1.5 to 17 s after the reply had played."""
        ws = await self.connect()
        await self.send(ws, noise(500) + tone(600) + noise(900, seed=1))
        await self.until(ws, "turn_done", [])
        await asyncio.sleep(2.3)
        log = []
        await self.send(ws, tone(140))
        started = await self.until(ws, "turn_start", log)
        self.assertEqual(started["turn"], 2)
        self.assertEqual(log, ["turn_start"], "nothing is playing, so there is nothing to duck or stop")
        await ws.close()

    async def test_a_second_client_is_turned_away(self):
        first = await self.connect()
        second = await websockets.connect(self.url, additional_headers={server.HEADER: SECRET})
        with self.assertRaises(websockets.ConnectionClosed):
            await second.recv()
        self.assertEqual(second.close_code, 1013)
        await first.close()


class SpeakTest(unittest.IsolatedAsyncioTestCase):
    """The live transport half of the fact check: the server speaks only what the turn hands it after the check, and
    a reply the person talked over is not sent at all."""

    async def conversation(self):
        live = SimpleNamespace(settings=server.Settings(silence_ms=700, start_ms=100, preroll_ms=300,
                                                        floor_dbfs=-50.0, margin_db=10.0, max_turn_s=30.0),
                               turns=0)
        conversation = server.Conversation(live, SimpleNamespace(send=None))
        conversation.sender.cancel()
        self.addAsyncCleanup(asyncio.gather, conversation.sender, return_exceptions=True)
        return conversation

    async def test_a_checked_reply_is_sent_start_audio_end(self):
        conversation = await self.conversation()
        turn = server.MicTurn(1, b"")
        turn.out_rate = OUT_RATE
        conversation.speak(turn, "I don't have the day for that yet.", b"\x01\x00" * OUT_RATE, True)
        kinds, frames = self.drain(conversation)
        self.assertEqual(kinds[0], "reply_start")
        self.assertEqual(kinds[-1], "reply_end")
        self.assertEqual(set(kinds[1:-1]), {"audio"})
        self.assertEqual(frames, OUT_RATE * 2, "one second of 16-bit audio")

    def drain(self, conversation):
        """What the client would get from the outbox: the send loop drops a muted turn's audio."""
        kinds, frames = [], 0
        while not conversation.outbox.empty():
            turn, payload = conversation.outbox.get_nowait()
            if isinstance(payload, bytes):
                if turn is not None and turn.muted:
                    continue
                kinds.append("audio")
                frames += len(payload)
                self.assertLessEqual(len(payload), OUT_RATE * 2 // 25, "no frame longer than 40 ms")
            else:
                kinds.append(json.loads(payload)["type"])
        return kinds, frames

    async def test_a_reply_released_in_pieces_is_one_reply_on_the_wire(self):
        conversation = await self.conversation()
        turn = server.MicTurn(1, b"")
        turn.out_rate = OUT_RATE
        conversation.speak(turn, "It's sunny", b"\x01\x00" * (OUT_RATE // 2), False)
        conversation.speak(turn, "It's sunny in Denver", b"\x01\x00" * (OUT_RATE // 2), False)
        conversation.speak(turn, "It's sunny in Denver tomorrow.", b"", True)
        kinds, frames = self.drain(conversation)
        self.assertEqual((kinds.count("reply_start"), kinds.count("reply_end")), (1, 1))
        self.assertEqual((kinds[0], kinds[-1]), ("reply_start", "reply_end"))
        self.assertEqual(frames, OUT_RATE * 2, "both pieces, one second in all")
        self.assertEqual(conversation.playback.text, "It's sunny in Denver", "the echo check hears each word once")

    async def test_a_turn_muted_mid_reply_sends_no_more_audio_and_still_ends_the_reply(self):
        conversation = await self.conversation()
        turn = server.MicTurn(1, b"")
        turn.out_rate = OUT_RATE
        conversation.speak(turn, "It's sunny", b"\x01\x00" * (OUT_RATE // 2), False)
        turn.mute("barge_in")
        conversation.speak(turn, "It's sunny in Denver", b"\x01\x00" * (OUT_RATE // 2), False)
        conversation.speak(turn, "It's sunny in Denver.", b"", True)
        kinds, _ = self.drain(conversation)
        self.assertEqual(kinds, ["reply_start", "reply_end"], "the muted turn's audio is dropped, its end is not")

    async def test_a_muted_turn_says_nothing(self):
        conversation = await self.conversation()
        turn = server.MicTurn(1, b"")
        turn.out_rate = OUT_RATE
        turn.muted = True
        conversation.speak(turn, "I'm running the task for Sunday.", b"\x01\x00" * OUT_RATE, True)
        self.assertTrue(conversation.outbox.empty(), "the person already talked over this reply")

    async def test_a_delta_on_the_session_alone_sends_no_audio(self):
        """The voice session is not the transport any more: only a checked reply reaches the client."""
        conversation = await self.conversation()
        conversation.voice_turn = server.MicTurn(1, b"")
        conversation.on_voice_event({"type": "response.output_audio.delta", "delta": "AAAA"})
        conversation.on_voice_event({"type": "response.done"})
        self.assertTrue(conversation.outbox.empty())
        self.assertFalse(conversation.voice_turn.reply_started, "nothing was spoken")


class ChunkTest(unittest.IsolatedAsyncioTestCase):
    async def test_upstream_audio_goes_to_the_asr_in_40_ms_chunks_and_to_the_voice_once_at_the_end(self):
        class Recorder(FakeSession):
            def __init__(self):
                super().__init__()
                self.chunks = []

            async def send(self, **event):
                if "audio" in event:
                    self.chunks.append(len(base64.b64decode(event["audio"])))

        turn = server.MicTurn(1, tone(200))
        turn.end(700)
        voice, asr = Recorder(), Recorder()
        await turn.stream(voice, asr)
        self.assertEqual(server.UP_CHUNK_BYTES, 1280, "40 ms of 16 kHz PCM16")
        self.assertEqual(max(asr.chunks), server.UP_CHUNK_BYTES)
        self.assertEqual(sum(asr.chunks), len(turn.pcm))
        self.assertEqual(voice.chunks, [], "a turn that is cancelled while streaming leaves the voice session empty")
        await turn.send_voice(voice)
        self.assertEqual(sum(voice.chunks), len(turn.pcm), "the voice session gets the whole turn when it ends")


class DefaultsTest(unittest.TestCase):
    def test_live_mode_sends_only_a_lookup_note_unless_told_otherwise(self):
        argv = mock.patch("sys.argv", ["server"])
        with argv, mock.patch.object(server, "main_async") as main_async, mock.patch.object(server.asyncio, "run"):
            server.main()
        self.assertEqual(main_async.call_args.args[0].snapshot, "note")

    def test_the_note_persona_is_the_default_without_a_prompt_file(self):
        self.assertEqual(server.load_persona(None, server.NOTE_PERSONA), (server.NOTE_PERSONA, "built-in default"))

    def test_live_mode_names_the_turn_a_question_asks_about_unless_told_otherwise(self):
        argv = mock.patch("sys.argv", ["server"])
        with argv, mock.patch.object(server, "main_async") as main_async, mock.patch.object(server.asyncio, "run"):
            server.main()
        self.assertEqual(main_async.call_args.args[0].recall, "note")


class BindTest(unittest.TestCase):
    def test_only_tailnet_or_loopback(self):
        for host in ("100.64.0.1", "127.0.0.1"):
            self.assertEqual(server.check_bind(host), host)
        for host in ("0.0.0.0", "192.168.0.10", "10.0.0.5"):
            with self.assertRaises(SystemExit):
                server.check_bind(host)

    def test_the_secret_is_made_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "live" / "secret"
            first = server.shared_secret(path)
            self.assertEqual(len(first), 64)
            self.assertEqual(server.shared_secret(path), first)

    def test_the_prompt_file_replaces_the_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "prompt.txt"
            self.assertEqual(server.load_persona(path), (server.PERSONA, "built-in default"))
            path.write_text("Be brief.\n", encoding="utf-8")
            text, label = server.load_persona(path)
            self.assertEqual(text, "Be brief.")
            self.assertTrue(label.startswith("prompt.txt sha256:"))


class FallbackAudioTest(unittest.TestCase):
    """The line a failed turn says reaches the client as audio at the session's own rate."""

    ARGS = SimpleNamespace(silence_ms=700, start_ms=100, preroll_ms=300, floor_dbfs=-50.0, margin_db=10.0,
                           max_turn_s=30.0, prompt=None, lfm_port=8099)

    def live(self, tmp):
        return server.Live(self.ARGS, "ws://unused", None, None, Path(tmp))

    def test_no_rendered_clip_means_no_audio(self):
        with tempfile.TemporaryDirectory() as tmp:
            live = self.live(tmp)
            for clip in (live.fallback, live.ask):
                self.assertEqual(clip.pcm(OUT_RATE), b"",
                                 "the turn still says the line in its record, but plays nothing")

    def test_a_rendered_clip_is_pcm_at_the_session_rate(self):
        from voice.live.tests.test_fallback import wav_bytes
        with tempfile.TemporaryDirectory() as tmp:
            live = self.live(tmp)
            live.fallback.wav = wav_bytes(16_000, [0] * 1600)
            self.assertEqual(len(live.fallback.pcm(OUT_RATE)) // 2, 2400)
            self.assertEqual(live.fallback.pcm(OUT_RATE), live.fallback.pcm(OUT_RATE),
                             "one conversion per rate, kept for the call")

    def test_a_failed_render_is_reported_and_changes_nothing_else(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(server.fallback, "wav", side_effect=OSError("no TTS")):
                live = self.live(tmp)
                live.render_lines()
            for clip in (live.fallback, live.ask):
                self.assertEqual(clip.wav, b"")
                self.assertIn("no TTS", clip.error)
                self.assertEqual(clip.pcm(OUT_RATE), b"")

    def test_both_lines_are_rendered_in_the_servers_voice(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(server.fallback, "wav", side_effect=lambda url, line: line.encode()) as render:
                self.live(tmp).render_lines()
            self.assertEqual([call.args for call in render.call_args_list],
                             [("http://127.0.0.1:8099", server.FALLBACK_LINE),
                              ("http://127.0.0.1:8099", server.ASK_INSTEAD_LINE)])


class LlamaServerSlotsTest(unittest.IsolatedAsyncioTestCase):
    """One llama-server slot holds the call's voice session for the whole call, so a call needs two more: the turn's
    ASR and the reply it generates. With one free slot (two in total) the ASR waited behind the previous turn's reply
    and its final transcript arrived tens of seconds late (measured: 2026-11-16 17:38 replay)."""

    def test_each_extra_asr_lane_gets_its_own_slot(self):
        self.assertEqual([server.slots(n) for n in (1, 2, 4)], [3, 4, 6],
                         "the voice session, one slot per ASR lane, and one for the reply")

    async def test_the_call_gets_a_slot_for_its_session_its_asr_and_its_reply(self):
        recorded = {}

        class FakeServer:
            startup_s = 0.0
            proc = SimpleNamespace(pid=1)

            def __init__(self, port, log_path, extra_args, slots):
                recorded["slots"], recorded["args"] = slots, tuple(extra_args)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        class FakeServe:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        @contextlib.contextmanager
        def fake_decider(name, out):
            yield SimpleNamespace(live_calls=lambda: 0)

        async def stop_now(*args, **kwargs):
            return None

        with tempfile.TemporaryDirectory() as tmp:
            args = SimpleNamespace(host="127.0.0.1", port=8799, lfm_port=8119, lfm_ctx=32768, lfm_args=None,
                                   stop_file=Path(tmp) / "STOP", decider_cache=Path(tmp) / "cache",
                                   decider_budget=200, out=Path(tmp) / "out", max_minutes=1.0, mock_tools=True,
                                   decider="liquid", snapshot="note", prompt=None, silence_ms=700, start_ms=100,
                                   preroll_ms=300, floor_dbfs=-50.0, margin_db=10.0, max_turn_s=30.0)
            with mock.patch.object(server, "Server", FakeServer), \
                    mock.patch.object(server, "serve", FakeServe), \
                    mock.patch.object(server, "make_decider", fake_decider), \
                    mock.patch.object(server, "wait_for_stop", stop_now), \
                    mock.patch.object(server, "shared_secret", lambda: "s" * 64), \
                    mock.patch.object(server, "use_decider_cache", lambda *a, **k: None):
                await server.main_async(args)
        self.assertGreaterEqual(recorded["slots"], 3)
        self.assertIn("-kvu", recorded["args"])


if __name__ == "__main__":
    unittest.main()
