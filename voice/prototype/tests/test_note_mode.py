"""Live mode's "note" turn: what text the voice session is given, and what a refused reply does.

The recorded call these stand for is 20261116-152653. Its voice session was given a JSON list of the person's
earlier turns before every reply, and the model answered the last line of that list instead of the audio. These
tests stand in for llama.cpp, so they pin what the turn sends and speaks, not what the model then says.
"""

import base64
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from voice.live.facts import ungrounded
from voice.prototype import run as prototype
from voice.prototype.tasks import MockTaskService
from voice.prototype.tests import isolate_decider_cache
from voice.prototype.tests.test_run_shared_session import CLOCK, FakeSession, Scripted, Source, heard_by
from voice.segmentation.tasks import Orchestrator
from voice.t3 import fakes as F
from voice.t3.acts import Act, act
from voice.t3.acts import Kind as S
from voice.t3.speech import Lane, Speech, fallback_prompt
from voice.t3.test_verify import Failing
from voice.t3.verify import Decider
from voice.tools.registry import ToolCall

EARLIER = ("Could you check the weather in Raleigh today?", "Okay, and these are decent results.")


CHECKED = Speech(None, lambda thread_id: None, F.Agree())


def checked(line, acts=(act(S.CONFIRM_STOP, thread="clamp helper"),)):
    """`line` as a RenderedLine, through the real slot check and a decider that agrees."""
    return CHECKED.accept(acts, line, "text_model").line


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
        self.addCleanup(isolate_decider_cache(Path(self.tmp.name) / "decider-cache", 20))
        self.original_open = prototype.Session.open
        self.addCleanup(setattr, FakeSession, "transcript", FakeSession.transcript)
        self.fallback_sessions = []

        async def open_session(url, name, clock, on_event=None):
            session = await (Scripted if name in ("voice", "t3-fallback") else FakeSession).open(
                url, name, clock, on_event)
            if name == "t3-fallback":
                session.script = self.fallback_script
                self.fallback_sessions.append(session)
            return session

        prototype.Session.open = open_session
        self.addCleanup(setattr, prototype.Session, "open", self.original_open)
        self.voice = await prototype.Session.open("ws://unused", "voice", lambda: CLOCK)
        self.spoken = []

    async def turn(self, script, **more):
        self.fallback_script = script.copy()
        self.voice.script, self.voice.asked = script, 0
        return await prototype.run_clip(
            Source(), "c", False, "ws://unused", None, 0.0, Path(self.tmp.name), {}, "hybrid", None, "gated",
            voice=self.voice, snapshot_mode="note", prior_utterances=EARLIER, final_decider=True, recall="note",
            fallback_pcm=b"\x01\x00" * 480, speak=heard_by(self.spoken), **more)


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



class SayVerbatimTest(NoteTurn):
    """Integrated runs 2 and 3 (2026-11-18): handed a T3 line to word, LFM3 said "I've stopped the thread" for "Stop
    the X thread?" and made up quotes from the thread. A line the text model worded and the slot check passed is
    spoken as it is."""

    async def test_a_worded_line_is_spoken_word_for_word_and_the_model_is_not_asked(self):
        spoken, rendered = [], []

        def render(text, rate):
            rendered.append((text, rate))
            return b"\x01\x00" * 480

        line = "Should I stop the clamp helper thread?"
        reply, pcm = await prototype.say_verbatim(self.voice, 24_000, 0.0, checked(line),
                                                  lambda kind, **detail: None, heard_by(spoken), render)
        self.assertEqual((spoken, rendered), ([line], [(line, 24_000)]))
        self.assertEqual((reply["text"], len(pcm)), (line, 960))
        self.assertFalse([e for e in self.voice.sent if e.get("type") == "response.create"], "LFM3 words nothing")
        told = prototype.NOTE_OPEN + f'you just told them this about their T3 work: "{line}"]'
        self.assertEqual(texts(self.voice), [told])

    async def test_a_failed_render_says_the_line_in_the_record_and_plays_nothing(self):
        spoken = []

        def render(text, rate):
            raise OSError("no TTS")

        line = checked("Should I stop the clamp helper thread?")
        reply, pcm = await prototype.say_verbatim(self.voice, 24_000, 0.0, line,
                                                  lambda kind, **detail: None, heard_by(spoken), render)
        self.assertEqual((reply["text"], pcm, spoken), (line.text, b"", []))
        self.assertEqual(texts(self.voice), [], "a failed render never enters the conversation as heard")

    async def test_a_plain_string_is_never_spoken(self):
        spoken, rendered = [], []
        for line in ("It finished.", ("It finished.",)):
            with self.subTest(line=line), self.assertRaises(TypeError):
                await prototype.say_verbatim(self.voice, 24_000, 0.0, line, lambda kind, **detail: None,
                                             heard_by(spoken), lambda text, rate: rendered.append(text) or b"")
        self.assertEqual((spoken, rendered, self.voice.sent), ([], [], []))

    def test_a_t3_entry_gets_no_task_note(self):
        acts = [act(S.CONFIRM_STOP, thread="clamp helper").to_json()]
        self.assertIsNone(prototype.task_note({"acts": acts, "status": "completed"}))
        self.assertIsNone(prototype.task_notes([{"acts": acts, "status": "completed"}]))


