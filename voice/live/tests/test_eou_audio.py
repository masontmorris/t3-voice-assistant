"""What `--eou audio` changes: the checks, the early end, and the silence rule that still ends every other turn.

The policy is the study's (docs/voice/audio-decider.md): ask P(done) at 150, 300 and 500 ms of silence, end the turn
at the first answer of 0.5 or more, and otherwise end it at `--silence-ms` exactly as before. The model is faked here;
the real one is measured on Mason's recorded calls (docs/voice/live-eou-audio.md).
"""

import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import websockets
from websockets.asyncio.server import serve

from voice.live import eou_audio, server
from voice.live.endpoint import FRAME_BYTES, Check, End, Endpointer, Settings
from voice.live.tests.test_endpoint import events as pushed, noise, tone
from voice.live.tests.test_server import FakeVoice, fake_run_clip

SECRET = "s" * 64
CHECKS = (150, 300, 500)
ARGS = dict(silence_ms=700, start_ms=100, preroll_ms=300, floor_dbfs=-50.0, margin_db=10.0, max_turn_s=30.0,
            delay=4.0, segmenter="hybrid", snapshot="reply", recall="note")


def watched(endpointer, pcm, on_check=None):
    """Feeds `pcm` one frame at a time, so a listener can end the turn on a `Check` while the silence still counts."""
    seen = []
    for offset in range(0, len(pcm), FRAME_BYTES):
        for event in endpointer.push(pcm[offset:offset + FRAME_BYTES]):
            seen.append(event)
            if isinstance(event, Check) and on_check is not None:
                on_check(event, endpointer)
    return seen


def end_ms(seen):
    return [event.trailing_ms for event in seen if isinstance(event, End)]


def check_ms(seen):
    return [event.silent_ms for event in seen if isinstance(event, Check)]


class CheckPointTest(unittest.TestCase):
    def test_the_checks_land_at_the_policy_points_before_the_silence_rule(self):
        got = pushed(Endpointer(Settings(check_ms=CHECKS)), noise(300) + tone(600) + noise(900, seed=1))
        self.assertEqual(check_ms(got), [150, 300, 500])
        self.assertEqual(end_ms(got), [700], "the fallback is still the silence rule")

    def test_a_turn_without_an_audio_decider_is_asked_nothing(self):
        got = pushed(Endpointer(), noise(300) + tone(600) + noise(900, seed=1))
        self.assertEqual(check_ms(got), [])

    def test_an_early_end_leaves_no_second_end(self):
        endpointer = Endpointer(Settings(check_ms=CHECKS))
        got = watched(endpointer, noise(300) + tone(600) + noise(1200, seed=1),
                      on_check=lambda event, ep: ep.finish(event.silent_ms) if event.silent_ms == 150 else None)
        self.assertEqual(end_ms(got), [], "the endpointer was ended by hand, so it owes no End")
        self.assertEqual(check_ms(got), [150], "the turn is over: no further checks and no fallback end")
        self.assertEqual(endpointer.state, "idle")

    def test_speech_after_a_pause_asks_the_checks_again(self):
        got = pushed(Endpointer(Settings(check_ms=(150,))),
                     noise(300) + tone(600) + noise(200, seed=1) + tone(100) + noise(800, seed=2))
        self.assertEqual(check_ms(got), [150, 150])
        self.assertEqual(end_ms(got), [700])


class FakeEou:
    """Stands in for the audio model: a list of answers, the last one repeated, or an exception to raise."""

    def __init__(self, answers=(0.9,), policy=eou_audio.Policy()):
        self.answers, self.policy, self.asked = list(answers), policy, []

    def p_done(self, pcm, timeout_s=None):
        self.asked.append({"bytes": len(pcm), "timeout_s": timeout_s})
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        if callable(answer):
            return answer(pcm)
        return eou_audio.Answer(answer, 2.0)


