import os
import unittest
from types import SimpleNamespace
from unittest import mock

from voice.prototype.run import close_with_decider, decider_arguments, segmenter_for
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


class DeciderArgumentsTest(unittest.TestCase):
    """The free cloud decider answers 400 to the place and day questions, so they are off for it by default."""

    def test_a_decider_without_text_questions_is_not_asked_them_unless_the_setting_says_so(self):
        cloud, liquid = SimpleNamespace(text_questions=False), SimpleNamespace()
        with mock.patch.dict(os.environ):
            os.environ.pop("VOICE_DECIDER_ARGS", None)
            self.assertEqual((decider_arguments(cloud, t3_enabled=True), decider_arguments(liquid, t3_enabled=True)), (False, True))
        with mock.patch.dict(os.environ, {"VOICE_DECIDER_ARGS": "on"}):
            self.assertTrue(decider_arguments(cloud, t3_enabled=True))
        with mock.patch.dict(os.environ, {"VOICE_DECIDER_ARGS": "off"}):
            self.assertFalse(decider_arguments(liquid, t3_enabled=True))

    def test_cloud_weather_with_t3_off_asks_for_place_and_day(self):
        from voice.segmentation.client import Client
        from voice.live.t3_tasks import T3Registry

        tools = SimpleNamespace(choices=lambda: {"weather": "Forecast", "none": "No tool"})
        for registry, expected in ((tools, True), (T3Registry(tools), False)):
            cloud = Client()
            with self.subTest(t3=not expected), mock.patch.dict(os.environ), \
                    mock.patch.object(cloud, "call", return_value={
                        "status": 400, "timing": {"ms": 0, "started_at": 0}, "response": {},
                    }) as ask:
                os.environ.pop("VOICE_DECIDER_ARGS", None)
                segmenter_for("hybrid", cloud, "weather", registry).update(
                    TranscriptUpdate(1, "What is the weather in Lisbon tomorrow?", True))
                names = ask.call_args.args[1]["questions"]
                self.assertEqual(any(name.endswith("_place") for name in names), expected)
                self.assertEqual(any(name.endswith("_when") for name in names), expected)

    def test_the_cloud_client_says_it_has_no_text_questions(self):
        from voice.segmentation.client import Client
        with mock.patch.dict(os.environ):
            os.environ.pop("VOICE_DECIDER_ARGS", None)
            self.assertFalse(decider_arguments(Client(), t3_enabled=True))


class LocalDeciderArguments(unittest.TestCase):
    def test_the_local_decider_is_never_asked_a_text_question(self):
        """llama.cpp refuses a request holding one, so nothing in the turn would be decided."""
        from voice.local_decider.client import D1Client
        with mock.patch.dict(os.environ, {"VOICE_DECIDER_ARGS": "on"}):
            self.assertFalse(decider_arguments(D1Client(), t3_enabled=False))
            self.assertFalse(decider_arguments(D1Client(), t3_enabled=True))
