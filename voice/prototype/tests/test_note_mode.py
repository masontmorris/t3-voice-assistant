"""Live mode's "note" turn: what text the voice session is given, and what a refused reply does.

The recorded call these stand for is 20261116-152653. Its voice session was given a JSON list of the person's
earlier turns before every reply, and the model answered the last line of that list instead of the audio. These
tests stand in for llama.cpp, so they pin what the turn sends and speaks, not what the model then says.
"""

import tempfile
import unittest
from pathlib import Path

import numpy as np

from voice.live.facts import ungrounded
from voice.prototype import run as prototype
from voice.prototype.segmenter import use_decider_cache
from voice.prototype.tasks import MockTaskService
from voice.prototype.tests.test_run_shared_session import CLOCK, FakeSession, Scripted, Source
from voice.segmentation.tasks import Orchestrator
from voice.tools.registry import ToolCall

EARLIER = ("Could you check the weather in Raleigh today?", "Okay, and these are decent results.")


def texts(session):
    return [event["item"]["content"][0]["text"] for event in session.sent
            if event.get("type") == "conversation.item.create"]


class TaskNoteTest(unittest.TestCase):
    def test_a_running_lookup_asks_for_a_few_words(self):
        note = prototype.task_note({"status": "running", "capability": "weather", "arguments": {"place": "Seattle"},
                                    "description": "and how is the weather in Seattle right now?"})
        self.assertIn("the weather lookup for Seattle is running", note)
        self.assertIn("Tell them you're checking, in a few words.", note)
        self.assertNotIn("right now", note, "the person's own words stay out of the note")

    def test_a_finished_lookup_carries_its_result(self):
        note = prototype.task_note({"status": "completed", "capability": "weather", "arguments": {"place": "Durham"},
                                    "result_summary": "Forecast for Durham: a high of 57 degrees Fahrenheit."})
        self.assertIn("Result: Forecast for Durham: a high of 57 degrees Fahrenheit.", note)

    def test_the_general_worker_is_named_as_a_lookup(self):
        note = prototype.task_note({"status": "running", "capability": "agent", "arguments": {}})
        self.assertIn("the lookup is running", note)

    def test_a_task_that_has_not_run_gets_no_note(self):
        for status in ("pending", "needs_clarification", "held_for_confirmation"):
            self.assertIsNone(prototype.task_note({"status": status}))
        self.assertIsNone(prototype.task_notes([{"status": "needs_clarification"}]))

    def test_a_task_waiting_for_a_place_asks_which_city(self):
        note = prototype.task_note({"status": "needs_clarification", "capability": "weather",
                                    "arguments": {"place": None, "when": "today"}, "needs": ["place"]})
        self.assertIn("the weather lookup has not started because they have not said which city or place they mean",
                      note)
        self.assertIn("Ask them in one short sentence, and don't guess one.", note)
        self.assertNotIn("today", note, "an argument the words did not say is not handed to the model")

    def test_every_note_says_who_it_is_for(self):
        for note in (prototype.RETRY_NOTE, prototype.STILL_RUNNING_NOTE, prototype.ASK_NOTE):
            self.assertTrue(note.startswith("[Note for you, not said by the person: "))
            self.assertTrue(note.endswith("]"))


