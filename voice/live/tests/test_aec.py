import json
import unittest

import numpy as np

from voice.live.aec import FRAME_BYTES, EchoCanceller, encode, frame_dbfs
from voice.live.tests.synthetic import Scene, echo_path, make_scene, run_scene

TAIL = slice(int(2.0 * 16_000), int(5.5 * 16_000))


def clean(scene, **options):
    return run_scene(scene, EchoCanceller(**options), playback_rate=24_000)


class EchoOnlyTest(unittest.TestCase):
    """The reply's own echo must never start a turn, at any speaker volume."""

    def test_no_reply_starts_a_turn_from_its_own_echo(self):
        for echo_dbfs in (-40, -32, -26, -20, -14):
            with self.subTest(echo_dbfs=echo_dbfs):
                result = clean(make_scene(6.0, echo_dbfs=echo_dbfs))
                self.assertEqual(result.starts, (), "the echo alone began a turn")

    def test_the_echo_in_the_sent_audio_is_well_below_the_endpointer_floor(self):
        for echo_dbfs in (-40, -32, -26, -20):
            with self.subTest(echo_dbfs=echo_dbfs):
                scene = make_scene(6.0, echo_dbfs=echo_dbfs)
                result = clean(scene)
                drop = frame_dbfs(scene.microphone[TAIL]) - frame_dbfs(result.cleaned[TAIL])
                self.assertGreaterEqual(drop, 18.0, f"echo fell only {drop:.1f} dB")
                self.assertLess(frame_dbfs(result.cleaned[TAIL]), -55.0)

    def test_the_filter_cancels_echo_before_the_suppressor(self):
        result = clean(make_scene(6.0, echo_dbfs=-26))
        self.assertGreaterEqual(result.stats["erle_db"], 10.0)
        self.assertLessEqual(result.stats["echo_loss_db"], -8.0)

    def test_a_gap_in_playback_does_not_start_a_turn(self):
        result = clean(make_scene(8.0, echo_dbfs=-26, reply_gaps=((1.0, 1.6), (3.0, 3.6))))
        self.assertEqual(result.starts, (), "the end of a playback burst began a turn")
        self.assertGreater(result.stats["tail_frames"], 0, "the echo tail was not held")

    def test_a_long_echo_delay_is_covered(self):
        for delay_ms in (120.0, 220.0):
            with self.subTest(delay_ms=delay_ms):
                self.assertEqual(clean(make_scene(6.0, echo_dbfs=-26, delay_ms=delay_ms)).starts, ())

    def test_a_reverberant_room_does_not_start_a_turn(self):
        self.assertEqual(clean(make_scene(6.0, echo_dbfs=-26, tail_ms=100.0)).starts, ())

    def test_another_echo_path_does_not_start_a_turn(self):
        for seed in (7, 11, 23):
            with self.subTest(seed=seed):
                self.assertEqual(clean(make_scene(6.0, echo_dbfs=-26, seed=seed)).starts, ())

    def test_a_path_change_mid_reply_does_not_start_a_turn(self):
        scene = make_scene(10.0, echo_dbfs=-26.0)
        moved = np.convolve(scene.reference, echo_path(99, 60.0, 60.0))[: len(scene.reference)]
        moved *= 10 ** (-26 / 20) / (np.sqrt(np.mean(moved ** 2)) + 1e-12)
        microphone = scene.microphone.copy()
        microphone[5 * 16_000:] = moved[5 * 16_000:] + \
            0.00056 * np.random.default_rng(5).standard_normal(len(microphone) - 5 * 16_000)
        result = clean(Scene(scene.reference, microphone, scene.echo, scene.speech))
        self.assertEqual(result.starts, ())


