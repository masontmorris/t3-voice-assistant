import unittest

from voice.prototype.transcript import COMPLETED, DELTA, TranscriptAdapter


def delta(text, item="item_1"):
    return {"type": DELTA, "item_id": item, "delta": text}


def completed(text, item="item_1"):
    return {"type": COMPLETED, "item_id": item, "transcript": text}


class TranscriptAdapterTest(unittest.TestCase):
    def test_each_delta_gives_the_whole_utterance_with_a_new_revision(self):
        adapter = TranscriptAdapter()
        updates = [adapter.feed(delta(t)) for t in ("Set", " a timer", " for 10 minutes.", " No wait.")]
        self.assertEqual([(u.revision, u.text, u.is_final) for u in updates], [
            (1, "Set", False), (2, "Set a timer", False), (3, "Set a timer for 10 minutes.", False),
            (4, "Set a timer for 10 minutes. No wait.", False)])

    def test_events_without_new_text_give_no_update(self):
        adapter = TranscriptAdapter()
        self.assertIsNone(adapter.feed(delta("")))
        self.assertIsNone(adapter.feed({"type": "input_audio_buffer.committed", "item_id": "item_1"}))
        self.assertEqual(adapter.revision, 0)

    def test_the_turn_is_final_only_when_the_input_has_ended(self):
        adapter = TranscriptAdapter()
        adapter.feed(delta("What time is it"))
        self.assertFalse(adapter.feed(completed("What time is it")).is_final)
        adapter.feed(delta(" in Tokyo?", "item_2"))
        adapter.end_input()
        final = adapter.feed(completed("in Tokyo?", "item_2"))
        self.assertEqual((final.text, final.is_final), ("What time is it in Tokyo?", True))

    def test_the_completed_text_replaces_the_item_partials(self):
        adapter = TranscriptAdapter()
        adapter.feed(delta("make that fifteen"))
        adapter.end_input()
        self.assertEqual(adapter.feed(completed("Make that 15 minutes.")).text, "Make that 15 minutes.")

    def test_nothing_follows_the_final_update(self):
        adapter = TranscriptAdapter()
        adapter.end_input()
        adapter.feed(completed("Done."))
        self.assertIsNone(adapter.feed(delta(" more")))
        self.assertIsNone(adapter.finish())

    def test_finish_ends_a_turn_whose_item_never_completed(self):
        adapter = TranscriptAdapter()
        adapter.feed(delta("Add milk"))
        adapter.end_input()
        final = adapter.finish()
        self.assertEqual((final.revision, final.text, final.is_final), (2, "Add milk", True))


if __name__ == "__main__":
    unittest.main()