class NoteTurn(unittest.IsolatedAsyncioTestCase):
    """The harness for one note-mode turn, and the voice session the turns share."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        use_decider_cache(Path(self.tmp.name) / "decider-cache", 20)
        self.original_open = prototype.Session.open
        self.addCleanup(setattr, FakeSession, "transcript", FakeSession.transcript)

        async def open_session(url, name, clock, on_event=None):
            return await (Scripted if name == "voice" else FakeSession).open(url, name, clock, on_event)

        prototype.Session.open = open_session
        self.addCleanup(setattr, prototype.Session, "open", self.original_open)
        self.voice = await prototype.Session.open("ws://unused", "voice", lambda: CLOCK)
        self.spoken = []

    async def turn(self, script, **more):
        self.voice.script, self.voice.asked = script, 0
        return await prototype.run_clip(
            Source(), "c", False, "ws://unused", None, 0.0, Path(self.tmp.name), {}, "hybrid", None, "gated",
            voice=self.voice, snapshot_mode="note", prior_utterances=EARLIER, final_decider=True, recall="note",
            fallback_pcm=b"\x01\x00" * 480, speak=lambda text, pcm: self.spoken.append(text), **more)


class NoteTurnTest(NoteTurn):
    async def test_a_turn_with_no_lookup_gives_the_model_no_text(self):
        record = await self.turn(["Glad to hear it."])
        self.assertEqual(texts(self.voice), [], "the reply follows the turn's audio and nothing else")
        self.assertEqual(self.spoken, ["Glad to hear it."])
        self.assertEqual(record["replies"][0]["text"], "Glad to hear it.")

    async def test_the_earlier_turns_never_reach_the_voice_session(self):
        await self.turn(["Glad to hear it."])
        sent = " ".join(str(event) for event in self.voice.sent)
        self.assertNotIn("decent results", sent)
        self.assertNotIn("call_turns", sent)

    async def test_a_refused_reply_is_asked_again_with_one_short_note(self):
        await self.turn(["The assistant responds with a greeting.", "Glad to hear it."])
        self.assertEqual(texts(self.voice), [prototype.RETRY_NOTE])
        self.assertEqual(self.spoken, ["Glad to hear it."])

    async def test_a_reply_refused_twice_is_asked_one_last_time_and_a_third_refusal_is_silent(self):
        record = await self.turn(["The user wants the weather.", "The user asked about results.",
                                  "The user is still asking."])
        self.assertEqual(self.spoken, [])
        self.assertEqual(record["replies"][0]["text"], "")
        self.assertIn("blocked", record["replies"][0])
        self.assertNotIn("fallback", [event["kind"] for event in record["timeline"]])

    async def test_the_last_ask_gives_a_turn_that_would_be_silent_one_short_line(self):
        record = await self.turn(["The user wants the weather.", "The user asked about results.",
                                  "I'm not sure what you mean."])
        self.assertEqual(self.spoken, ["I'm not sure what you mean."])
        self.assertEqual(record["replies"][0]["text"], "I'm not sure what you mean.")

    async def test_a_refused_value_is_named_in_the_retry(self):
        await self.turn(["George Washington was born on February 22, 1732.", "I can't look that up."])
        note, = texts(self.voice)
        self.assertIn("nothing gave you February 22, 1732", note)
        self.assertEqual(self.spoken, ["I can't look that up."])

    async def test_an_earlier_lookup_result_may_be_said_again(self):
        result = "Forecast for Raleigh. Monday, November 16: a high of 56 degrees Fahrenheit."
        await self.turn(["It was a high of 56 degrees in Raleigh."], prior_results=(result,))
        self.assertEqual(self.spoken, ["It was a high of 56 degrees in Raleigh."])


class PickedPlaceTest(NoteTurn):
    """A turn where the person asked the assistant to choose. The measured call is 20261116-152653 turn 25:
    "Pick a city on the east coast to check the weather for." The model chose Boston, nothing in the call states
    Boston, so both replies were refused and turns 27 and 28 then said nothing at all."""

    PICK = "Pick a city on the east coast to check the weather for."

    async def test_the_place_the_person_asked_the_assistant_to_choose_is_spoken(self):
        FakeSession.transcript = self.PICK
        record = await self.turn(["Let's check the weather in Boston."])
        self.assertEqual(self.spoken, ["Let's check the weather in Boston."])
        self.assertEqual(record["established"], ["Boston"], "the chosen place is the call's from now on")

    async def test_a_chosen_place_still_grounds_the_next_turns(self):
        FakeSession.transcript = "Do I need to wear a jacket?"
        await self.turn(["I couldn't check the weather in Boston, so I can't say if you need a jacket."],
                        established=("Boston",))
        self.assertEqual(self.spoken, ["I couldn't check the weather in Boston, so I can't say if you need a jacket."])

    async def test_a_place_with_no_pick_request_is_still_refused(self):
        record = await self.turn(["Let's check the weather in Boston.", "I can't look that up."])
        note, = texts(self.voice)
        self.assertIn("nothing gave you Boston", note)
        self.assertEqual(record["established"], [])
        self.assertEqual(self.spoken, ["I can't look that up."])


class StaleReplyTest(NoteTurn):
    """The measured failure is tool-pick arm 1 (`$VOICE_HOME/evidence/toolpick/toolpick-real`): turns 25 and 26 said
    "Hello! I'm checking the weather in a city on the East Coast. Which one?" and turns 28 to 37 said it again."""

    STALE = "Hello! I'm checking the weather in a city on the East Coast. Which one?"

    async def test_a_line_the_assistant_already_said_is_asked_again(self):
        await self.turn([self.STALE])
        self.assertEqual(self.spoken, [self.STALE])
        record = await self.turn([self.STALE, "I can hear you fine."], prior_replies=(self.STALE,))
        note = [text for text in texts(self.voice) if text == prototype.REPEAT_NOTE]
        self.assertEqual(note, [prototype.REPEAT_NOTE], "the retry is told it repeated itself")
        self.assertEqual(self.spoken[-1], "I can hear you fine.")
        self.assertEqual(record["replies"][0]["text"], "I can hear you fine.")

    async def test_a_stale_retry_is_asked_once_more_with_the_old_answer_set_aside(self):
        record = await self.turn([self.STALE, self.STALE, "I can hear you fine."], prior_replies=(self.STALE,))
        notes = [text for text in texts(self.voice) if text in (prototype.REPEAT_NOTE, prototype.NEW_WORDS_NOTE)]
        self.assertEqual(notes, [prototype.REPEAT_NOTE, prototype.NEW_WORDS_NOTE])
        self.assertEqual(record["replies"][0]["text"], "I can hear you fine.")

    async def test_a_stale_line_that_survives_every_ask_is_spoken_not_dropped(self):
        record = await self.turn([self.STALE, self.STALE, self.STALE], prior_replies=(self.STALE,))
        self.assertEqual(self.spoken, [self.STALE], "silence after the person speaks is worse than a stale line")
        self.assertEqual(record["replies"][0]["text"], self.STALE)
        self.assertIn("repeat", [event["kind"] for event in record["timeline"]])

    async def test_a_repeated_forecast_is_asked_again(self):
        forecast = "The weather in Durham today is a high of 57 degrees Fahrenheit with a 0% chance."
        given = "Forecast for Durham: a high of 57 degrees Fahrenheit with a 0 percent chance."
        record = await self.turn([forecast, "Both forecasts agree on a high of 57 degrees."],
                                 prior_replies=(forecast,), prior_results=(given,))
        self.assertEqual(self.spoken, ["Both forecasts agree on a high of 57 degrees."],
                         "the same forecast again is a stale reply, so the model answers in new words")
        self.assertEqual([text for text in texts(self.voice) if text == prototype.REPEAT_NOTE], [prototype.REPEAT_NOTE])
        self.assertEqual(record["replies"][0]["text"], "Both forecasts agree on a high of 57 degrees.")

    async def test_a_stale_line_that_grows_a_sentence_is_still_stale(self):
        record = await self.turn([self.STALE + " You likely won't need a jacket.", "I can hear you fine."],
                                 prior_replies=(self.STALE,))
        self.assertEqual(record["replies"][0]["text"], "I can hear you fine.")


def tone(seconds, rate=24_000, level=8000):
    return (np.sin(np.arange(int(seconds * rate)) * 0.2) * level).astype("<i2").tobytes()


class SpokenPartTest(unittest.TestCase):
    """A reply of two sentences with a pause between them, 1 s and 2 s of sound around 0.3 s of silence."""

    PCM = tone(1.0) + b"\x00\x00" * 7200 + tone(2.0)
    HEARD = ["What about in Durham?"]

    def part(self, text, pcm=None):
        return prototype.spoken_part({"text": text}, self.PCM if pcm is None else pcm, 24_000, self.HEARD, True)

    def test_the_opening_sentence_is_kept_and_cut_at_the_pause(self):
        reply, pcm = self.part("I'm checking the weather in Durham. It's a high of 68 degrees with a 10% chance of rain.")
        self.assertEqual(reply["text"], "I'm checking the weather in Durham.")
        self.assertIn("68 degrees", reply["dropped"])
        self.assertAlmostEqual(len(pcm) / 2 / 24_000, 1.15, delta=0.16)

    def test_a_reply_with_no_pause_is_not_cut(self):
        self.assertIsNone(self.part("I'm checking the weather in Durham. It's a high of 68 degrees.", tone(3.0)))

    def test_a_reply_whose_opening_fails_has_no_part(self):
        self.assertIsNone(self.part("It's a high of 68 degrees in Durham. I'm checking."))

    def test_a_reply_of_one_sentence_has_no_part(self):
        self.assertIsNone(self.part("It's a high of 68 degrees in Durham."))

    def test_a_one_word_opening_is_not_a_reply(self):
        """Measured on the baseline reply-path replay, turn 25: the refused pick was cut to "Hello!" and spoken."""
        pcm = tone(0.3) + b"\x00\x00" * 7200 + tone(1.0)
        self.assertIsNone(prototype.spoken_part({"text": "Hello! It's a high of 68 degrees in Boston."}, pcm,
                                                24_000, self.HEARD, True))


class AskRegistry:
    """A registry whose general worker answers at once."""

    timeout_s = 5.0

    def __init__(self, agent=True):
        self.agent, self.asked = agent, []

    def ask(self, instruction, context=None):
        self.asked.append((instruction, context))
        return ToolCall("agent", {"instruction": instruction, "context": context or []}) if self.agent else None

    def timeout_for(self, call):
        return 12.0

    def run(self, call):
        class Result:
            outcome, summary = "ok", "George Washington was born on February 22, 1732."

            def record(self):
                return {"tool": "agent", "arguments": call.arguments, "outcome": "ok", "summary": self.summary}
        return Result()


class Segmenter:
    """The service's record source for a lookup it starts itself: `orch` holds the tasks, and this one holds none."""

    dispatch_log = []

    def __init__(self):
        self.orch = Orchestrator()

    def records(self):
        return []


