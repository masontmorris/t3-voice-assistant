"""Smart Turn on the CPU: a missing or broken model is a fallback to the silence rule, never a failed call."""

import importlib.util
import tempfile
import unittest
from pathlib import Path

import numpy as np

from voice.live import eou_smart_turn


class LoadTest(unittest.TestCase):
    def test_a_missing_model_loads_as_none_with_the_reason(self):
        decider, reason = eou_smart_turn.load(model=Path(tempfile.gettempdir()) / "no-such-smart-turn.onnx")
        self.assertIsNone(decider)
        self.assertIn("no model at", reason)

    def test_a_failed_load_leaves_the_process_threading_as_it_was(self):
        try:
            import torch
        except ImportError:
            self.skipTest("no torch in this environment")
        before = torch.get_num_threads()
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.onnx"
            bad.write_bytes(b"not a model")
            eou_smart_turn.load(model=bad)
        self.assertEqual(torch.get_num_threads(), before)

    def test_a_model_file_that_is_not_a_model_loads_as_none_with_the_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.onnx"
            bad.write_bytes(b"not a model")
            decider, reason = eou_smart_turn.load(model=bad)
        self.assertIsNone(decider)
        self.assertTrue(reason)


class PresetTest(unittest.TestCase):
    def test_the_preset_holds_and_never_ends_a_turn_early(self):
        policy = eou_smart_turn.POLICY
        self.assertEqual([policy.verdict(p) for p in (0.999, 0.5, 0.06, 0.05, 0.0)], [None, None, None, "hold", "hold"])
        self.assertLess(policy.check_ms[1], 700, "the hold is decided before the silence rule ends the turn")
        self.assertGreater(policy.hold_ms, 700)
        self.assertEqual(policy.fallback_ms(600, 700), 700)
        self.assertEqual(policy.fallback_ms(900, 700), policy.hold_ms)


@unittest.skipUnless(eou_smart_turn.MODEL.is_file(), "the Smart Turn weights are not under VOICE_HOME")
class ModelTest(unittest.TestCase):
    def setUp(self):
        missing = [name for name in ("onnxruntime", "transformers") if importlib.util.find_spec(name) is None]
        if missing:
            self.skipTest(f"no {' or '.join(missing)} in this environment")

    def test_loading_leaves_the_torch_thread_pool_alone(self):
        try:
            import torch
        except ImportError:
            self.skipTest("no torch in this environment")
        before = torch.get_num_threads()
        decider, reason = eou_smart_turn.load()
        self.assertIsNotNone(decider, reason)
        decider.p_done(bytes(32_000))
        self.assertEqual(torch.get_num_threads(), before)

    def test_the_features_are_whisper_log_mel_of_eight_seconds(self):
        decider, reason = eou_smart_turn.load()
        self.assertIsNotNone(decider, reason)
        features = decider.features(np.random.default_rng(0).normal(0, 0.05, 8 * 16_000).astype(np.float32))
        self.assertEqual(features.shape, (1, 80, 800))
        self.assertTrue(np.isfinite(features).all())

    def test_a_probability_comes_back_for_a_short_and_a_long_clip(self):
        decider, reason = eou_smart_turn.load()
        self.assertIsNotNone(decider, reason)
        t = np.arange(16_000) / 16_000
        tone = (np.sin(2 * np.pi * 220 * t) * 8000).astype("<i2").tobytes()
        for pcm in (tone, tone * 12):
            answer = decider.p_done(pcm)
            self.assertTrue(0.0 <= answer.p_done <= 1.0)
            self.assertGreater(answer.ms, 0)


if __name__ == "__main__":
    unittest.main()
