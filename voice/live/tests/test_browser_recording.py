import base64
import json
import tempfile
import unittest
import wave
from pathlib import Path

import numpy as np

from voice.live.browser_recording import MAX_AHEAD_S, BrowserRecording, call_dir


def pcm(value, frames):
    return np.full(frames, value, dtype="<i2").tobytes()


def played(t, value, frames):
    return {"type": "played", "t": t, "pcm": base64.b64encode(pcm(value, frames)).decode()}


def read(path):
    with wave.open(str(path), "rb") as wav:
        return wav.getframerate(), np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2")


class CallDirTest(unittest.TestCase):
    def test_a_reconnect_in_the_same_run_gets_its_own_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual([call_dir(tmp).name, call_dir(tmp).name], ["call-001", "call-002"])


class BrowserRecordingTest(unittest.TestCase):
    def finish(self, tmp, call, last_turn=1):
        manifest_path = call.finish(last_turn)
        return json.loads(manifest_path.read_text()), Path(tmp)

    def test_played_audio_sits_where_the_page_stamped_it_on_the_microphone_stream(self):
        with tempfile.TemporaryDirectory() as tmp:
            call = BrowserRecording(tmp, mode="vad", first_turn=1)
            for _ in range(4):
                call.mic(pcm(1000, 8_000))
            call.played(played(1.0, 500, 8_000))
            call.hangup = True
            manifest, root = self.finish(tmp, call)
            rate, samples = read(root / "conversation.wav")
            self.assertEqual((rate, samples.size), (16_000, 32_000))
            self.assertTrue(np.all(samples[:16_000] == 1000))
            self.assertTrue(np.all(samples[16_000:24_000] == 1500), "the played run is mixed with the microphone")
            self.assertTrue(np.all(samples[24_000:] == 1000))
            self.assertEqual((manifest["input_source"], manifest["end"], manifest["partial"]),
                             ("browser", "hangup", False))
            self.assertEqual(manifest["turns"], {"first": 1, "last": 1})
            self.assertEqual(manifest["origin"]["monotonic_s"], 0.0)

    def test_an_unprompted_line_after_a_silent_microphone_is_heard_where_it_played(self):
        with tempfile.TemporaryDirectory() as tmp:
            call = BrowserRecording(tmp, mode="vad", first_turn=1)
            call.mic(pcm(0, 32_000))
            call.played(played(1.5, 700, 4_000))
            call.finish(1)
            _, samples = read(Path(tmp) / "conversation.wav")
            self.assertTrue(np.all(samples[:24_000] == 0))
            self.assertTrue(np.all(samples[24_000:28_000] == 700))
            self.assertTrue(np.all(samples[28_000:] == 0))

    def test_without_a_bye_the_recording_is_finalized_and_marked_partial(self):
        with tempfile.TemporaryDirectory() as tmp:
            call = BrowserRecording(tmp, mode="ptt", first_turn=4)
            call.mic(pcm(10, 1_600))
            manifest, root = self.finish(tmp, call, last_turn=5)
            self.assertEqual((manifest["end"], manifest["partial"], manifest["finalized"]),
                             ("connection_closed", True, True))
            self.assertEqual((manifest["mode"], manifest["turns"]), ("ptt", {"first": 4, "last": 5}))
            self.assertTrue((root / "conversation.wav").is_file())

    def test_a_malformed_or_impossible_report_is_dropped_and_counted(self):
        with tempfile.TemporaryDirectory() as tmp:
            call = BrowserRecording(tmp, mode="vad", first_turn=1)
            call.mic(pcm(0, 16_000))
            for message in ({"t": 0.5}, {"t": "soon", "pcm": ""}, {"t": 0.5, "pcm": "!!!"},
                            played(-1.0, 1, 10), played(1 + MAX_AHEAD_S + 1, 1, 10), played(float("nan"), 1, 10)):
                call.played(message)
            manifest, _ = self.finish(tmp, call)
            self.assertEqual(manifest["played_messages_rejected"], 6)
            self.assertEqual([track["kind"] for track in manifest["tracks"]], ["microphone"])


if __name__ == "__main__":
    unittest.main()