class AskTest(unittest.TestCase):
    def test_a_lookup_outside_the_segmenter_runs_and_reports_once(self):
        registry = AskRegistry()
        service = MockTaskService(Segmenter(), "conv", tools=registry, context=[{"user": "Hi"}])
        events = service.ask("what day was George Washington born on?", 1.0)
        self.assertEqual([event["event"] for event in events], ["running"])
        self.assertEqual(registry.asked[0][1], [{"user": "Hi"}])
        for call in service.pending_calls():
            call.result(timeout=5)
        self.assertEqual(service.poll(2.0), ["ask_1"])
        update, = service.take_updates()
        self.assertEqual(update["result_summary"], "George Washington was born on February 22, 1732.")
        entry, = service.update_entries([update])
        self.assertEqual(entry["capability"], "agent")
        self.assertEqual(service.take_updates(), [])
        service.close()

    def test_no_worker_means_no_lookup(self):
        service = MockTaskService(Segmenter(), "conv", tools=AskRegistry(agent=False))
        self.assertEqual(service.ask("anything", 1.0), [])
        self.assertEqual(service.executions, {})
        service.close()

    def test_the_workers_arguments_stay_out_of_the_snapshot(self):
        service = MockTaskService(Segmenter(), "conv", tools=AskRegistry(), context=[{"user": "secret words"}])
        service.ask("do I need a jacket?", 1.0)
        task, = service.snapshot("utt_1", "do I need a jacket?")["tasks"]
        self.assertEqual(task["arguments"], {})
        service.close()


