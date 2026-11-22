"""What `--eou audio` changes: the checks, the early end, and the silence rule that still ends every other turn.

The policy is the study's (docs/voice/audio-decider.md): ask P(done) at 150, 300 and 500 ms of silence, end the turn
at the first answer of 0.5 or more, and otherwise end it at `--silence-ms` exactly as before. The model is faked here;
the real one is measured on Mason's recorded calls (docs/voice/live-eou-audio.md).
"""

import asyncio
import contextlib
import io
import json
import tempfile
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


class EndRig(unittest.IsolatedAsyncioTestCase):
    """One turn through the live server with the audio model in place of the clock."""

    policy = eou_audio.Policy()

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        args = SimpleNamespace(**ARGS, eou="audio", prompt=Path(self.tmp.name) / "prompt.txt")
        self.eou = FakeEou(policy=self.policy)
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

class AudioEndTest(EndRig):
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


class HoldPolicyTest(unittest.TestCase):
    def test_the_verdict_is_end_hold_or_nothing(self):
        policy = eou_audio.Policy(theta=0.8, hold_theta=0.1, hold_ms=1500)
        self.assertEqual([policy.verdict(p) for p in (0.95, 0.8, 0.5, 0.1, 0.02)], ["end", "end", None, "hold", "hold"])
        self.assertIsNone(eou_audio.Policy().verdict(0.0), "hold_theta 0 is off: today's policy never holds")

    def test_an_answer_is_judged_against_its_pause_and_its_deadline(self):
        policy = eou_audio.Policy(check_ms=(500,), theta=0.8, hold_theta=0.1, hold_ms=2000)
        endpointer = Endpointer(Settings(check_ms=(500,), hold_ms=2000))
        check = [e for e in pushed(endpointer, noise(300) + tone(600) + noise(520, seed=1)) if isinstance(e, Check)][0]
        fast, slow = eou_audio.Answer(0.95, 50.0), eou_audio.Answer(0.95, 250.0)
        self.assertEqual(policy.judge(endpointer, check, fast, 700), "end")
        self.assertIsNone(policy.judge(endpointer, check, slow, 700), "the 500 ms check has 200 ms")
        pushed(endpointer, tone(100))
        self.assertIsNone(policy.judge(endpointer, check, fast, 700), "speech resumed: another pause")

    def test_the_fallback_is_the_silence_rule_until_it_is_past_then_the_cap(self):
        policy = eou_audio.Policy(hold_ms=1500)
        self.assertEqual([policy.fallback_ms(s, 700) for s in (500, 700, 900)], [700, 1500, 1500])
        self.assertEqual(eou_audio.Policy().fallback_ms(900, 700), 700)


class OpenEouTest(unittest.TestCase):
    def test_smart_without_its_model_says_so_once_and_leaves_the_silence_rule(self):
        said = io.StringIO()
        with mock.patch.object(server.eou_smart_turn, "load", return_value=(None, "no model at x")), \
                contextlib.redirect_stdout(said):
            self.assertIsNone(server.open_eou(SimpleNamespace(), "smart", None, 0))
        self.assertEqual(said.getvalue().count("Smart Turn is not available"), 1)
        self.assertIn("silence rule", said.getvalue())

    def test_the_silence_mode_opens_nothing(self):
        self.assertIsNone(server.open_eou(SimpleNamespace(), "silence", None, 0))

    def test_flags_override_the_modes_policy(self):
        args = SimpleNamespace(eou_theta=0.9, eou_hold_theta=None, eou_hold_ms=3000, eou_checks="200,400")
        policy = server.eou_policy(args, eou_audio.Policy())
        self.assertEqual((policy.theta, policy.hold_theta, policy.hold_ms, policy.check_ms), (0.9, 0.0, 3000, (200, 400)))


class DefaultTest(unittest.TestCase):
    def test_without_the_audio_decider_the_silence_rule_is_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            live = server.Live(SimpleNamespace(**ARGS, eou="silence", prompt=None), "ws://unused", None, None,
                               Path(tmp), secret=SECRET)
        self.assertIsNone(live.eou)
        self.assertEqual(live.settings.check_ms, (), "no check points, so no model is asked")


if __name__ == "__main__":
    unittest.main()
