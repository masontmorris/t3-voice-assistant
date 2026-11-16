import unittest

import numpy as np

from voice.live.endpoint import FRAME_BYTES, FRAME_MS, RATE, Audio, End, Endpointer, Settings, Start


def tone(ms, dbfs=-20.0):
    t = np.arange(RATE * ms // 1000) / RATE
    return (np.sin(2 * np.pi * 220 * t) * 10 ** (dbfs / 20) * np.sqrt(2) * 32767).astype("<i2").tobytes()


def noise(ms, dbfs=-60.0, seed=0):
    samples = np.random.default_rng(seed).normal(0, 10 ** (dbfs / 20), RATE * ms // 1000)
    return (samples * 32767).astype("<i2").tobytes()


def events(endpointer, pcm, chunk=FRAME_BYTES * 3 + 2):
    out = []
    for offset in range(0, len(pcm), chunk):
        out.extend(endpointer.push(pcm[offset:offset + chunk]))
    return out


class EndpointerTest(unittest.TestCase):
    def test_one_turn_ends_after_the_silence(self):
        got = events(Endpointer(), noise(500) + tone(600) + noise(1000, seed=1))
        starts = [e for e in got if isinstance(e, Start)]
        ends = [e for e in got if isinstance(e, End)]
        self.assertEqual(len(starts), 1)
        self.assertEqual([e.trailing_ms for e in ends], [700])
        self.assertEqual(len(starts[0].pcm), (300 + 100) // FRAME_MS * FRAME_BYTES, "pre-roll plus the start frames")

    def test_turn_audio_is_every_frame_from_start_to_end(self):
        got = events(Endpointer(), noise(500) + tone(600) + noise(1000, seed=1))
        first = next(i for i, e in enumerate(got) if isinstance(e, Start))
        last = next(i for i, e in enumerate(got) if isinstance(e, End))
        frames = got[first + 1:last]
        self.assertTrue(all(isinstance(e, Audio) for e in frames))
        self.assertEqual(len(frames), (600 - 100 + 700) // FRAME_MS)

    def test_a_click_does_not_start_a_turn(self):
        got = events(Endpointer(), noise(500) + tone(40) + noise(1000, seed=1))
        self.assertEqual(got, [])

    def test_a_short_pause_does_not_end_the_turn(self):
        got = events(Endpointer(), noise(500) + tone(400) + noise(400, seed=1) + tone(400) + noise(1000, seed=2))
        self.assertEqual(sum(isinstance(e, Start) for e in got), 1)
        self.assertEqual(sum(isinstance(e, End) for e in got), 1)

    def test_the_noise_floor_follows_a_loud_room(self):
        room = -38.0
        got = events(Endpointer(), noise(2000, room) + tone(600, -15) + noise(1000, room, seed=1))
        self.assertEqual(sum(isinstance(e, Start) for e in got), 1, "room noise above the absolute floor is not speech")
        self.assertEqual(sum(isinstance(e, End) for e in got), 1)

    def test_a_long_turn_is_forced_to_end(self):
        got = events(Endpointer(Settings(max_turn_s=1.0)), noise(300) + tone(1500))
        self.assertEqual([(e.forced) for e in got if isinstance(e, End)], [True])


if __name__ == "__main__":
    unittest.main()
