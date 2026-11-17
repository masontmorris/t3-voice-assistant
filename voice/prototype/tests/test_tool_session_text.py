"""The tool session's text is the last screen between conversation and a task.

The intent gate (`voice.segmentation.intent`) skips the tool session for an utterance it classifies as chit-chat or a
correction. Two of the six non-requests measured for PR #26 still reach it ("You're dumb." and "What do you
understand?"), so on those turns the text is the only thing that stops a task. These tests pin the clauses that do it.
The text is frozen against LFM3's measured calls, so an edit to it needs the measured set re-run before it ships.
"""

import unittest

from voice.prototype.run import GATED_INSTRUCTIONS, HANDOFF_TOOLS


class ToolSessionTextTest(unittest.TestCase):
    def test_the_gated_text_names_an_insult_and_a_question_about_what_it_heard_as_no_task(self):
        self.assertIn("insults", GATED_INSTRUCTIONS)
        self.assertIn("questions about you or this conversation or what you heard", GATED_INSTRUCTIONS)

    def test_the_delegate_description_excludes_conversation(self):
        delegate, = [tool for tool in HANDOFF_TOOLS if tool["name"] == "delegate"]
        for phrase in ("an insult", "a reaction", "thanks", "a question about you or this conversation"):
            self.assertIn(phrase, delegate["description"])


if __name__ == "__main__":
    unittest.main()