class PlaceTest(unittest.TestCase):
    def test_a_place_they_named_without_a_preposition_is_theirs(self):
        """20261116-152653 turn 22: "what about San Diego?" and a reply that said "in San Diego" was refused."""
        reply = "I'm checking the weather in San Diego."
        self.assertEqual(ungrounded(reply, ["Okay, and what about San Diego?"]), [])
        self.assertEqual([fact.surface for fact in ungrounded(reply, ["Okay, and what about Portland?"])],
                         ["San"])


if __name__ == "__main__":
    unittest.main()


class EarlierResultsTest(unittest.TestCase):
    """Which earlier lookup results the fact check counts as given on a turn."""

    RESULT = "Forecast for Lisbon. Tuesday, November 17: a high of 70 degrees, 78 percent chance of precipitation."

    def test_a_failed_task_keeps_the_earlier_results(self):
        """recall-memory note turn 035: "What did you tell me about Lisbon?" beside a failed task was refused."""
        self.assertEqual(prototype.given_results((self.RESULT,), True, [{"status": "failed"}]), (self.RESULT,))

    def test_a_running_or_completed_task_drops_them(self):
        """Beside a new lookup an old result would pass for the new one (callfix3 replay fix1, turn 25)."""
        for status in ("running", "completed"):
            with self.subTest(status=status):
                self.assertEqual(prototype.given_results((self.RESULT,), True, [{"status": status}]), ())

    def test_no_task_keeps_them(self):
        self.assertEqual(prototype.given_results((self.RESULT,), True, []), (self.RESULT,))


class MayChooseTest(unittest.TestCase):
    PICK = "Pick a city on the east coast to check the weather for."

    def test_a_pick_with_no_lookup_place_is_the_assistants(self):
        self.assertTrue(prototype.may_choose(self.PICK, [{"status": "running", "capability": "weather",
                                                          "arguments": {"place": None, "when": "today"}}]))
        self.assertTrue(prototype.may_choose(self.PICK, []))

    def test_a_lookup_that_already_has_a_place_made_the_choice(self):
        """callfix3 replay fix2, turn 025: the lookup ran for San Diego and the reply chose Boston."""
        tasks = [{"status": "running", "capability": "weather", "arguments": {"place": "San Diego", "when": "today"}}]
        self.assertFalse(prototype.may_choose(self.PICK, tasks))

    def test_no_pick_request_is_no_choice(self):
        self.assertFalse(prototype.may_choose("What's the weather in Boston?", []))
