import unittest

import numpy as np

from voice.live.rawmic import Decimator, wasapi_input


class FakeSd:
    def __init__(self, devices, apis):
        self.devices, self.apis = devices, apis

    def query_hostapis(self):
        return self.apis

    def query_devices(self):
        return self.devices


APIS = [{"name": "MME", "default_input_device": 1}, {"name": "Windows WASAPI", "default_input_device": 3}]
DEVICES = [
    {"name": "Speakers (SoundWire Audio)", "hostapi": 0, "max_input_channels": 0},
    {"name": "Microphone Array (Intel® Smart ", "hostapi": 0, "max_input_channels": 2},
    {"name": "Speakers (SoundWire Audio)", "hostapi": 1, "max_input_channels": 0},
    {"name": "Microphone Array (Intel® Smart Sound Technology for Digital Microphones)", "hostapi": 1,
     "max_input_channels": 2},
]


class WasapiInputTest(unittest.TestCase):
    def test_the_default_is_wasapis_default_input(self):
        self.assertEqual(wasapi_input(FakeSd(DEVICES, APIS), None), 3)

    def test_an_mme_index_maps_to_the_wasapi_device_of_the_same_microphone(self):
        self.assertEqual(wasapi_input(FakeSd(DEVICES, APIS), 1), 3)

    def test_a_name_fragment_finds_the_wasapi_input(self):
        self.assertEqual(wasapi_input(FakeSd(DEVICES, APIS), "microphone array"), 3)

    def test_no_wasapi_means_no_raw_capture(self):
        self.assertIsNone(wasapi_input(FakeSd(DEVICES[:2], APIS[:1]), None))


class DecimatorTest(unittest.TestCase):
    def test_blocks_of_any_length_give_the_same_samples_as_one_pass(self):
        signal = np.random.default_rng(0).normal(size=4800)
        whole = Decimator(3)(signal)
        decimate, parts, start = Decimator(3), [], 0
        for size in (7, 480, 1, 1000, 2, 3310):
            parts.append(decimate(signal[start:start + size]))
            start += size
        np.testing.assert_allclose(np.concatenate(parts), whole)
        self.assertEqual(len(whole), 1600)

    def test_speech_band_passes_and_content_above_the_new_nyquist_is_removed(self):
        t = np.arange(48000) / 48000
        def level(freq):
            out = Decimator(3)(np.sin(2 * np.pi * freq * t))[200:]
            return np.sqrt(np.mean(out ** 2)) / np.sqrt(0.5)
        self.assertGreater(level(1000), 0.95)
        self.assertLess(level(12000), 0.01)


if __name__ == "__main__":
    unittest.main()
