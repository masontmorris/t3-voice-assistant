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

    def test_every_note_says_who_it_is_for(self):
        for note in (prototype.RETRY_NOTE, prototype.STILL_RUNNING_NOTE, prototype.ASK_NOTE):
            self.assertTrue(note.startswith("[Note for you, not said by the person: "))
            self.assertTrue(note.endswith("]"))


class NoteTurnTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        use_decider_cache(Path(self.tmp.name) / "decider-cache", 20)
        self.original_open = prototype.Session.open

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

    async def test_a_reply_refused_twice_is_not_spoken_and_no_fixed_line_plays(self):
        record = await self.turn(["The user wants the weather.", "The user asked about results."])
        self.assertEqual(self.spoken, [])
        self.assertEqual(record["replies"][0]["text"], "")
        self.assertIn("blocked", record["replies"][0])
        self.assertNotIn("fallback", [event["kind"] for event in record["timeline"]])

    async def test_a_refused_value_is_named_in_the_retry(self):
        await self.turn(["George Washington was born on February 22, 1732.", "I can't look that up."])
        note, = texts(self.voice)
        self.assertIn("nothing gave you February 22, 1732", note)
        self.assertEqual(self.spoken, ["I can't look that up."])

    async def test_an_earlier_lookup_result_may_be_said_again(self):
        result = "Forecast for Raleigh. Monday, November 16: a high of 56 degrees Fahrenheit."
        await self.turn(["It was a high of 56 degrees in Raleigh."], prior_results=(result,))
        self.assertEqual(self.spoken, ["It was a high of 56 degrees in Raleigh."])


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
    dispatch_log = []

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