class Worded:
    """The `words` side of a T3 task service: `lines` maps a task id to what its text model says, or to None when
    the text model fails. The slot check and the LFM3 prompt are the real ones, and the decider agrees."""

    lines: dict = {}
    speech = CHECKED

    def say(self, entry, turns=()):
        line = self.lines.get(entry["task_id"])
        if line is None:
            return self.speech.render(self._acts(entry), turns)
        return self._accepted(entry, self.speech.accept(self._acts(entry), line, "text_model"))

    def fallback_prompt(self, entry):
        return fallback_prompt(self._acts(entry))

    def accept(self, entry, text):
        return self._accepted(entry, self.speech.accept(self._acts(entry), text, "lfm3"))

    @staticmethod
    def _accepted(entry, rendered):
        if rendered.line is not None:
            entry["spoken"] = rendered.line.text
        return rendered

    @staticmethod
    def _acts(entry):
        return [Act.from_json(a) for a in entry["acts"]]


STOP_ACTS = [act(S.CONFIRM_STOP, thread="clamp helper").to_json()]


class T3SnapshotService(Worded, MockTaskService):
    """The task service with one T3 confirm act in its snapshot, as T3TaskService gives it."""

    lines = {"t3_1": "Should I stop the clamp helper thread?"}
    delivered: list = []

    def snapshot(self, *args, **kwargs):
        snapshot = super().snapshot(*args, **kwargs)
        snapshot["tasks"].append({"task_id": "t3_1", "description": "a T3 thread", "status": "completed",
                                  "acts": STOP_ACTS, "evidence": {"thread_id": "t5", "sequences": [150]}})
        return snapshot

    def record_delivery(self, entry, state):
        type(self).delivered.append((entry["task_id"], state))


