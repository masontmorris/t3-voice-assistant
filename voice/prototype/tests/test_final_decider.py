import unittest

from voice.prototype.run import close_with_decider
from voice.segmentation.contract import TranscriptUpdate


class Segmenter:
    def __init__(self, fails=False):
        self.calls = []
        self.fails = fails

    def update(self, update):
        self.calls.append("decide")
        if self.fails:
            raise RuntimeError("unavailable")
        return {"decisions": {"s1": "request"}}

    def close(self, update, settle):
        self.calls.append("close")
        return {"kind": "close", "unreviewed_speech": update.text}


class FinalDeciderTest(unittest.TestCase):
    def test_final_transcript_is_decided_before_close(self):
        segmenter = Segmenter()
        result = close_with_decider(segmenter, TranscriptUpdate(1, "What is the weather?", True))
        self.assertEqual(segmenter.calls, ["decide", "close"])
        self.assertIsNotNone(result["final_decision"])

    def test_failure_closes_and_reports_error(self):
        segmenter = Segmenter(fails=True)
        result = close_with_decider(segmenter, TranscriptUpdate(1, "What is the weather?", True))
        self.assertEqual(segmenter.calls, ["decide", "close"])
        self.assertIn("unavailable", result["final_decision_error"])

    def test_correction_does_not_dispatch_as_new_work(self):
        segmenter = Segmenter()
        close_with_decider(segmenter, TranscriptUpdate(1, "That's not what I meant.", True))
        self.assertEqual(segmenter.calls, ["close"])

    def test_conversation_recall_does_not_dispatch_as_new_work(self):
        segmenter = Segmenter()
        close_with_decider(segmenter, TranscriptUpdate(1, "What was my first message in this conversation?", True))
        self.assertEqual(segmenter.calls, ["close"])