class InterruptionTest(unittest.TestCase):
    """A person talking over the reply must still start the next turn, at their own level."""

    def test_a_person_talking_over_the_reply_starts_a_turn(self):
        for echo_dbfs, user_dbfs in ((-32, -24), (-26, -18), (-20, -12), (-14, -8)):
            with self.subTest(echo=echo_dbfs, user=user_dbfs):
                result = clean(make_scene(6.0, echo_dbfs=echo_dbfs, user=(2.0, 3.0, user_dbfs)))
                self.assertTrue(result.starts, "a real interruption was suppressed")
                self.assertGreaterEqual(result.starts[0], 2.0)
                self.assertLessEqual(result.starts[0], 2.5)

    def test_an_interruption_keeps_its_level(self):
        scene = make_scene(6.0, echo_dbfs=-26, user=(2.0, 3.0, -18))
        result = clean(scene)
        window = slice(int(2.2 * 16_000), int(2.9 * 16_000))
        self.assertLess(abs(frame_dbfs(result.cleaned[window]) - frame_dbfs(scene.microphone[window])), 3.0)

    def test_a_person_in_a_playback_gap_starts_a_turn(self):
        result = clean(make_scene(8.0, echo_dbfs=-26, user=(3.0, 3.5, -20),
                                  reply_gaps=((1.0, 1.6), (3.0, 3.6))))
        self.assertTrue(result.starts)
        self.assertLess(abs(result.starts[0] - 3.1), 0.2)

    def test_headphones_pass_speech_at_its_own_level(self):
        scene = make_scene(6.0, echo_dbfs=None, user=(2.0, 3.0, -20))
        result = clean(scene)
        window = slice(int(2.2 * 16_000), int(2.9 * 16_000))
        self.assertTrue(result.starts)
        self.assertLess(abs(frame_dbfs(result.cleaned[window]) - frame_dbfs(scene.microphone[window])), 1.5)

    def test_the_playback_rate_does_not_change_the_decision(self):
        for rate in (16_000, 24_000):
            with self.subTest(rate=rate):
                scene = make_scene(6.0, echo_dbfs=-26, user=(2.0, 3.0, -18))
                result = run_scene(scene, EchoCanceller(), playback_rate=rate)
                self.assertTrue(result.starts)
                self.assertGreaterEqual(result.starts[0], 2.0)
                self.assertLessEqual(result.starts[0], 2.5)


class FrameTest(unittest.TestCase):
    def test_frames_without_playback_are_sent_unchanged(self):
        canceller = EchoCanceller()
        for count in (1, 160, 320, 480):
            with self.subTest(count=count):
                samples = np.random.default_rng(count).standard_normal(count) * 0.1
                pcm = encode(samples)
                self.assertEqual(canceller.process(pcm, 1.0 + count / 16_000), pcm)

    def test_the_sample_count_is_preserved(self):
        canceller = EchoCanceller()
        for count in (1, 160, 320, 480):
            with self.subTest(count=count):
                pcm = (np.random.default_rng(count).standard_normal(count) * 32767 / 4).astype("<i2").tobytes()
                self.assertEqual(len(canceller.process(pcm, 2.0 + count / 16_000)), len(pcm))

    def test_the_output_is_valid_pcm16_mono(self):
        scene = make_scene(2.0, echo_dbfs=-26, user=(1.0, 1.5, -18))
        result = run_scene(scene, EchoCanceller(), playback_rate=24_000)
        self.assertEqual(result.cleaned.dtype, np.float32)
        self.assertFalse(np.isnan(result.cleaned).any())
        self.assertLessEqual(float(np.max(np.abs(result.cleaned))), 1.0)

    def test_stats_are_json_safe(self):
        result = clean(make_scene(2.0, echo_dbfs=-26))
        json.dumps(result.stats)
        for key in ("frames", "playing_frames", "double_talk_frames", "suppressed_frames", "erle_db"):
            self.assertIn(key, result.stats)
        self.assertEqual(result.stats["frames"], 100)

    def test_reset_returns_to_the_initial_state(self):
        scene = make_scene(2.0, echo_dbfs=-26)
        canceller = EchoCanceller()
        run_scene(scene, canceller, playback_rate=24_000)
        self.assertGreater(canceller.stats()["frames"], 0)
        canceller.reset()
        stats = canceller.stats()
        self.assertEqual(stats["frames"], 0)
        self.assertIsNone(stats["erle_db"])
        self.assertIsNone(stats["echo_loss_db"])
        frame = (np.random.default_rng(0).standard_normal(FRAME_BYTES // 2) * 0.1).astype("<i2").tobytes()
        self.assertEqual(canceller.process(frame, 3.0), frame)


if __name__ == "__main__":
    unittest.main()
