import asyncio
import base64
import unittest

from voice.prototype.stagger import LANE, StaggeredAsr, StaggeredTranscript, lane_offsets_s, tail_after
from voice.prototype.transcript import COMPLETED, DELTA, TranscriptAdapter


def delta(lane, text):
    return {"type": DELTA, "item_id": f"item_{lane}", "delta": text, LANE: lane}


def completed(lane, text):
    return {"type": COMPLETED, "item_id": f"item_{lane}", "transcript": text, LANE: lane}


class LaneOffsetsTest(unittest.TestCase):
    def test_lanes_start_on_the_model_row_grid(self):
        self.assertEqual(lane_offsets_s(1), [0.0])
        self.assertEqual([round(x, 2) for x in lane_offsets_s(4)], [0.0, 0.24, 0.48, 0.72])

    def test_a_lane_count_that_leaves_the_row_grid_is_refused(self):
        with self.assertRaises(ValueError):
            lane_offsets_s(5)


class TailAfterTest(unittest.TestCase):
    def test_returns_the_words_the_base_does_not_have(self):
        tail, distance = tail_after("set a timer".split(), "Set a timer for ten".split())
        self.assertEqual((tail, distance), (["for", "ten"], 0))

    def test_a_different_first_word_costs_distance_but_keeps_the_tail(self):
        tail, distance = tail_after("et cetera timer".split(), "set a timer for".split())
        self.assertEqual(tail, ["for"])
        self.assertGreater(distance, 0)


class StaggeredTranscriptTest(unittest.TestCase):
    def test_a_shifted_lane_adds_words_a_single_session_would_wait_for(self):
        single, merged = TranscriptAdapter(), StaggeredTranscript(2)
        self.assertIsNotNone(single.feed({"type": DELTA, "item_id": "a", "delta": "Set a timer"}))
        merged.feed(delta(0, "Set a timer"))
        update = merged.feed(delta(1, "Set a timer for ten"))
        self.assertEqual(update.text, "Set a timer for ten")
        self.assertFalse(update.is_final)

    def test_a_partial_word_from_a_shifted_lane_is_replaced_not_kept(self):
        merged = StaggeredTranscript(2)
        merged.feed(delta(0, "Check the weather in"))
        merged.feed(delta(1, "Check the weather in Rale"))
        update = merged.feed(delta(1, "igh"))
        self.assertEqual(update.text, "Check the weather in Raleigh")

    def test_lane_zero_replaces_the_shifted_tail_at_its_next_block(self):
        merged = StaggeredTranscript(2)
        merged.feed(delta(0, "Check the weather"))
        merged.feed(delta(1, "Check the weather in Raleigh"))
        update = merged.feed(delta(0, " in Raleigh today"))
        self.assertEqual(update.text, "Check the weather in Raleigh today")

    def test_a_lane_that_disagrees_with_lane_zero_is_ignored(self):
        merged = StaggeredTranscript(2)
        merged.feed(delta(0, "Find last week's sales report and then email Dana"))
        self.assertIsNone(merged.feed(delta(1, "zzz qqq xxx")))
        self.assertEqual(merged.text(), "Find last week's sales report and then email Dana")

    def test_the_turn_ends_with_lane_zero_and_nothing_after_it_changes_the_text(self):
        merged = StaggeredTranscript(2)
        merged.feed(delta(0, "What time is it"))
        merged.feed(delta(1, "What time is it in Tokyo"))
        merged.end_input()
        merged.feed(completed(1, "What time is it in Tokyo?"))
        self.assertFalse(merged.final_sent)
        final = merged.feed(completed(0, "What time is it in Tokyo?"))
        self.assertEqual((final.text, final.is_final), ("What time is it in Tokyo?", True))
        self.assertIsNone(merged.feed(delta(1, " now")))
        self.assertEqual(merged.text(), "What time is it in Tokyo?")

    def test_revisions_count_up_over_every_emitted_update(self):
        merged = StaggeredTranscript(2)
        updates = [merged.feed(e) for e in (delta(0, "Hello"), delta(1, "Hello there"), delta(0, " there my friend"))]
        self.assertEqual([u.revision for u in updates], [1, 2, 3])


class RecordingLane:
    def __init__(self, name, on_event):
        self.name, self.on_event, self.events, self.sent = name, on_event, [], []

    @classmethod
    async def open(cls, url, name, clock, on_event=None):
        return cls(name, on_event)

    async def send(self, **event):
        self.sent.append(event)

    async def wait_for(self, *types, after=0, timeout=120):
        return {"type": types[0]}

    async def stream(self, pcm, rate):
        self.sent.append({"type": "stream", "bytes": len(pcm)})
        return 0.0, 1.0

    async def close(self):
        pass


def audio(seconds, rate=16_000):
    return base64.b64encode(bytes(round(seconds * rate) * 2)).decode()


class StaggeredAsrTest(unittest.IsolatedAsyncioTestCase):
    async def open(self, k):
        return await StaggeredAsr.open("ws://unused", "asr", lambda: 0.0, lambda event: None, k, session_cls=RecordingLane)

    async def test_a_shifted_lane_hears_silence_before_its_first_audio_and_lane_zero_does_not(self):
        asr = await self.open(2)
        await asr.send(type="input_audio_buffer.append", audio=audio(0.04))
        await asr.send(type="input_audio_buffer.append", audio=audio(0.04))
        lane0, lane1 = (lane.sent for lane in asr.lanes)
        self.assertEqual([len(base64.b64decode(e["audio"])) for e in lane0], [1280, 1280])
        self.assertEqual([len(base64.b64decode(e["audio"])) for e in lane1], [15360, 1280, 1280])

    async def test_the_silence_follows_the_rate_the_session_was_given(self):
        asr = await self.open(2)
        await asr.send(type="session.update", session={"audio": {"input": {"format": {"rate": 48_000}}}})
        await asr.send(type="input_audio_buffer.append", audio=audio(0.04, 48_000))
        self.assertEqual(len(base64.b64decode(asr.lanes[1].sent[1]["audio"])), 46080)

    async def test_commit_reaches_every_lane_and_the_call_records_lane_zero(self):
        asr = await self.open(4)
        await asr.send(type="input_audio_buffer.commit", final=True)
        self.assertTrue(all(lane.sent == [{"type": "input_audio_buffer.commit", "final": True}] for lane in asr.lanes))
        self.assertIs(asr.events, asr.lanes[0].events)
        self.assertEqual(len(asr.extra_events), 3)

    async def test_a_replayed_clip_primes_every_shifted_lane_before_it_streams(self):
        asr = await self.open(2)
        await asr.stream(bytes(3200), 16_000)
        self.assertEqual([e["type"] for e in asr.lanes[1].sent], ["input_audio_buffer.append", "stream"])
        self.assertEqual([e["type"] for e in asr.lanes[0].sent], ["stream"])


if __name__ == "__main__":
    unittest.main()
