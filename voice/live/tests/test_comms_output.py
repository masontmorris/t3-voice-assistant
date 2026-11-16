import unittest

from voice.live.client import mme_output
from voice.live.tests.test_rawmic import FakeSd

APIS = [{"name": "MME"}, {"name": "Windows WASAPI"}]
DEVICES = [
    {"name": "Microsoft Sound Mapper - Output", "hostapi": 0, "max_output_channels": 2},
    {"name": "Speakers (iFi (by AMR) HD USB A", "hostapi": 0, "max_output_channels": 2},
    {"name": "Speakers (Steam Streaming Speak", "hostapi": 0, "max_output_channels": 2},
    {"name": "Speakers (iFi (by AMR) HD USB Audio)", "hostapi": 1, "max_output_channels": 2},
]


class MmeOutputTest(unittest.TestCase):
    def test_the_truncated_mme_name_of_the_communications_speakers_is_found(self):
        self.assertEqual(mme_output(FakeSd(DEVICES, APIS), "Speakers (iFi (by AMR) HD USB Audio)"), 1)

    def test_no_match_falls_back_to_the_default(self):
        self.assertIsNone(mme_output(FakeSd(DEVICES, APIS), "Headphones (Bluetooth)"))


if __name__ == "__main__":
    unittest.main()
