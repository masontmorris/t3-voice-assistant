import json
import queue
import tempfile
import unittest
import wave
from datetime import datetime
from pathlib import Path
from unittest import mock

import numpy as np

from voice.live import recording


def pcm(value, frames):
    return np.full(frames, value, dtype="<i2").tobytes()


def read(path):
    with wave.open(str(path), "rb") as wav:
        return wav.getframerate(), np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2")


class SessionDirTest(unittest.TestCase):
    def test_two_runs_in_the_same_second_get_separate_folders(self):
        with tempfile.TemporaryDirectory() as tmp:
            when = datetime(2026, 11, 16, 7, 30, 0)
            first = recording.unique_session_dir(tmp, when)
            second = recording.unique_session_dir(tmp, when)
            self.assertNotEqual(first, second)
            self.assertEqual(first.name, "20261116-073000")
            self.assertEqual(second.name, "20261116-073000-01")
            self.assertTrue(first.is_dir() and second.is_dir())


class TimingTest(unittest.TestCase):
    def test_microphone_and_playback_land_on_one_clock(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorder = recording.SessionRecorder(tmp)
            start = 1000.0
            recorder.mic(pcm(1000, 16_000), t=start)
            recorder.playback(pcm(2000, 24_000), 24_000, t=start + 2.0)
            recorder.finalize()

            manifest = json.loads((Path(tmp) / "manifest.json").read_text())
            self.assertEqual(manifest["origin"]["monotonic_s"], start)
            self.assertEqual([track["rate"] for track in manifest["tracks"]], [16_000, 24_000])
            self.assertEqual(manifest["tracks"][1]["offset_s"], 2.0)

            rate, samples = read(Path(tmp) / "conversation.wav")
            self.assertEqual(rate, 16_000)
            self.assertEqual(samples.size, 3 * 16_000, "mic, the pause, then the reply")
            self.assertTrue(np.all(samples[:16_000] == 1000), "mic audio is at the origin")
            self.assertTrue(np.all(samples[16_000:32_000] == 0), "the pause is silent")
            self.assertTrue(np.all(samples[32_000:] == 2000), "the reply starts 2 s in")

    def test_a_pause_inside_playback_stays_a_pause(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorder = recording.SessionRecorder(tmp)
            start = 50.0
            recorder.mic(pcm(50, 16), t=start)
            recorder.playback(pcm(700, 4_800), 24_000, t=start + 1.0)
            recorder.playback(pcm(900, 2_400), 24_000, t=start + 3.0)
            recorder.finalize()

            manifest = json.loads((Path(tmp) / "manifest.json").read_text())
            track = [track for track in manifest["tracks"] if track["kind"] == "assistant_playback"][0]
            self.assertEqual(track["offset_s"], 1.0)
            self.assertAlmostEqual(track["duration_s"], 2.1, places=3)
            rate, samples = read(Path(tmp) / "conversation.wav")
            self.assertEqual(rate, 16_000)
            self.assertTrue(np.all(samples[16_000:19_200] == 700))
            self.assertTrue(np.all(samples[19_200:48_000] == 0), "the unplayed pause is silent")
            self.assertTrue(np.all(samples[48_000:49_600] == 900), "the second reply keeps its time")

    def test_each_playback_rate_gets_its_own_track(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorder = recording.SessionRecorder(tmp)
            start = 10.0
            recorder.mic(pcm(30, 16), t=start)
            recorder.playback(pcm(300, 24_000), 24_000, t=start + 1.0)
            recorder.playback(pcm(600, 48_000), 48_000, t=start + 2.0)
            recorder.finalize()

            manifest = json.loads((Path(tmp) / "manifest.json").read_text())
            playback = [track for track in manifest["tracks"] if track["kind"] == "assistant_playback"]
            self.assertEqual([track["rate"] for track in playback], [24_000, 48_000])
            self.assertEqual([track["file"] for track in playback],
                             ["assistant-playback-001.wav", "assistant-playback-002.wav"])
            rate, samples = read(Path(tmp) / "conversation.wav")
            self.assertEqual(rate, 16_000)
            self.assertTrue(np.all(samples[16_000:32_000] == 300))
            self.assertTrue(np.all(samples[32_000:48_000] == 600), "the 48 kHz epoch is resampled to 16 kHz")


class FinalizeTest(unittest.TestCase):
    def test_a_partial_run_still_gets_a_finalized_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorder = recording.SessionRecorder(tmp, mode="ptt", input_source="wav", server="ws://unused")
            recorder.mic(pcm(400, 3_200), t=5.0)
            first = recorder.finalize()
            second = recorder.finalize()
            self.assertEqual(first, second)
            manifest = json.loads(first.read_text())
            self.assertTrue(manifest["finalized"])
            self.assertIsNone(manifest["finalize_error"])
            self.assertEqual(manifest["mode"], "ptt")
            self.assertEqual(manifest["input_source"], "wav")
            self.assertIn("simulated input", manifest["notes"])
            self.assertEqual(manifest["tracks"][0]["frames"], 3_200)
            rate, samples = read(Path(tmp) / "conversation.wav")
            self.assertEqual((rate, samples.size), (16_000, 3_200))

    def test_a_run_with_no_audio_writes_only_the_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorder = recording.SessionRecorder(tmp)
            manifest = json.loads(recorder.finalize().read_text())
            self.assertTrue(manifest["finalized"])
            self.assertEqual(manifest["tracks"], [])
            self.assertIsNone(manifest["conversation"])
            self.assertEqual(sorted(path.name for path in Path(tmp).iterdir()), ["manifest.json"])

    def test_dropped_audio_is_counted(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorder = recording.SessionRecorder(tmp, max_queue=1)
            with mock.patch.object(recorder._queue, "put_nowait", side_effect=queue.Full):
                recorder.mic(pcm(100, 320), t=1.0)
                recorder.playback(pcm(200, 480), 24_000, t=1.1)
            manifest = json.loads(recorder.finalize().read_text())
            self.assertEqual(manifest["dropped"]["mic_chunks"], 1)
            self.assertEqual(manifest["dropped"]["mic_frames"], 320)
            self.assertEqual(manifest["dropped"]["playback_chunks"], 1)
            self.assertEqual(manifest["dropped"]["playback_frames"], 480)


if __name__ == "__main__":
    unittest.main()
