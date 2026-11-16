"""Which turn of the call a question names, and the one sentence the live server states before the reply.

The failure these tests stand in for: asked "What was the first thing I asked you?", the server named the call's most
recent request. The note is chosen here in code from the person's own turns, so these tests fix what that choice is.
They say nothing about whether the model then uses it; that is a live run's job.
"""

import unittest

from voice.live import recall

CALL = ("Hey, can you hear me?",
        "What time is it in Tokyo right now?",
        "What's the weather in Denver tomorrow?",
        "What time is it in Berlin?")


class QuestionKindTest(unittest.TestCase):
    def test_the_ways_of_asking_for_the_first_turn_all_read_as_first(self):
        for question in ("What did I ask first?", "What was the first thing I asked you?",
                         "Going back to the beginning, what did I ask?",
                         "What did I ask at the start of the call?"):
            with self.subTest(question=question):
                self.assertEqual(recall.question_kind(question), ("first",))

    def test_the_ways_of_asking_for_the_previous_turn_all_read_as_last(self):
        for question in ("What did I ask just before this?", "What was the last thing I asked you?"):
            with self.subTest(question=question):
                self.assertEqual(recall.question_kind(question), ("last",))

    def test_an_ordinal_names_that_request(self):
        self.assertEqual(recall.question_kind("What was the second thing I asked?"), ("request_number", 2))

    def test_a_question_about_a_topic_reads_as_that_topic(self):
        self.assertEqual(recall.question_kind("Did I ask you about the weather in Denver?"),
                         ("topic", "the weather in Denver", ("weather", "denver")))

    def test_a_request_that_is_not_about_the_call_reads_as_nothing(self):
        for question in ("Tell me about Lisbon.", "What's the weather in Denver tomorrow?",
                         "That is not what I meant.", "Search my email for the report.",
                         "I need the first train to Denver."):
            with self.subTest(question=question):
                self.assertIsNone(recall.question_kind(question))


class RequestTurnsTest(unittest.TestCase):
    def test_a_greeting_and_a_correction_are_not_requests(self):
        self.assertEqual(recall.request_turns(CALL), [2, 3, 4])
        self.assertEqual(recall.request_turns(("Hey, can you hear me?", "That is not what I meant.")), [])


class RecallNoteTest(unittest.TestCase):
    def test_the_first_request_is_named_with_its_turn_number_and_the_persons_words(self):
        self.assertEqual(
            recall.recall_note("What was the first thing I asked you?", CALL),
            recall.PREFIX + 'first request of this call, turn 2 of 4: "What time is it in Tokyo right now?"')

    def test_the_most_recent_request_is_not_the_first(self):
        note = recall.recall_note("What did I ask just before this?", CALL)
        self.assertIn("turn 4 of 4", note)
        self.assertIn("Berlin", note)
        self.assertNotIn("Tokyo", note)

    def test_the_second_request_is_the_middle_one(self):
        self.assertIn('turn 3 of 4: "What\'s the weather in Denver tomorrow?"',
                      recall.recall_note("What was the second thing I asked?", CALL))

    def test_a_topic_names_the_most_recent_turn_that_says_it(self):
        self.assertIn('turn 3 of 4: "What\'s the weather in Denver tomorrow?"',
                      recall.recall_note("Did I ask you about the weather in Denver?", CALL))

    def test_a_topic_the_call_never_said_says_so_and_lists_the_turns(self):
        note = recall.recall_note("Did I ask about Saturday?", CALL)
        self.assertIn("nothing in this call mentions Saturday", note)
        self.assertIn("turn 2", note)
        self.assertIn("Berlin", note)

    def test_a_request_the_call_never_made_is_named_as_nothing(self):
        self.assertIsNone(recall.recall_note("What was the first thing I asked?", ("Hey, can you hear me?",)))

    def test_a_question_about_something_else_gets_no_note(self):
        self.assertIsNone(recall.recall_note("What's the weather in Denver tomorrow?", CALL))

    def test_the_first_turn_of_a_call_gets_no_note(self):
        self.assertIsNone(recall.recall_note("What did I ask first?", ()))


class CallRecordTest(unittest.TestCase):
    def test_every_turn_is_numbered_oldest_first_in_plain_words(self):
        self.assertEqual(
            recall.call_record(("Hey, can you hear me?", "What time is it in Tokyo?")),
            recall.PREFIX + 'earlier turns of this call, oldest first: turn 1: "Hey, can you hear me?"; '
                            'turn 2: "What time is it in Tokyo?"')
