import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from voice.live import client
from voice.live.aec import EchoCanceller


def pcm(value, frames):
    return np.full(frames, value, dtype="<i2").tobytes()


def make_args(tmp, **overrides):
    values = dict(server="ws://127.0.0.1:1", secret_file=Path(tmp) / "secret", output="none", input=None,
                  prebuffer_ms=0, wav=None, ptt=False, output_rate=24_000, wait_for_server=0,
                  barge_in_ms=None, log_dir=Path(tmp) / "logs", record=False, echo_cancel=None,
                  echo_tail_ms=256)
    values.update(overrides)
    return SimpleNamespace(**values)


class FakeCanceller:
    def __init__(self, raises=False):
        self.playback_calls, self.process_calls = [], []
        self.raises = raises

    def playback(self, data, rate, t):
        if self.raises:
            raise RuntimeError("playback broke")
        self.playback_calls.append((data, rate, t))

    def process(self, data, t):
        if self.raises:
            raise RuntimeError("process broke")
        self.process_calls.append((data, t))
        return b"c" * len(data)

    def stats(self):
        return {"frames": 1}


class EchoSelectionTest(unittest.TestCase):
    def test_the_default_is_on_for_a_microphone_and_off_for_wav(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsInstance(client.echo_canceller(make_args(tmp)), EchoCanceller)
            self.assertIsNone(client.echo_canceller(make_args(tmp, wav=[Path(tmp) / "clip.wav"])))

    def test_no_echo_cancel_turns_it_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(client.echo_canceller(make_args(tmp, echo_cancel=False)))

    def test_an_explicit_echo_cancel_turns_it_on_for_wav(self):
        with tempfile.TemporaryDirectory() as tmp:
            canceller = client.echo_canceller(make_args(tmp, wav=[Path(tmp) / "clip.wav"], echo_cancel=True))
            self.assertIsInstance(canceller, EchoCanceller)

    def test_the_tail_length_is_passed(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(client.echo_canceller(make_args(tmp, echo_tail_ms=400)).tail_ms, 400)


class ClientWiringTest(unittest.TestCase):
    def test_the_client_gives_the_canceller_to_the_player_and_the_microphone(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = client.Client(make_args(tmp))
            try:
                self.assertIsInstance(run.echo, EchoCanceller)
                self.assertIs(run.player.canceller, run.echo)
            finally:
                run.player.close()
                run.log_file.close()

    def test_no_echo_cancel_leaves_the_player_without_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = client.Client(make_args(tmp, echo_cancel=False))
            try:
                self.assertIsNone(run.echo)
                self.assertIsNone(run.player.canceller)
            finally:
                run.player.close()
                run.log_file.close()


class PlayerTest(unittest.TestCase):
    def make_player(self, canceller):
        player = client.Player(None, 0, canceller=canceller)
        player.rate = 24_000
        return player

    def test_only_audio_handed_to_the_device_is_fed_to_the_canceller(self):
        canceller = FakeCanceller()
        player = self.make_player(canceller)
        block = pcm(1, 480)
        player.buffer += block
        player.playing, player.reply_done = True, False

        player._callback(bytearray(960), 480, None, None)
        player._callback(bytearray(960), 480, None, None)

        self.assertEqual([call[0] for call in canceller.playback_calls], [block])
        self.assertEqual(canceller.playback_calls[0][1], 24_000)
        self.assertIsInstance(canceller.playback_calls[0][2], float)

    def test_a_canceller_failure_does_not_escape_the_audio_callback(self):
        canceller = FakeCanceller(raises=True)
        player = self.make_player(canceller)
        player.buffer += pcm(1, 480)
        player.playing, player.reply_done = True, False

        player._callback(bytearray(960), 480, None, None)

        self.assertIsNone(player.canceller)
        self.assertEqual(len(canceller.playback_calls), 0)


class SendAudioTest(unittest.IsolatedAsyncioTestCase):
    class FakeWebSocket:
        def __init__(self):
            self.sent = []

        async def send(self, data):
            self.sent.append(data)

    async def run_one_frame(self, owner, frame, t=123.5):
        queue = asyncio.Queue()
        queue.put_nowait((frame, t))
        ws = self.FakeWebSocket()
        task = asyncio.create_task(client.Client.send_audio(owner, ws, queue))
        for _ in range(20):
            await asyncio.sleep(0)
            if ws.sent:
                break
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return ws.sent

    async def test_the_cleaned_frame_is_sent_and_recorded(self):
        recorder = SimpleNamespace(mic_calls=[], mic=lambda data, t=None: recorder.mic_calls.append((data, t)))
        owner = SimpleNamespace(echo=FakeCanceller(), recorder=recorder,
                                log=lambda *args, **kwargs: None, log_file=None)
        sent = await self.run_one_frame(owner, pcm(5, 320))
        self.assertEqual(sent, [b"c" * 640])
        self.assertEqual(recorder.mic_calls, [(b"c" * 640, 123.5)])

    async def test_a_canceller_failure_sends_the_raw_microphone_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = client.Client(make_args(tmp))
            try:
                broken = FakeCanceller(raises=True)
                run.echo, run.player.canceller = broken, broken
                frame = pcm(7, 320)
                sent = await self.run_one_frame(run, frame)
                self.assertEqual(sent, [frame])
                self.assertIsNone(run.echo)
                events = [json.loads(line) for line in (run.log_dir / "client.jsonl").read_text().splitlines()]
                self.assertTrue(any(event["kind"] == "echo_failed" for event in events))
            finally:
                run.player.close()
                run.log_file.close()


if __name__ == "__main__":
    unittest.main()
