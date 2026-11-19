import unittest

from voice.live.endpoint import FRAME_BYTES
from voice.live.overlap import Overlap, OverlapSettings, Playback, Verdict, classify, played_part

REPLY = "The forecast for Durham today shows a high of 57 degrees Fahrenheit and a 0 percent chance of precipitation."


class ClassifyTest(unittest.TestCase):
    def test_backchannels_leave_the_reply_playing(self):
        for text in ("Okay.", "Okay, thanks.", "Hmm,", "Mm-hm.", "mhm", "Uh-huh.", "Yeah, yeah.", "Right.",
                     "Oh, I see.", "Got it.", "Uhm,", "Thank you."):
            self.assertIs(classify(text, REPLY), Verdict.BACKCHANNEL, text)

    def test_words_aimed_at_the_assistant_stop_it(self):
        for text in ("Stop.", "Wait.", "No.", "Wait, do it again.", "Can you hear me?", "Answer the question.",
                     "Okay, and what about San Diego?", "Hold on.", "Yeah, but what about Raleigh?"):
            self.assertIs(classify(text, REPLY), Verdict.BARGE_IN, text)

    def test_the_reply_heard_back_through_speakers_is_echo(self):
        self.assertIs(classify("a high of 57 degrees", REPLY), Verdict.ECHO)
        self.assertIs(classify("chance of precipitation.", REPLY), Verdict.ECHO)

    def test_one_shared_word_is_not_echo(self):
        self.assertIs(classify("Durham?", REPLY), Verdict.BARGE_IN)

    def test_no_words_is_noise(self):
        for text in ("", "  ", "...", None):
            self.assertIs(classify(text, REPLY), Verdict.NOISE, repr(text))


class PlaybackTest(unittest.TestCase):
    def test_a_reply_is_audible_for_its_length(self):
        playback = Playback()
        playback.add(2.0, "first", now=10.0, turn=1)
        self.assertTrue(playback.playing(11.9, grace_s=0))
        self.assertFalse(playback.playing(12.1, grace_s=0))
        self.assertTrue(playback.playing(12.1, grace_s=0.25), "the client starts a little late")

    def test_a_task_update_queues_behind_the_reply(self):
        playback = Playback()
        playback.add(2.0, "first", now=10.0, turn=1)
        playback.add(3.0, "update", now=11.0, turn=1)
        self.assertEqual(playback.until, 15.0)
        self.assertEqual(playback.text, "first update")

    def test_a_reply_after_silence_starts_fresh(self):
        playback = Playback()
        playback.add(2.0, "first", now=10.0, turn=1)
        playback.add(1.0, "second", now=20.0, turn=2)
        self.assertEqual((playback.until, playback.text, playback.turn), (21.0, "second", 2))

    def test_a_barge_in_ends_playback(self):
        playback = Playback()
        playback.add(5.0, "long", now=10.0, turn=1)
        playback.stop(11.0)
        self.assertFalse(playback.playing(11.5, grace_s=0.25))


class PlayedPartTest(unittest.TestCase):
    def test_a_reply_that_is_still_playing_keeps_only_what_played(self):
        pcm = b"\x01\x00" * 16_000
        self.assertEqual(len(played_part(pcm, 16_000, 0.4)), 9_600 * 2, "0.6 of 1 s was played")
        self.assertEqual(played_part(pcm, 16_000, 0.0), pcm)
        self.assertEqual(played_part(pcm, 16_000, 3.0), b"", "nothing had played yet")


class OverlapTest(unittest.TestCase):
    def frames(self, overlap, n, level, noise=-75.0):
        return [overlap.feed(b"\x00" * FRAME_BYTES, level, noise) for _ in range(n)]

    def test_it_confirms_after_the_voiced_length(self):
        overlap = Overlap(OverlapSettings(confirm_ms=600), bytearray(), turn=1)
        got = self.frames(overlap, 30, level=-30.0)
        self.assertEqual(got.index(True), 29, "the thirtieth 20 ms frame reaches 600 ms")

    def test_quiet_speech_counts_below_the_endpointer_floor(self):
        """Call 2's quiet turns peak near -40 dBFS, and the endpointer's -50 dBFS floor drops most of their frames."""
        overlap = Overlap(OverlapSettings(), bytearray(), turn=1)
        self.frames(overlap, 10, level=-55.0)
        self.assertEqual(overlap.voiced_ms, 200)

    def test_silence_inside_the_overlap_does_not_count(self):
        overlap = Overlap(OverlapSettings(), bytearray(), turn=1)
        self.frames(overlap, 10, level=-70.0)
        self.assertEqual(overlap.voiced_ms, 0)
        self.assertEqual(len(overlap.pcm), 10 * FRAME_BYTES, "every frame is kept, voiced or not")


if __name__ == "__main__":
    unittest.main()


class TranscribeTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_session_that_never_opens_is_given_up_on(self):
        """Held sound waits on this transcript, so a hang before the audio is sent must not hold the reply forever."""
        import asyncio

        from voice.live.overlap import transcribe

        class Hanging:
            @classmethod
            async def open(cls, *args, **kwargs):
                await asyncio.Event().wait()

        with self.assertRaises(asyncio.TimeoutError):
            await transcribe("ws://unused", b"\0" * 320, Hanging, timeout_s=0.05)
