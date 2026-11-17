"""The fallback clip: rendered once, cached by its wording, and resampled to the session's rate."""

import io
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock

import numpy as np

from voice.live import fallback


def wav_bytes(rate, samples):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(np.asarray(samples, dtype="<i2").tobytes())
    return buffer.getvalue()


class CacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patched = mock.patch.object(fallback, "CACHE", Path(self.tmp.name))
        patched.start()
        self.addCleanup(patched.stop)

    def test_the_name_follows_the_wording(self):
        self.assertNotEqual(fallback.cache_path("one line"), fallback.cache_path("another line"))
        self.assertEqual(fallback.cache_path("one line").parent, Path(self.tmp.name))

    def test_a_cached_line_is_not_rendered_again(self):
        blob = wav_bytes(24_000, [0, 1, 2, 3])
        fallback.cache_path("one line").write_bytes(blob)
        with mock.patch.object(fallback.script, "render") as render:
            self.assertEqual(fallback.wav("http://unused", "one line"), blob)
        render.assert_not_called()

    def test_a_first_render_is_kept(self):
        blob = wav_bytes(24_000, [0, 1, 2, 3])
        with mock.patch.object(fallback.script, "voices", return_value=["female"]), \
                mock.patch.object(fallback.script, "render", return_value=("/tts", "female", blob)):
            self.assertEqual(fallback.wav("http://unused", "one line"), blob)
        self.assertEqual(fallback.cache_path("one line").read_bytes(), blob)

    def test_a_server_without_tts_is_an_os_error(self):
        with mock.patch.object(fallback.script, "voices", return_value=[]), \
                mock.patch.object(fallback.script, "render", side_effect=SystemExit("no TTS shape")):
            with self.assertRaises(OSError):
                fallback.wav("http://unused", "one line")

    def test_bare_pcm_is_refused(self):
        with mock.patch.object(fallback.script, "voices", return_value=["female"]), \
                mock.patch.object(fallback.script, "render", return_value=("/tts", "female", b"\x00\x01" * 800)):
            with self.assertRaises(OSError):
                fallback.wav("http://unused", "one line")


class PcmTest(unittest.TestCase):
    def test_a_clip_at_the_session_rate_passes_through(self):
        blob = wav_bytes(24_000, [0, 100, -100])
        self.assertEqual(fallback.pcm(blob, 24_000), np.asarray([0, 100, -100], dtype="<i2").tobytes())

    def test_a_clip_at_another_rate_is_resampled(self):
        blob = wav_bytes(16_000, [0] * 1600)
        converted = fallback.pcm(blob, 24_000)
        self.assertEqual(len(converted) // 2, 2400, "20 ms of 16 kHz becomes 20 ms of 24 kHz")
        self.assertEqual(len(fallback.pcm(blob, 8_000)) // 2, 800)

    def test_stereo_is_refused(self):
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as out:
            out.setnchannels(2)
            out.setsampwidth(2)
            out.setframerate(24_000)
            out.writeframes(b"\x00\x00" * 8)
        with self.assertRaises(ValueError):
            fallback.pcm(buffer.getvalue(), 24_000)


if __name__ == "__main__":
    unittest.main()
