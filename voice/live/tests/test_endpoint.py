import unittest

import numpy as np

from voice.live.endpoint import FRAME_BYTES, FRAME_MS, RATE, Audio, Check, End, Endpointer, Settings, Start


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


class HoldTest(unittest.TestCase):
    def run_hold(self, settings, pcm, hold_at=500, finish_at=None):
        endpointer, seen = Endpointer(settings), []
        for offset in range(0, len(pcm), FRAME_BYTES):
            for event in endpointer.push(pcm[offset:offset + FRAME_BYTES]):
                seen.append(event)
                if isinstance(event, Check):
                    if event.silent_ms == hold_at:
                        endpointer.hold()
                    if event.silent_ms == finish_at:
                        endpointer.finish(event.silent_ms)
        return seen

    def test_a_held_turn_waits_for_the_hold_cap_not_the_silence_rule(self):
        got = self.run_hold(Settings(check_ms=(500,), hold_ms=1500), noise(300) + tone(600) + noise(2000, seed=1))
        self.assertEqual([e.trailing_ms for e in got if isinstance(e, End)], [1500])

    def test_checks_past_the_silence_rule_reach_a_held_turn_and_can_finish_it(self):
        got = self.run_hold(Settings(check_ms=(500, 1000), hold_ms=1500), noise(300) + tone(600) + noise(2000, seed=1),
                            finish_at=1000)
        self.assertEqual([e.silent_ms for e in got if isinstance(e, Check)], [500, 1000])
        self.assertEqual([e for e in got if isinstance(e, End)], [], "finish owes no End")

    def test_without_a_hold_cap_hold_does_nothing(self):
        got = self.run_hold(Settings(check_ms=(500,)), noise(300) + tone(600) + noise(2000, seed=1))
        self.assertEqual([e.trailing_ms for e in got if isinstance(e, End)], [700])

    def test_a_turn_nobody_held_ends_at_the_silence_rule_and_asks_nothing_past_it(self):
        got = self.run_hold(Settings(check_ms=(500, 1000), hold_ms=1500), noise(300) + tone(600) + noise(2000, seed=1),
                            hold_at=None)
        self.assertEqual([e.silent_ms for e in got if isinstance(e, Check)], [500])
        self.assertEqual([e.trailing_ms for e in got if isinstance(e, End)], [700])

    def test_speech_clears_the_hold(self):
        got = self.run_hold(Settings(check_ms=(500,), hold_ms=1500),
                            noise(300) + tone(600) + noise(900, seed=1) + tone(300) + noise(2000, seed=2))
        self.assertEqual(sum(isinstance(e, Start) for e in got), 1, "the held pause did not split the turn")
        self.assertEqual([e.trailing_ms for e in got if isinstance(e, End)], [1500], "the next pause is held on its own check")

    def test_the_hold_does_not_outlast_the_max_turn(self):
        got = self.run_hold(Settings(check_ms=(500,), hold_ms=5000, max_turn_s=1.5),
                            noise(300) + tone(600) + noise(3000, seed=1))
        self.assertEqual([e.forced for e in got if isinstance(e, End)], [True])


if __name__ == "__main__":
    unittest.main()