class T3TurnTest(NoteTurn):
    async def test_a_fallback_echo_cannot_reach_playback_or_read_the_call(self):
        class RateLimited:
            model = "fake/text:free"

            def complete(self, messages):
                raise OSError("HTTP 429")

        class NoWording(Worded):
            speech = Speech(Lane(RateLimited()), lambda thread_id: None, F.Agree())

        class Echo(FakeSession):
            async def send(self, **event):
                if event["type"] != "response.create":
                    await super().send(**event)
                    return
                self.sent.append(event)
                await self.push({"type": "response.output_text.delta", "delta": texts(self)[-1]})
                if self.configs[-1].get("output_modalities") != ["text"]:
                    await self.push({"type": "response.output_audio.delta",
                                     "delta": base64.b64encode(bytes(960)).decode()})
                await self.push({"type": "response.done", "response": {"status": "completed", "output": []}})

        voice = await Echo.open("ws://unused", "voice", lambda: CLOCK)
        await prototype.update_session(voice, {"instructions": "The call", "output_modalities": ["text", "audio"]})
        user = "Stop the clamp helper thread."
        await prototype.create_item(voice, prototype.plain_text(user))
        spoken, fallback_sessions = [], []

        async def open_fallback(url, name, clock):
            fallback = await Echo.open(url, name, clock)
            fallback_sessions.append(fallback)
            return fallback

        rendered = []
        with patch.object(prototype.Session, "open", open_fallback):
            reply, pcm = await prototype.say_acts(
                voice, 24_000, CLOCK,
                {"task_id": "echo", "acts": [act(S.RUN_UPDATE, thread="clamp helper", status="running").to_json()]},
                NoWording(), [{"user": user}], lambda *args, **kwargs: None, heard_by(spoken),
                lambda text, rate: rendered.append(text) or bytes(960), fallback_url="ws://unused")
        self.assertEqual((spoken, pcm, reply["delivery"]), ([], b"", "unsent"))
        self.assertEqual(rendered, [], "an echo cannot reach TTS")
        self.assertIn("not plain speech", reply["problems"])
        self.assertEqual(texts(voice), [user])
        fallback, = fallback_sessions
        self.assertEqual(fallback.configs[-1]["output_modalities"], ["text"])
        self.assertNotIn(user, str(fallback.sent))
        self.assertNotIn("The thread's run is now", texts(fallback)[0])
        self.assertTrue(fallback.closed)

    async def test_note_mode_speaks_stop_needs_you_and_done_without_a_model_reply(self):
        needs_you = "The clamp helper thread needs you: Which file?"
        done = "The release notes thread finished, and it wrote the notes."

        class WithNews(T3SnapshotService):
            lines = {**T3SnapshotService.lines, "n1": needs_you, "n2": done}

            def take_updates(self):
                return [{"type": "task_update", "task_id": "n1", "status": "live", "result_summary": None,
                         "acts": [act(S.NEEDS_YOU, thread="clamp helper", prompt="Which file?").to_json()]},
                        {"type": "task_update", "task_id": "n2", "status": "completed", "result_summary": None,
                         "acts": [act(S.DONE, thread="release notes", status="completed",
                                      summary="It wrote the notes.").to_json()]}]

            def update_entries(self, updates):
                return updates

        rendered = []

        def render(text, rate):
            rendered.append(text)
            return bytes(960)

        await self.turn([], task_service=WithNews, t3_voice=render)
        lines = ["Should I stop the clamp helper thread?", needs_you, done]
        self.assertEqual(rendered, lines)
        self.assertEqual(self.spoken, lines)
        self.assertEqual(self.voice.asked, 0)
        self.assertFalse([text for text in texts(self.voice)
                          if text.startswith((prototype.SNAPSHOT_PREFIX, prototype.UPDATE_PREFIX))])

    async def test_a_turn_with_only_a_t3_line_speaks_it_and_asks_the_model_nothing(self):
        record = await self.turn(["I've stopped the clamp helper thread."], task_service=T3SnapshotService,
                                 t3_voice=lambda text, rate: bytes(960))
        self.assertEqual(self.spoken, ["Should I stop the clamp helper thread?"])
        self.assertEqual(self.voice.asked, 0, "LFM3 words no T3 act the text model worded")
        self.assertEqual([(r["kind"], r["worded_by"]) for r in record["replies"]], [("t3", "text_model")])

    async def test_a_slot_failing_render_falls_back_to_lfm3_under_the_same_check(self):
        class BadWording(T3SnapshotService):
            lines = {"t3_1": "I've stopped the clamp helper thread."}

        BadWording.delivered = []
        record = await self.turn(["Do you want me to stop the clamp helper thread?"], task_service=BadWording,
                                 t3_voice=lambda text, rate: bytes(960))
        self.assertEqual(self.spoken, ["Do you want me to stop the clamp helper thread?"])
        fallback, = self.fallback_sessions
        self.assertEqual((fallback.asked, self.voice.asked), (1, 0))
        self.assertIn(self.spoken[0], texts(self.voice)[0])
        self.assertEqual([(r["kind"], r["worded_by"], r["delivery"]) for r in record["replies"]],
                         [("t3", "lfm3", "sent")])
        self.assertEqual(BadWording.delivered, [("t3_1", "sent")])

    async def test_a_decider_timeout_goes_to_the_fallback_and_never_to_speech(self):
        class TimedOut(T3SnapshotService):
            speech = Speech(None, lambda thread_id: None, Decider(Failing(socket.timeout("timed out"))))

        TimedOut.delivered = []
        record = await self.turn(["Do you want me to stop the clamp helper thread?"], task_service=TimedOut,
                                 t3_voice=lambda text, rate: bytes(960))
        self.assertEqual(self.spoken, [], "neither the text model's line nor LFM3's is spoken unchecked")
        fallback, = self.fallback_sessions
        self.assertEqual((fallback.asked, texts(self.voice)), (1, []))
        self.assertEqual([(r["kind"], r["worded_by"], r["delivery"]) for r in record["replies"]],
                         [("t3", "lfm3", "unsent")])
        self.assertIn("TimeoutError", record["replies"][0]["decider"]["error"])
        self.assertEqual(TimedOut.delivered, [("t3_1", "unsent")])

    async def test_when_both_wordings_fail_nothing_is_said_and_the_act_stays_unsent(self):
        class NoWording(T3SnapshotService):
            lines = {}

        NoWording.delivered = []
        record = await self.turn(["I've stopped the clamp helper thread."], task_service=NoWording,
                                 t3_voice=lambda text, rate: bytes(960))
        self.assertEqual(self.spoken, [], "no stock line stands in for the act")
        self.assertEqual([(r["kind"], r["worded_by"], r["delivery"], r["text"]) for r in record["replies"]],
                         [("t3", "lfm3", "unsent", "")])
        self.assertEqual(NoWording.delivered, [("t3_1", "unsent")])
