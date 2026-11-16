"""The utterance intent, case by case. The first three are the measured demo's turns."""

import unittest

from voice.segmentation.contract import Intent, UtteranceIntent
from voice.segmentation.intent import utterance_intent

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


if __name__ == "__main__":
    unittest.main()
