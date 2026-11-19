import unittest

from voice.live.facts import names_machinery, speaks_of_person, ungrounded

# The three turns of the demo call in $VOICE_HOME/evidence/live/memory-20261116/ablation/demo-runC, text side only:
# what the person said, the machine note the voice session received, and what the assistant said. Turn 3 is the
# measured failure: the reply names Sunday and nothing in the call does.
CALL = [
    {
        "said": "What's the weather in Denver tomorrow?",
        "note": {"tasks": [{"task_id": "task_1", "status": "running"}], "clarification_needed": None},
        "said_by_assistant": "I'm running the task to get the weather in Denver tomorrow.",
    },
    {
        "said": "And what about Saturday?",
        "note": {"tasks": [{"task_id": "task_1", "status": "running"}], "clarification_needed": None},
        "said_by_assistant": "I'm running the task to get the weather in Denver for Saturday.",
    },
    {
        "said": "That's not what I meant.",
        "note": {"tasks": [{"task_id": "task_1", "status": "running"}], "clarification_needed": None},
        "said_by_assistant": "I'm running the task to get the weather in Denver for Sunday.",
    },
]
UPDATE_1 = {"updates": [{"type": "task_update", "task_id": "task_1", "status": "completed",
                          "result_summary": "Forecast for Denver, Colorado. Tuesday, November 17: a high of 69 "
                                            "degrees Fahrenheit and a 0 percent chance of precipitation."}]}


def sources_for(turn):
    """What the assistant may say on this turn: the person's own words earlier in the call, and this turn's note."""
    return [entry["said"] for entry in CALL[:CALL.index(turn) + 1]] + [turn["note"]]


class WeekdayInventedFromCorrectionTest(unittest.TestCase):
    def test_weekday_invented_from_correction(self):
        """Turn 3's reply names a day the person never said, so the fact check has to catch it."""
        turn = CALL[2]
        found = ungrounded(turn["said_by_assistant"], sources_for(turn))
        self.assertEqual([fact.surface for fact in found], ["Sunday"],
                         f"turn 3 replied {turn['said_by_assistant']!r} and the only ungrounded value is Sunday")

    def test_the_day_the_person_said_passes(self):
        """Turn 2's reply names Saturday, which the person said, so the same check must let it through."""
        turn = CALL[1]
        self.assertEqual(ungrounded(turn["said_by_assistant"], sources_for(turn)), [])

    def test_a_number_only_the_result_note_states_passes(self):
        """Turn 1's result carries 69 degrees and Tuesday, November 17. A reply may repeat those."""
        note = {"updates": [UPDATE_1], "tasks": [{"task_id": "task_1", "status": "completed",
                                                  "result_summary": UPDATE_1["updates"][0]["result_summary"]}]}
        reply = "It's 69 degrees in Denver with no chance of rain on Tuesday."
        self.assertEqual(ungrounded(reply, [CALL[0]["said"], note]), [])

    def test_a_number_nothing_states_is_rejected(self):
        reply = "It's 72 degrees in Denver on Saturday."
        found = ungrounded(reply, sources_for(CALL[1]))
        self.assertEqual([(fact.kind, fact.surface) for fact in found], [("number", "72")])

    def test_a_place_nothing_states_is_rejected(self):
        reply = "I'm running the task to get the weather in Boulder for Saturday."
        found = ungrounded(reply, sources_for(CALL[1]))
        self.assertEqual([(fact.kind, fact.surface) for fact in found], [("place", "Boulder")])

    def test_a_time_nothing_states_is_rejected(self):
        reply = "I'll check that at 3 pm."
        found = ungrounded(reply, sources_for(CALL[1]))
        self.assertEqual([(fact.kind, fact.surface) for fact in found], [("time", "3 pm")])

    def test_a_time_the_person_said_passes(self):
        person = "Book a table at seven tonight."
        self.assertEqual(ungrounded("Booked for 7 pm tonight.", [person]), [])

    def test_the_assistant_own_earlier_words_do_not_ground_a_fact(self):
        """The assistant said Saturday on turn 2. A reply that changes it to Sunday is still ungrounded."""
        found = ungrounded("I'm running the task for Sunday.", [CALL[0]["said"], CALL[1]["said"], CALL[1]["note"]])
        self.assertEqual([fact.kind for fact in found], ["weekday"])

    def test_a_reply_with_no_facts_passes(self):
        self.assertEqual(ungrounded("I'm still on it.", sources_for(CALL[2])), [])
        self.assertEqual(ungrounded("", sources_for(CALL[2])), [])


