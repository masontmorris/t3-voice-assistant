import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np

from voice.live import client


def pcm(value, frames):
    return np.full(frames, value, dtype="<i2").tobytes()


class FakeRecorder:
    def __init__(self):
        self.mic_calls, self.playback_calls = [], []

    def mic(self, data, t=None):
        self.mic_calls.append((data, t))

    def playback(self, data, rate, t=None):
        self.playback_calls.append((data, rate, t))


def make_player(recorder=None, rate=24_000):
    player = client.Player(None, 0, recorder=recorder)
    player.rate = rate
    return player


class PlayerRecordingTest(unittest.TestCase):
    def test_only_audio_handed_to_the_device_is_recorded(self):
        recorder = FakeRecorder()
        player = make_player(recorder)
        block = pcm(1, 480)
        player.buffer += block
        player.playing, player.reply_done = True, False

        first = bytearray(960)
        player._callback(first, 480, None, None)
        self.assertEqual(first, block)
        self.assertEqual([call[0] for call in recorder.playback_calls], [block])
        self.assertEqual(recorder.playback_calls[0][1], 24_000)

        second = bytearray(960)
        player._callback(second, 480, None, None)
        self.assertEqual(second, b"\x00" * 960)
        self.assertEqual(len(recorder.playback_calls), 1, "an idle callback records no silence")

    def test_a_barge_in_flush_does_not_record_the_dropped_tail(self):
        recorder = FakeRecorder()
        player = make_player(recorder)
        first, tail = pcm(2, 480), pcm(3, 480)
        player.buffer += first + tail
        player.playing, player.reply_done = True, False

        player._callback(bytearray(960), 480, None, None)
        player.flush()
        player._callback(bytearray(960), 480, None, None)

        self.assertEqual([call[0] for call in recorder.playback_calls], [first],
                         "the played block is kept and the buffered tail is not")

    def test_a_short_last_block_records_the_audio_without_the_padding(self):
        recorder = FakeRecorder()
        player = make_player(recorder)
        block = pcm(4, 200)
        player.buffer += block
        player.playing, player.reply_done = True, True

        out = bytearray(960)
        player._callback(out, 480, None, None)
        self.assertEqual(out, block + b"\x00" * 560)
        self.assertEqual(recorder.playback_calls[0][0], block)


class SendAudioTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_sent_frame_is_recorded_with_its_capture_time(self):
        recorder = FakeRecorder()
        owner = SimpleNamespace(recorder=recorder)
        queue = asyncio.Queue()

        class FakeWebSocket:
            def __init__(self):
                self.sent = []

            async def send(self, data):
                self.sent.append(data)

        ws = FakeWebSocket()
        frame = pcm(5, 320)
        queue.put_nowait((frame, 123.5))
        task = asyncio.create_task(client.Client.send_audio(owner, ws, queue))
        for _ in range(10):
            await asyncio.sleep(0)
            if recorder.mic_calls:
                break
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        self.assertEqual(ws.sent, [frame])
        self.assertEqual(recorder.mic_calls, [(frame, 123.5)])

    async def test_a_wav_clip_enters_the_same_timestamped_path(self):
        queue = asyncio.Queue()
        clip = pcm(6, 8000)
        with mock.patch.object(client, "read_wav_16k", lambda _path: clip):
            task = asyncio.create_task(client.wav_frames([Path("clip.wav")], queue, asyncio.Event()))
            frame, stamp = await asyncio.wait_for(queue.get(), 1)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(len(frame), client.FRAME_SAMPLES * 2)
        self.assertIsInstance(stamp, float)


def make_args(tmp, **overrides):
    values = dict(server="ws://127.0.0.1:1", secret_file=Path(tmp) / "secret", output="0", input=None,
                  prebuffer_ms=80, wav=None, ptt=False, output_rate=24_000, wait_for_server=0,
                  barge_in_ms=None, log_dir=Path(tmp) / "logs", record=True)
    values.update(overrides)
    Path(values["secret_file"]).write_text("s" * 64, encoding="utf-8")
    return SimpleNamespace(**values)


def session_manifest(tmp):
    sessions = list((Path(tmp) / "logs").iterdir())
    assert len(sessions) == 1, sessions
    return json.loads((sessions[0] / "manifest.json").read_text())


class ClientFinalizeTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_connection_error_keeps_the_original_error_and_finalizes(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = make_args(tmp)

            async def refuse(self, _secret):
                raise OSError("no server")

            with mock.patch.object(client.Player, "open", lambda _self, _rate: None), \
                    mock.patch.object(client.Client, "connect", refuse):
                with self.assertRaisesRegex(OSError, "no server"):
                    await client.Client(args).run()
            manifest = session_manifest(tmp)
            self.assertTrue(manifest["finalized"])
            self.assertEqual(manifest["tracks"], [])

    async def test_a_cancelled_run_finalizes_the_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = make_args(tmp)

            async def wait_forever(self, _secret):
                await asyncio.Event().wait()

            with mock.patch.object(client.Player, "open", lambda _self, _rate: None), \
                    mock.patch.object(client.Client, "connect", wait_forever):
                task = asyncio.create_task(client.Client(args).run())
                await asyncio.sleep(0.05)
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            manifest = session_manifest(tmp)
            self.assertTrue(manifest["finalized"])

    async def test_wav_input_and_ptt_are_named_in_the_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = make_args(tmp, wav=[Path(tmp) / "clip.wav"], ptt=True)
            run = client.Client(args)
            try:
                self.assertEqual(run.recorder.input_source, "wav")
                self.assertEqual(run.recorder.mode, "ptt")
            finally:
                run.finalize_recording()
                run.log_file.close()
            manifest = session_manifest(tmp)
            self.assertEqual(manifest["input_source"], "wav")
            self.assertEqual(manifest["mode"], "ptt")

    async def test_no_record_skips_the_recorder(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = make_args(tmp, record=False)
            run = client.Client(args)
            try:
                self.assertIsNone(run.recorder)
            finally:
                run.log_file.close()
            session = list((Path(tmp) / "logs").iterdir())[0]
            self.assertTrue(session.is_dir())
            self.assertFalse((session / "manifest.json").exists())


if __name__ == "__main__":
    unittest.main()
