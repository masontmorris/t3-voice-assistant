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

    def test_a_correction_is_kept_as_its_own_checked_line_after_the_reply_it_cut_off(self):
        call, wrong, held, correction = CallTranscript(), object(), object(), object()
        call.heard(5, "Did you start the clamp thread?")
        call.said(wrong, 5, "Yes, I started it", relay=False)
        call.interrupted(5)
        call.said(held, 5, "Sorry, let me check that.", relay=False)
        call.said(correction, 5, "Actually, nothing was started on this call.", relay=True, acts=("nothing_done",))
        call.spoken(("nothing_done",), ())
        self.assertEqual(call.wire()[1:], [
            {"speaker": "voice", "text": "Yes, I started it", "relay": False, "interrupted": True},
            {"speaker": "voice", "text": "Sorry, let me check that.", "relay": False},
            {"speaker": "voice", "text": "Actually, nothing was started on this call.", "relay": True,
             "act": "nothing_done", "sequences": []},
        ])
