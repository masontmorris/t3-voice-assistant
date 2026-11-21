"""The call transcript recorded on the thread a call was opened on."""

import unittest

from voice.live.transcript import CallTranscript


class CallTranscriptTest(unittest.TestCase):
    def test_each_side_as_it_went_out_with_checked_lines_and_interruptions_marked(self):
        call, first, second = CallTranscript(), object(), object()
        call.heard(1, " How is the clamp thread doing? ")
        call.said(first, 1, "The clamp thread", relay=True, acts=("thread_status",))
        call.said(first, 1, " is still running.", relay=True, acts=("thread_status",))
        call.spoken(("thread_status",), (140, 140, 141))
        call.heard(2, "   ")
        call.said(second, 2, "Anything else I can", relay=False)
        call.interrupted(2)
        self.assertEqual(call.wire(), [
            {"speaker": "caller", "text": "How is the clamp thread doing?", "relay": False},
            {"speaker": "voice", "text": "The clamp thread is still running.", "relay": True,
             "act": "thread_status", "sequences": [140, 141]},
            {"speaker": "voice", "text": "Anything else I can", "relay": False, "interrupted": True},
        ])

    def test_news_heard_for_other_acts_claims_nothing_and_an_interruption_never_marks_the_caller(self):
        call, reply = CallTranscript(), object()
        call.said(reply, 3, "It finished.", relay=True, acts=("done",))
        call.spoken(("thread_status",), (9,))
        call.heard(4, "Thanks")
        call.interrupted(4)
        self.assertEqual(call.wire(), [{"speaker": "voice", "text": "It finished.", "relay": True},
                                       {"speaker": "caller", "text": "Thanks", "relay": False}])