class AudioEndTest(unittest.IsolatedAsyncioTestCase):
    """One turn through the live server with the audio model in place of the clock."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        args = SimpleNamespace(**ARGS, eou="audio", prompt=Path(self.tmp.name) / "prompt.txt")
        self.eou = FakeEou()
        self.live = server.Live(args, "ws://unused", decider=None, tools=None, out=Path(self.tmp.name),
                                eou=self.eou, secret=SECRET)
        patch = mock.patch.object(server, "run_clip", fake_run_clip)
        patch.start()
        self.addCleanup(patch.stop)
        session = mock.patch.object(server.Session, "open", FakeVoice.open)
        session.start()
        self.addCleanup(session.stop)
        self.ws_server = await serve(self.live.handler, "127.0.0.1", 0, process_request=server.authorize(SECRET))
        self.url = f"ws://127.0.0.1:{self.ws_server.sockets[0].getsockname()[1]}"

    async def asyncTearDown(self):
        self.ws_server.close()
        await self.ws_server.wait_closed()

    async def connect(self):
        ws = await websockets.connect(self.url, additional_headers={server.HEADER: SECRET})
        await ws.send(json.dumps({"type": "hello", "mode": "vad"}))
        self.assertEqual(json.loads(await ws.recv())["type"], "ready")
        return ws

    async def send(self, ws, pcm):
        """Real-time pace, 10 ms a chunk, because the checks are a race against the silence rule."""
        for offset in range(0, len(pcm), 320):
            await ws.send(pcm[offset:offset + 320])
            await asyncio.sleep(0.01)

    async def end_of(self, pcm):
        """The client's view of one burst: its turn_end, and everything the server said for 0.3 s after it."""
        ws = await self.connect()
        await self.send(ws, pcm)
        log = []
        while True:
            message = await asyncio.wait_for(ws.recv(), 10)
            if isinstance(message, bytes):
                continue
            log.append(json.loads(message)["type"])
            if log[-1] == "turn_end":
                ended = json.loads(message)
                break
        try:
            while True:
                more = await asyncio.wait_for(ws.recv(), 0.3)
                if not isinstance(more, bytes):
                    log.append(json.loads(more)["type"])
        except (asyncio.TimeoutError, websockets.ConnectionClosed):
            pass
        await ws.close()
        return ended, log

    def record(self, n=1):
        return json.loads((Path(self.tmp.name) / f"turn_{n:03d}.json").read_text())

    async def test_a_finished_turn_ends_at_the_first_check_that_says_so(self):
        ended, log = await self.end_of(noise(300) + tone(3200) + noise(900, seed=1))
        self.assertEqual(ended["trailing_ms"], 150, "the 150 ms check point, the first frame at or past it")
        self.assertEqual(log.count("turn_end"), 1, "one turn, one end")
        self.assertEqual(len(self.eou.asked), 1, "the first answer was enough")
        self.assertEqual(self.eou.asked[0]["bytes"], 3 * 16_000 * 2, "the model hears only the turn's last 3 s")
        self.assertLessEqual(self.eou.asked[0]["timeout_s"], 0.5, "bounded well inside the 700 ms rule")
        live = self.record()["live"]
        self.assertEqual(live["eou"], "audio")
        self.assertEqual([check["silent_ms"] for check in live["eou_checks"]], [150])
        self.assertTrue(live["eou_checks"][0]["ended"])

    async def test_a_low_answer_waits_for_the_next_check(self):
        self.eou.answers = [0.1, 0.9]
        ended, _ = await self.end_of(noise(300) + tone(600) + noise(900, seed=1))
        self.assertEqual([ask["timeout_s"] for ask in self.eou.asked], [0.5, 0.4], "each check's own deadline")
        self.assertEqual(ended["trailing_ms"], 300, "the second check, at 300 ms, ended it")

    async def test_an_unfinished_turn_still_ends_at_the_silence_rule(self):
        self.eou.answers = [0.1]
        ended, log = await self.end_of(noise(300) + tone(600) + noise(900, seed=1))
        self.assertEqual(ended["trailing_ms"], 700)
        self.assertEqual([ask["timeout_s"] for ask in self.eou.asked], [0.5, 0.4, 0.2])
        self.assertEqual(log.count("turn_end"), 1)

    async def test_a_model_that_errors_falls_back_to_the_silence_rule(self):
        self.eou.answers = [ConnectionError("no slot")]
        ended, log = await self.end_of(noise(300) + tone(600) + noise(900, seed=1))
        self.assertEqual(ended["trailing_ms"], 700)
        self.assertEqual(log.count("turn_end"), 1, "the failure is not the turn's")
        check = self.record()["live"]["eou_checks"][0]
        self.assertEqual(check["error"], "ConnectionError")
        self.assertFalse(check["ended"])

    async def test_an_answer_that_arrives_after_the_person_speaks_again_ends_nothing(self):
        self.eou.answers = [lambda pcm: (time.sleep(0.2), eou_audio.Answer(0.9, 200))[1], 0.1]
        ended, log = await self.end_of(noise(300) + tone(600) + noise(160, seed=1) + tone(1000) + noise(900, seed=2))
        self.assertEqual(ended["trailing_ms"], 700, "he was talking again, so the check's answer was stale")
        self.assertEqual(log.count("turn_end"), 1)
        checks = self.record()["live"]["eou_checks"]
        self.assertEqual((checks[0]["p"], checks[0]["ended"]), (0.9, False))
        self.assertEqual(checks[-1]["silent_ms"], 500, "the trailing silence asked again at every point, and got a no")

    async def test_a_slow_model_never_holds_a_turn_past_the_silence_rule(self):
        self.eou.answers = [lambda pcm: (time.sleep(0.6), eou_audio.Answer(0.9, 600))[1]]
        ended, log = await self.end_of(noise(300) + tone(600) + noise(900, seed=1))
        self.assertEqual(ended["trailing_ms"], 700, "the turn ended while the answer was still coming")
        self.assertEqual(log.count("turn_end"), 1)
        await asyncio.sleep(0.3)
        self.assertEqual(log.count("turn_end"), 1, "the late answer ends nothing")
        check = self.record()["live"]["eou_checks"][0]
        self.assertIsNone(check["p"])
        self.assertEqual(check["error"], "TimeoutError")


class DefaultTest(unittest.TestCase):
    def test_without_the_audio_decider_the_silence_rule_is_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            live = server.Live(SimpleNamespace(**ARGS, eou="silence", prompt=None), "ws://unused", None, None,
                               Path(tmp))
        self.assertIsNone(live.eou)
        self.assertEqual(live.settings.check_ms, (), "no check points, so no model is asked")


if __name__ == "__main__":
    unittest.main()