class MachineryTest(unittest.TestCase):
    def test_the_recorded_refusals_are_machinery(self):
        """Both sentences the demo actually said on turn 3 name the check, and one names the note."""
        self.assertEqual(names_machinery("I do not have the information needed to answer the fact check."),
                         ["fact check"])
        self.assertEqual(names_machinery("I cannot read the task description in the machine note."),
                         ["machine note", "task description", "note"])

    def test_every_listed_word_is_caught(self):
        for word in ("fact check", "snapshot", "machine note", "machine-generated", "task description", "task update",
                     "note"):
            with self.subTest(word=word):
                self.assertTrue(names_machinery(f"Here is the {word}."), word)

    def test_an_ordinary_reply_is_clean(self):
        for reply in ("I'm running the task to get the weather in Denver for Saturday.",
                      "The forecast is a high of 69 degrees.",
                      "I don't have the day for that yet.",
                      "Sorry, I can't confirm that right now.",
                      ""):
            with self.subTest(reply=reply):
                self.assertEqual(names_machinery(reply), [])


class ThirdPersonTest(unittest.TestCase):
    def test_the_person_named_in_the_third_person(self):
        self.assertEqual(speaks_of_person("I need to know what the user wants for Denver."), ["the user"])
        self.assertEqual(speaks_of_person("The person asked about Saturday, and they meant Sunday."),
                         ["the person", "they meant"])

    def test_a_reply_to_the_person(self):
        self.assertEqual(speaks_of_person("Sorry about that. What did you want instead?"), [])
        self.assertEqual(speaks_of_person("Saturday looks sunny, with a high of 54."), [])


if __name__ == "__main__":
    unittest.main()


class FalsePositiveTest(unittest.TestCase):
    """Correct replies the screens refused, found by the false-positive review of the records on disk."""

    def test_a_truthful_failed_lookup_is_not_machinery(self):
        """toolpick-polish2 turn 016 and toolpick-args turn 025: the task failed, and the reply said so."""
        self.assertEqual(names_machinery("I couldn't check the weather in Raleigh because the lookup failed."), [])

    def test_reading_the_running_note_aloud_is_still_machinery(self):
        self.assertTrue(names_machinery("It looks like the lookup is still running and has no answer yet."))

    def test_note_is_matched_as_a_word(self):
        self.assertEqual(names_machinery("Got it, I've noted that. The keynote starts soon."), [])
        self.assertEqual(names_machinery("The notes say it is running."), ["note"])

    def test_a_date_is_not_also_a_bare_number(self):
        self.assertEqual([fact.surface for fact in ungrounded("He was born on February 22, 1732.", [])],
                         ["February 22", "1732"])

    def test_thousands_commas_match_either_way(self):
        self.assertEqual(ungrounded("About 1200 people live there.", ["Population: 1,200."]), [])
        self.assertEqual(ungrounded("About 1,200 people live there.", ["Population: 1200."]), [])

    def test_a_rounded_source_value_may_be_said(self):
        self.assertEqual(ungrounded("A high of 57 degrees.", ["High 57.2 F."]), [])
        self.assertTrue(ungrounded("A high of 58 degrees.", ["High 57.2 F."]))

    def test_a_spoken_compound_number_is_one_value(self):
        for said in ("I need twenty five of them.", "I need twenty-five of them."):
            with self.subTest(said=said):
                self.assertEqual(ungrounded("You need 25.", [said]), [])

    def test_a_unit_is_not_a_place(self):
        self.assertEqual(ungrounded("It is 57 degrees in Fahrenheit.", ["a high of 57 degrees"]), [])
