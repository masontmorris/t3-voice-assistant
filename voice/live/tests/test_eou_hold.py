"""The hold and early end through the live server, driven frame by frame with the model's answers handed over by the test.

No clock is involved: the test feeds 20 ms frames to the conversation and decides, at each check, whether and when the
model's answer arrives, so the order of "answer", "speech again" and "turn ended" is the test's and never a race.
"""

import asyncio
import os
import queue
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from voice.live import eou_audio, server
from voice.live.endpoint import FRAME_BYTES
from voice.live.tests.test_endpoint import noise, tone

SECRET = "s" * 64
ARGS = dict(silence_ms=700, start_ms=100, preroll_ms=300, floor_dbfs=-50.0, margin_db=10.0, max_turn_s=30.0,
            delay=4.0, segmenter="hybrid", snapshot="reply", recall="note", eou="audio")
POLICY = eou_audio.Policy(check_ms=(150, 300, 500, 900, 1200), theta=0.8, hold_theta=0.1, hold_ms=1500)


class Model:
    """The end-of-turn model: it answers a question only when the test puts an answer in."""

    def __init__(self, policy):
        self.policy, self.asked, self.answers = policy, [], queue.Queue()

    def p_done(self, pcm, timeout_s=None):
        self.asked.append(timeout_s)
        item = self.answers.get(timeout=10)  # a bound for a test that fails: nothing passing waits on it
        if isinstance(item, Exception):
            raise item
        return eou_audio.Answer(*item)

    def answer(self, p, ms=2.0):
        self.answers.put((p, ms))

    def fail(self, exc):
        self.answers.put(exc)


class Rig(unittest.IsolatedAsyncioTestCase):
    policy = POLICY

    async def asyncSetUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.model = Model(self.policy)
        live = server.Live(SimpleNamespace(**ARGS, prompt=Path(tmp.name) / "prompt.txt"), "ws://unused", None, None,
                           Path(tmp.name), eou=self.model, secret=SECRET)

        async def no_pipeline(conversation, turn, prior):
            return

        patch = mock.patch.object(server.Conversation, "run_turn", no_pipeline)
        patch.start()
        self.addCleanup(patch.stop)
        self.conversation = server.Conversation(live, None, SECRET)
        self.live, self.handled = live, set()
        self.addAsyncCleanup(self.release_threads)

    async def release_threads(self):
        for _ in range(len(self.live.settings.check_ms) * 4):
            self.model.fail(ConnectionError("test over"))
        await asyncio.sleep(0)

    @property
    def turn(self):
        return self.conversation.turn

    def pending(self):
        check = getattr(self.turn, "check", None)
        return check if check is not None and not check.done() and check not in self.handled else None

    async def drive(self, pcm, answers=()):
        """Feeds `pcm` a frame at a time. Each check the server asks is handed the next of `answers` at once: a
        (p_done, ms) pair, an exception, or None to leave that check unanswered."""
        answers = iter(answers)
        for offset in range(0, len(pcm), FRAME_BYTES):
            self.conversation.on_audio(pcm[offset:offset + FRAME_BYTES])
            check = self.pending()
            if check is None:
                continue
            self.handled.add(check)
            reply = next(answers, (0.4, 2.0))
            if reply is None:
                self.unanswered = check
                continue
            self.model.fail(reply) if isinstance(reply, Exception) else self.model.answer(*reply)
            await check

    def trailing_ms(self):
        return round(self.turn.metrics()["detector_s"] * 1000)

    def records(self):
        return self.turn.checks


class HoldTest(Rig):
    async def test_a_pause_the_model_calls_unfinished_holds_the_turn_to_the_cap(self):
        await self.drive(noise(300) + tone(600) + noise(2200, seed=1), [(0.05, 2.0)] * 5)
        self.assertEqual(self.trailing_ms(), 1500)
        self.assertEqual([c["silent_ms"] for c in self.records()], [150, 300, 500, 900, 1200])
        self.assertTrue(all(c["held"] and not c["ended"] for c in self.records()))
        self.assertEqual(self.model.asked, [0.5, 0.4, 0.2, 0.5, 0.3], "past the silence rule a deadline is the cap's")

    async def test_a_held_turn_ends_at_the_first_later_check_that_says_finished(self):
        await self.drive(noise(300) + tone(600) + noise(2200, seed=1), [(0.05, 2.0)] * 3 + [(0.9, 2.0)])
        self.assertEqual(self.trailing_ms(), 900)
        self.assertTrue(self.records()[-1]["ended"])

    async def test_an_unsure_answer_leaves_the_silence_rule_exactly(self):
        await self.drive(noise(300) + tone(600) + noise(2200, seed=1), [(0.4, 2.0)] * 5)
        self.assertEqual(self.trailing_ms(), 700)
        self.assertFalse(any(c["held"] or c["ended"] for c in self.records()))

    async def test_a_model_that_errors_never_holds_a_turn(self):
        await self.drive(noise(300) + tone(600) + noise(2200, seed=1), [ConnectionError("no slot")] * 5)
        self.assertEqual(self.trailing_ms(), 700)
        self.assertEqual({c["error"] for c in self.records()}, {"ConnectionError"})

    async def test_an_answer_past_its_deadline_is_a_timeout_and_holds_nothing(self):
        async def timed_out(awaitable, seconds):
            awaitable.close()
            raise TimeoutError

        with mock.patch.object(server, "bounded", timed_out):
            await self.drive(noise(300) + tone(600) + noise(2200, seed=1), [(0.05, 2.0)] * 5)
        self.assertEqual(self.trailing_ms(), 700)
        self.assertEqual({c["error"] for c in self.records()}, {"TimeoutError"})

    async def test_an_answer_that_arrives_after_the_silence_rule_ended_the_turn_holds_nothing(self):
        await self.drive(noise(300) + tone(600) + noise(1200, seed=1), [None])
        self.assertEqual(self.trailing_ms(), 700)
        self.model.answer(0.05)
        await self.unanswered
        self.assertEqual(self.trailing_ms(), 700)
        self.assertFalse(self.records()[0]["held"])

    async def test_speech_after_a_held_pause_goes_on_in_the_same_turn(self):
        await self.drive(noise(300) + tone(600) + noise(900, seed=1) + tone(600) + noise(2200, seed=2),
                         [(0.05, 2.0)] * 9)
        self.assertEqual(self.live.turns, 1, "the 900 ms pause was held, so it did not split the request")
        self.assertEqual(self.trailing_ms(), 1500)


