import unittest

from voice.prototype.segmenter import coalesce
from voice.segmentation.contract import TranscriptUpdate


def u(revision, final=False):
    return ("update", TranscriptUpdate(revision, f"text {revision}", final))


class Coalesce(unittest.TestCase):
    def test_only_the_newest_queued_update_is_processed(self):
        kept, dropped = coalesce([u(3), u(4), u(5, final=True)])
        self.assertEqual([p.revision for _, p in kept], [5])
        self.assertEqual([p.revision for p in dropped], [3, 4])

    def test_a_delegate_keeps_its_place_and_the_update_before_it(self):
        items = [u(3), u(4), ("delegate", "find the report"), u(5)]
        kept, dropped = coalesce(items)
        self.assertEqual(kept, [items[1], items[2], items[3]])
        self.assertEqual([p.revision for p in dropped], [3])

    def test_a_lone_update_is_kept(self):
        self.assertEqual(coalesce([u(1)]), ([u(1)], []))


if __name__ == "__main__":
    unittest.main()
