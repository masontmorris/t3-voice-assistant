"""What the --wav replay sends when a clip holds the turn before it.

A recorded turn's input can begin with the previous turn's audio, because the live server carries a cancelled turn's
audio into the turn that continues it. Replayed as recorded, those clips say the carried words twice and the
endpointer splits them at the pause they carry; the replay then reports more turns than the call had.
"""

import asyncio
import tempfile
import unittest
import wave
from pathlib import Path

import numpy as np

from voice.live import client

RATE = 16_000


def tone(freq, seconds=0.1, amplitude=0.35):
    t = np.arange(int(seconds * RATE)) / RATE
    return (amplitude * np.sin(2 * np.pi * freq * t) * 32767).astype("<i2").tobytes()


def write(path, pcm):
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(RATE)
        out.writeframes(pcm)
    return path


class WithoutCarryTest(unittest.TestCase):
    def test_a_clip_that_begins_with_the_previous_clip_sends_only_what_is_new(self):
        carried, new = tone(200), tone(700)
        self.assertEqual(client.without_carry(carried + new, carried), new)

    def test_a_clip_that_does_not_repeat_the_previous_one_is_sent_whole(self):
        previous, clip = tone(200), tone(700)
        self.assertEqual(client.without_carry(clip, previous), clip)

    def test_a_clip_identical_to_the_previous_one_is_still_sent(self):
        clip = tone(200)
        self.assertEqual(client.without_carry(clip, clip), clip)


class WavFramesTest(unittest.IsolatedAsyncioTestCase):
    async def test_the_carried_part_of_a_clip_never_reaches_the_server(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        carried, new, last = tone(200), tone(700), tone(1300)
        paths = [write(root / "turn_1.wav", carried),
                 write(root / "turn_2.wav", carried + new),
                 write(root / "turn_3.wav", last)]
        queue, next_clip = asyncio.Queue(), asyncio.Event()
        task = asyncio.create_task(client.wav_frames(paths, queue, next_clip, lead_s=0.0))
        frames = []
        try:
            for waits in (False, True, True):
                if waits:
                    next_clip.set()
                await asyncio.sleep(0.2)
                while not queue.empty():
                    frames.append(queue.get_nowait()[0])
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        sent = b"".join(frame for frame in frames if any(frame))
        heard, expected = (np.frombuffer(sent, dtype="<i2").astype(int),
                           np.frombuffer(carried + new + last, dtype="<i2").astype(int))
        self.assertEqual(len(heard), len(expected))
        self.assertLessEqual(int(np.max(np.abs(heard - expected))), 1)