class StalePauseTest(Rig):
    """A check asked in one pause is answered in a later one that has lasted as long."""

    async def stale(self, p_done):
        await self.drive(noise(300) + tone(600) + noise(200, seed=1) + tone(300) + noise(200, seed=2), [None])
        self.assertIsNone(self.turn.turn_end, "the turn is still open in the second pause")
        self.assertGreaterEqual(self.conversation.endpointer.silent_ms, 150, "as long a silence as the check was asked at")
        self.model.answer(p_done)
        await self.unanswered

    async def test_an_answer_about_an_earlier_pause_does_not_end_the_turn_in_a_later_one(self):
        await self.stale(0.99)
        self.assertIsNone(self.turn.turn_end)
        self.assertEqual([(c["ended"], c["held"]) for c in self.records()], [(False, False)])

    async def test_an_answer_about_an_earlier_pause_does_not_hold_a_later_one(self):
        await self.stale(0.01)
        self.assertEqual([(c["ended"], c["held"]) for c in self.records()], [(False, False)])
        await self.drive(noise(1000, seed=3), [(0.4, 2.0)] * 4)
        self.assertEqual(self.trailing_ms(), 700, "the second pause ended at the silence rule, not at the hold cap")

    async def test_an_answer_about_the_pause_still_running_does_act(self):
        await self.drive(noise(300) + tone(600) + noise(200, seed=1), [None])
        self.model.answer(0.99)
        await self.unanswered
        self.assertTrue(self.records()[0]["ended"])


class HoldCapTest(Rig):
    policy = eou_audio.Policy(check_ms=(500, 600, 900, 1200, 2500), theta=1.01, hold_theta=0.05, hold_ms=eou_audio.MAX_HOLD_MS)

    async def test_a_hold_never_outlasts_the_maximum_whatever_the_flag_says(self):
        flagged = server.eou_policy(SimpleNamespace(eou_theta=None, eou_hold_theta=None, eou_hold_ms=9000, eou_checks=None),
                                    self.policy)
        self.assertEqual(flagged.hold_ms, eou_audio.MAX_HOLD_MS)
        self.assertEqual(self.live.settings.hold_ms, eou_audio.MAX_HOLD_MS)
        await self.drive(noise(300) + tone(600) + noise(4000, seed=1), [(0.01, 2.0)] * 5)
        self.assertEqual(self.trailing_ms(), eou_audio.MAX_HOLD_MS)

    def test_the_flag_over_the_maximum_is_cut_to_it_and_said(self):
        said = []
        with mock.patch("builtins.print", lambda *a, **k: said.append(" ".join(map(str, a)))):
            policy = server.eou_policy(SimpleNamespace(eou_theta=None, eou_hold_theta=None, eou_hold_ms=9000,
                                                       eou_checks=None), eou_audio.Policy())
        self.assertEqual(policy.hold_ms, eou_audio.MAX_HOLD_MS)
        self.assertTrue(any("maximum" in line for line in said))

    def test_no_policy_holds_longer_than_the_maximum_or_less_than_nothing(self):
        self.assertEqual(eou_audio.Policy(hold_ms=10 ** 6).hold_ms, eou_audio.MAX_HOLD_MS)
        self.assertEqual(eou_audio.Policy(hold_ms=-5).hold_ms, 0)
        self.assertEqual(eou_audio.Policy(hold_ms=2000).hold_ms, 2000)


class DefaultModeTest(unittest.TestCase):
    def test_unset_or_empty_is_silence(self):
        self.assertEqual(server.eou_default({}), "silence")
        self.assertEqual(server.eou_default({"VOICE_EOU": ""}), "silence")

    def test_a_mode_in_the_variable_is_the_default(self):
        for mode in server.EOU_MODES:
            self.assertEqual(server.eou_default({"VOICE_EOU": mode}), mode)

    def test_a_value_that_is_not_a_mode_stops_the_server_and_names_it(self):
        with self.assertRaises(SystemExit) as stopped:
            server.eou_default({"VOICE_EOU": "smrat"})
        self.assertIn("smrat", str(stopped.exception))

    def test_the_server_command_stops_before_starting_anything_on_a_bad_variable(self):
        with mock.patch.dict(os.environ, {"VOICE_EOU": "smrat"}), mock.patch.object(sys, "argv", ["server"]):
            with self.assertRaises(SystemExit):
                server.main()


if __name__ == "__main__":
    unittest.main()
