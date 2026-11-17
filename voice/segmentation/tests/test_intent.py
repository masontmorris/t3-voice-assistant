"""The utterance intent, case by case. The first three are the measured demo's turns."""

import unittest

from voice.segmentation.contract import Intent, UtteranceIntent
from voice.segmentation.intent import asks_about_call, utterance_intent

REQUEST, CORRECTION, CHITCHAT = Intent.REQUEST, Intent.CORRECTION, Intent.CHITCHAT
CASES = [
    ("What's the weather in Denver tomorrow?", REQUEST, None),
    ("And what about Saturday?", REQUEST, None),
    ("That's not what I meant.", CORRECTION, None),
    ("That\N{RIGHT SINGLE QUOTATION MARK}s not what I meant.", CORRECTION, None),
    ("No, that's not what I asked for.", CORRECTION, None),
    ("That's wrong.", CORRECTION, None),
    ("You misunderstood me.", CORRECTION, None),
    ("No.", CORRECTION, None),
    ("No, no.", CORRECTION, None),
    ("Um, no, that's not right.", CORRECTION, None),
    ("No, Sunday.", CORRECTION, "Sunday"),
    ("No, I meant Sunday.", CORRECTION, "Sunday"),
    ("I meant Boulder.", CORRECTION, "Boulder"),
    ("I mean, Sunday instead.", CORRECTION, "Sunday"),
    ("Not Saturday, Sunday.", CORRECTION, "Sunday"),
    ("No, not Saturday, Sunday.", CORRECTION, "Sunday"),
    ("Not Boulder, Portland.", CORRECTION, "Portland"),
    ("Nope, not Boulder, Portland.", CORRECTION, "Portland"),
    ("Sunday, not Saturday.", CORRECTION, "Sunday"),
    ("No, Sunday, not Saturday.", CORRECTION, "Sunday"),
    ("Make it 8 pm.", CORRECTION, "8 pm"),
    ("Actually, Sunday.", CORRECTION, "Sunday"),
    ("That's not what I meant, I said Sunday.", CORRECTION, "Sunday"),
    ("No, what's the time in Tokyo?", REQUEST, None),
    ("Actually, what about Sunday?", REQUEST, None),
    ("No wait, set a timer for ten minutes.", REQUEST, None),
    ("Is it going to rain, not snow?", REQUEST, None),
    ("Not sure what to wear, what's the weather?", REQUEST, None),
    ("Thanks.", CHITCHAT, None),
    ("Okay, thank you so much!", CHITCHAT, None),
    ("No thanks.", CHITCHAT, None),
    ("Yes please.", CHITCHAT, None),
    ("Hey, hey, can you hear me?", CHITCHAT, None),
    ("Oh no, we're still on the dumb build.", CHITCHAT, None),
    ("Okay, this sucks.", CHITCHAT, None),
    ("Okay, this is frustrating.", CHITCHAT, None),
    ("What was my first message in this conversation?", CHITCHAT, None),
    ("Can you check the weather?", REQUEST, None),
    ("This is broken, please restart it.", REQUEST, None),
    ("This is frustrating, and what is the weather in Denver", REQUEST, None),
    ("What was the weather in Denver yesterday?", REQUEST, None),
    ("", CHITCHAT, None),
]


class UtteranceIntentTest(unittest.TestCase):
    def test_each_case(self):
        for said, intent, replacement in CASES:
            with self.subTest(said=said):
                self.assertEqual(utterance_intent(said), UtteranceIntent(intent, replacement))


class CallHistoryTest(unittest.TestCase):
    """A question about this call's own turns is conversation: it runs no task and reaches no tools session.

    The questions are the twelve-item paraphrase set's, which measured 7/12 with the note, and 8/12 once every one
    of them stopped being a request. The requests below refer to an earlier turn too, and must stay requests."""

    QUESTIONS = (
        "What did I ask first?",
        "What was the most recent thing I asked?",
        "Did I ask you about the weather in Denver?",
        "What was the first thing I asked you?",
        "What did I ask at the start of the call?",
        "What was the second thing I asked?",
        "What did you tell me about Lisbon?",
        "Did I ask about Tokyo?",
        "Going back to the beginning, what did I ask?",
        "What did I ask just before this?",
        "What was the second request I made?",
        "What did you say about Paris?",
        "What was my first message in this conversation?",
        "No, what did I ask first?",
    )
    REQUESTS = (
        "Do that again for Boston.",
        "Send that to Sam.",
        "And what about Saturday?",
        "What's the weather in Denver tomorrow?",
        "What was the weather in Denver yesterday?",
        "Tell me about Lisbon.",
        "I need the first train to Denver.",
        "Can you check the weather?",
    )

    def test_a_question_about_the_call_is_conversation(self):
        for said in self.QUESTIONS:
            with self.subTest(said=said):
                self.assertEqual(utterance_intent(said).intent, CHITCHAT)
                self.assertTrue(asks_about_call(said))

    def test_a_request_about_an_earlier_turn_is_still_a_request(self):
        for said in self.REQUESTS:
            with self.subTest(said=said):
                self.assertEqual(utterance_intent(said).intent, REQUEST)
                self.assertFalse(asks_about_call(said))


if __name__ == "__main__":
    unittest.main()
