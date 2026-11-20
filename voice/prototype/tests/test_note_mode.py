"""Live mode's "note" turn: what text the voice session is given, and that no reply is held back.

The recorded call these stand for is 20261116-152653. Its voice session was given a JSON list of the person's
earlier turns before every reply, and the model answered the last line of that list instead of the audio. These
tests stand in for llama.cpp, so they pin what the turn sends and speaks, not what the model then says.
"""

import asyncio
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
from voice.t3.acts import act
from voice.t3.acts import Kind as S
from voice.t3.speech import Speech
from voice.t3.test_verify import Failing
from voice.t3.verify import Decider
from voice.tools.registry import ToolCall

EARLIER = ("Could you check the weather in Raleigh today?", "Okay, and these are decent results.")


CHECKED = Speech(None, lambda thread_id: None, F.Agree())


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
        note = prototype.task_note({"status": "completed", "capability": "weather", "arguments": {"place": "Lisbon"},
                                    "result_summary": "Forecast for Lisbon: a high of 57 degrees Fahrenheit."})
        self.assertIn("Result: Forecast for Lisbon: a high of 57 degrees Fahrenheit.", note)

    def test_the_general_worker_is_named_as_a_lookup(self):
        note = prototype.task_note({"status": "running", "capability": "agent", "arguments": {}})
        self.assertIn("the lookup is running", note)

    def test_a_t3_segment_with_no_line_of_its_own_gets_no_note(self):
        """One request cut into several launch segments asks one question, so the later segments carry no acts."""
        self.assertIsNone(prototype.task_note({"status": "completed", "capability": "t3_start", "arguments": {},
                                               "result_summary": None}))

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
        for note in (prototype.ASK_NOTE,):
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
            speak=heard_by(self.spoken), **more)


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

    async def test_a_reply_the_check_dislikes_is_spoken_once_and_the_model_is_not_asked_again(self):
        record = await self.turn(["The assistant responds with a greeting.", "Glad to hear it."])
        self.assertEqual(texts(self.voice), [])
        self.assertEqual(self.voice.asked, 1)
        self.assertEqual(self.spoken, ["The assistant responds with a greeting."])
        self.assertEqual([event["kind"] for event in record["timeline"] if event["kind"] == "fact_check"],
                         ["fact_check"])

    async def test_a_third_person_reply_is_spoken_and_logged(self):
        record = await self.turn(["The user wants the weather."])
        self.assertEqual(self.spoken, ["The user wants the weather."])
        self.assertEqual(record["replies"][0]["text"], "The user wants the weather.")
        self.assertNotIn("blocked", record["replies"][0])
        self.assertNotIn("fallback", [event["kind"] for event in record["timeline"]])

    async def test_a_value_nothing_gave_is_spoken_and_logged(self):
        record = await self.turn(["George Washington was born on February 22, 1732."])
        self.assertEqual(self.spoken, ["George Washington was born on February 22, 1732."])
        check, = [event for event in record["timeline"] if event["kind"] == "fact_check"]
        self.assertIn("date February 22", check["ungrounded"])

    async def test_an_earlier_lookup_result_may_be_said_again(self):
        result = "Forecast for Raleigh. Monday, November 16: a high of 56 degrees Fahrenheit."
        record = await self.turn(["It was a high of 56 degrees in Raleigh."], prior_results=(result,))
        self.assertEqual(self.spoken, ["It was a high of 56 degrees in Raleigh."])
        self.assertEqual([event for event in record["timeline"] if event["kind"] == "fact_check"], [])

    async def test_a_line_the_assistant_already_said_is_spoken_again_and_logged_as_a_repeat(self):
        stale = "Hello! I'm checking the weather in a city on the East Coast. Which one?"
        await self.turn([stale])
        record = await self.turn([stale, "I can hear you fine."], prior_replies=(stale,))
        self.assertEqual(self.spoken, [stale, stale])
        check, = [event for event in record["timeline"] if event["kind"] == "fact_check"]
        self.assertTrue(check["repeated"])


class RelayNoteTest(unittest.TestCase):
    def test_the_note_quotes_the_words_and_ends_on_them_and_the_retry_is_another_wording(self):
        line = "Do you mean the thread called Example Tidy Tides?"
        first, retry = prototype.relay_note(line), prototype.relay_note(line, 1)
        self.assertNotEqual(first, retry)
        for note in (first, retry):
            with self.subTest(note=note[-60:]):
                self.assertTrue(note.endswith(f'"{line}"]'), "no instruction follows the words")
                self.assertEqual(note.count(line), 1)
        self.assertNotIn("Reply with only these words", first)
        self.assertLess(retry.index("Reply with only these words"), retry.index(line))


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


STOP = act(S.CONFIRM_STOP, thread="clamp helper")
DRAFT = "Should I stop the clamp helper thread?"


class T3SnapshotService(MockTaskService):
    draft = DRAFT

    def snapshot(self, *args, **kwargs):
        snapshot = super().snapshot(*args, **kwargs)
        snapshot["tasks"].append({"task_id": "t3_1", "status": "completed", "acts": [STOP.to_json()]})
        return snapshot

    def say(self, entry, turns=()):
        return CHECKED.accept((STOP,), self.draft, "text_model")

    def accept(self, entry, text, draft):
        return CHECKED.recite(draft, text)

    def refuse(self, entry, rendered):
        pass


LOOKUP = {"task_id": "task_1", "status": "completed", "capability": "weather", "arguments": {"place": "Lisbon"},
          "result_summary": "Forecast for Lisbon: a high of 57 degrees Fahrenheit."}


def asked_in_order(*entries):
    """A T3 service whose turn asked for `entries`, in that order."""
    class Ordered(T3SnapshotService):
        def snapshot(self, *args, **kwargs):
            return {**MockTaskService.snapshot(self, *args, **kwargs), "tasks": [dict(entry) for entry in entries]}
    return Ordered


T3_ENTRY = {"task_id": "t3_1", "status": "completed", "acts": [STOP.to_json()]}


class Streaming(Scripted):
    """A voice session that streams each reply a word at a time and ends it where a `response.cancel` arrives, as
    llama.cpp does: the words it generated before the cancel stay in the conversation."""

    async def send(self, **event):
        if event.get("type") == "response.cancel":
            self.sent.append(event)
            self.cancelled = True
        elif event.get("type") == "response.create":
            self.sent.append(event)
            self.asked += 1
            self.cancelled = False
            self.generating = asyncio.create_task(self.generate(self.script.pop(0)))
        else:
            await FakeSession.send(self, **event)

    async def wait_for(self, *types, after=0, timeout=120):
        if (drained := getattr(self, "drained", None)) is not None:
            drained.set()
        return await super().wait_for(*types, after=after, timeout=timeout)

    async def generate(self, text):
        """One word at a time, and the next only once the turn has read this one and gone back to waiting, so a cancel
        sent on a word reaches the session before the next word does."""
        self.drained = asyncio.Event()
        for word in text.split(" "):
            for event in ({"type": "response.output_text.delta", "delta": word + " "},
                          {"type": "response.output_audio.delta", "delta": "AQA="}):
                self.drained.clear()
                await self.push(event)
                await self.drained.wait()
            if self.cancelled:
                break
        await self.push({"type": "response.done",
                         "response": {"status": "cancelled" if self.cancelled else "completed", "output": []}})


class T3TurnTest(NoteTurn):
    async def send_checked(self, line, pcm):
        self.spoken.append(line.text)
        return pcm, CLOCK

    async def test_a_mixed_turn_speaks_only_the_t3_line_and_then_the_lookup_as_an_update(self):
        for order in ((LOOKUP, T3_ENTRY), (T3_ENTRY, LOOKUP)):
            with self.subTest(first=order[0]["task_id"]):
                self.spoken.clear()
                self.voice.sent.clear()
                record = await self.turn(["I can't start a thread.", DRAFT, "Lisbon will reach 57 degrees."],
                                         task_service=asked_in_order(*order), send_checked=self.send_checked)
                created = [e for e in self.voice.sent if e.get("type") == "response.create"]
                self.assertEqual(created[0]["response"], {"output_modalities": ["text"]},
                                 "the audio is answered in text first")
                self.assertEqual(texts(self.voice), [prototype.relay_note(DRAFT), prototype.task_note(LOOKUP)])
                self.assertEqual([(r["kind"], r.get("delivery", "sent")) for r in record["replies"]],
                                 [("t3", "sent"), ("update", "sent")])
                self.assertEqual(self.spoken, [DRAFT, "Lisbon will reach 57 degrees."],
                                 "the text answer is never heard")

    async def test_a_t3_turn_with_no_lookup_to_speak_about_answers_nothing_in_text(self):
        segment = {"task_id": "task_2", "status": "completed", "capability": "t3_start", "arguments": {},
                   "result_summary": None}
        await self.turn([DRAFT], task_service=asked_in_order(T3_ENTRY, segment), send_checked=self.send_checked)
        created = [e for e in self.voice.sent if e.get("type") == "response.create"]
        self.assertEqual(len(created), 1, "only the relay: the audio needs no answer of its own")
        self.assertEqual(self.spoken, [DRAFT])

    async def test_words_that_answered_a_t3_question_say_only_the_t3_line(self):
        asking = dict(LOOKUP, status="needs_clarification", needs=["place"])

        class Answered(asked_in_order(asking, T3_ENTRY)):
            def open_question(self):
                return {"t3": True}

            def answer(self, waiting, instruction, now):
                return [{"task_id": "t3_1", "event": "completed", "instruction": instruction}]

        record = await self.turn([DRAFT], task_service=Answered, send_checked=self.send_checked)
        self.assertEqual(texts(self.voice), [prototype.relay_note(DRAFT)],
                         "a lookup read from the same words is not asked about")
        self.assertEqual([r["kind"] for r in record["replies"]], ["t3"])

    async def test_a_held_t3_line_does_not_fall_through_to_ordinary_speech_and_the_lookup_is_still_said(self):
        record = await self.turn(["Sure.", "I'll check.", "I'll check.", "Lisbon will reach 57 degrees."],
                                 task_service=asked_in_order(LOOKUP, T3_ENTRY), send_checked=self.send_checked)
        self.assertEqual([(r["kind"], r.get("delivery", "sent")) for r in record["replies"]],
                         [("t3", "unsent"), ("update", "sent")])
        self.assertEqual(self.spoken, ["Lisbon will reach 57 degrees."])

    async def test_a_turn_with_a_t3_line_takes_its_lookups_late_updates_and_never_t3_news(self):
        taken = []

        class Late(T3SnapshotService):
            def take_task_updates(self):
                taken.append(True)
                return []

            def take_updates(self):
                raise AssertionError("a turn never takes T3 news with its updates")

        record = await self.turn([DRAFT], task_service=Late, send_checked=self.send_checked)
        self.assertEqual(([r["kind"] for r in record["replies"]], self.voice.asked, taken), (["t3"], 1, [True]))
        self.assertEqual(self.spoken, [DRAFT])

    async def test_an_ordinary_turn_takes_its_own_updates_and_never_t3_news(self):
        class Plain(MockTaskService):
            def take_task_updates(self):
                return []

            def take_updates(self):
                raise AssertionError("a turn never takes T3 news with its updates")

        record = await self.turn(["Glad to hear it."], task_service=Plain)
        self.assertEqual([r["kind"] for r in record["replies"]], ["snapshot"])

    async def test_a_draft_that_fails_is_worded_again_in_the_same_turn(self):
        class Retries(T3SnapshotService):
            attempts = 0

            def say(self, entry, turns=()):
                Retries.attempts += 1
                return (CHECKED.accept((STOP,), "I stopped the clamp helper thread.", "text_model")
                        if Retries.attempts == 1 else CHECKED.accept((STOP,), DRAFT, "text_model"))

        record = await self.turn([DRAFT], task_service=Retries, send_checked=self.send_checked)
        self.assertEqual((Retries.attempts, self.spoken), (2, [DRAFT]))
        self.assertEqual([e["attempt"] for e in record["timeline"] if e["kind"] == "t3_words"], [1, 2])

    async def test_a_t3_reply_with_an_appended_sentence_is_held(self):
        record = await self.turn([DRAFT + " Ready?"], task_service=T3SnapshotService, send_checked=self.send_checked)
        self.assertEqual(self.spoken, [])
        self.assertEqual(record["replies"][0]["delivery"], "unsent")

    async def test_a_t3_entry_uses_the_same_call_session_in_each_snapshot_mode(self):
        for mode in ("note", "reply", "before-audio", "off"):
            with self.subTest(mode=mode):
                self.voice.script, self.voice.asked = [DRAFT], 0
                self.voice.sent.clear()
                record = await prototype.run_clip(
                    Source(), "c", False, "ws://unused", None, 0.0, Path(self.tmp.name), {},
                    voice=self.voice, snapshot_mode=mode, final_decider=True, task_service=T3SnapshotService,
                    send_checked=self.send_checked)
                self.assertEqual(self.voice.asked, 1)
                self.assertEqual(texts(self.voice), [prototype.relay_note(DRAFT)])
                self.assertEqual([(r["kind"], r["delivery"]) for r in record["replies"]], [("t3", "sent")])
        self.assertEqual(self.spoken, [DRAFT] * 4)

    async def test_a_refused_draft_never_enters_the_call(self):
        class Refused(T3SnapshotService):
            draft = "I stopped the clamp helper thread."

        record = await self.turn(["It stopped because it was interrupted."], task_service=Refused,
                                 send_checked=self.send_checked)
        self.assertEqual(texts(self.voice), [])
        self.assertEqual(self.spoken, [])
        self.assertEqual(record["replies"][0]["delivery"], "unsent")
        # Unanswered, the turn's audio was answered by the next turn's reply with thread facts no check saw
        # (rehearsal, 2026-11-20), so it gets an answer in text nobody hears.
        asked, = [e for e in self.voice.sent if e.get("type") == "response.create"]
        self.assertEqual(asked["response"]["output_modalities"], ["text"])

    async def test_call_audio_waits_for_response_done_and_the_reply_check(self):
        generating, finish = asyncio.Event(), asyncio.Event()

        class Held(Scripted):
            async def push(self, event):
                if event["type"] == "response.done":
                    generating.set()
                    await finish.wait()
                await super().push(event)

        class CheckedTwice(T3SnapshotService):
            def accept(inner, entry, text, draft):
                self.assertEqual(self.spoken, [])
                return super().accept(entry, text, draft)

        self.voice = await Held.open("ws://unused", "voice", lambda: CLOCK)
        task = asyncio.create_task(self.turn([DRAFT], task_service=CheckedTwice, send_checked=self.send_checked))
        await generating.wait()
        self.assertTrue(self.voice.audio)
        self.assertEqual(self.spoken, [])
        finish.set()
        await task
        self.assertEqual(self.spoken, [DRAFT])

    async def test_a_reply_that_is_not_the_drafts_words_releases_no_audio_and_leaves_no_discard_note(self):
        call_shapes = ("I can start that for you. Here's the draft: " + DRAFT + " Let's get this started!",
                       "Here's the draft: " + DRAFT, DRAFT + " Let's get this started!", "Should you stop the clamp helper thread?",
                       "Should I stop the clamp helper?")
        for reply in call_shapes:
            with self.subTest(reply=reply):
                self.spoken.clear()
                self.voice.sent.clear()
                record = await self.turn([reply, reply], task_service=T3SnapshotService, send_checked=self.send_checked)
                self.assertEqual(self.spoken, [])
                self.assertEqual(texts(self.voice), [prototype.relay_note(DRAFT), prototype.relay_note(DRAFT, 1)],
                                 "the words went in twice, the second time in the other wording, and no note about a "
                                 "reply that did not play follows either")
                self.assertEqual(record["replies"][0]["delivery"], "unsent")
                self.assertEqual(record["replies"][0]["text"], reply)

    async def test_the_drafts_words_pass_whatever_the_case_punctuation_or_markdown(self):
        for reply in (DRAFT, DRAFT.lower(), "**" + DRAFT + "**", DRAFT.replace("?", " ?"), '"' + DRAFT + '"'):
            with self.subTest(reply=reply):
                self.spoken.clear()
                await self.turn([reply], task_service=T3SnapshotService, send_checked=self.send_checked)
                self.assertEqual(self.spoken, [DRAFT])

    async def test_a_reply_that_leaves_the_draft_is_stopped_at_the_first_other_word(self):
        """llama.cpp cannot remove an item, so what a held reply generated stays in the call. The turn stops the
        response at the first word that is not the draft's, so only its start is left."""
        self.voice = await Streaming.open("ws://unused", "voice", lambda: CLOCK)
        reply = "I can stop that for you. Here's the draft: " + DRAFT + " Let's get this started!"
        record = await self.turn([reply, reply], task_service=T3SnapshotService, send_checked=self.send_checked)
        said = [e.get("delta", "").strip() for e in self.voice.events if e["type"] == "response.output_text.delta"]
        self.assertEqual(said, ["I", "I"], "each try was stopped at the first word the draft does not start with")
        self.assertEqual(sum(e.get("type") == "response.cancel" for e in self.voice.sent), 2)
        self.assertEqual(record["replies"][0]["status"], "cancelled")
        self.assertEqual((self.spoken, record["replies"][0]["delivery"]), ([], "unsent"))

    async def test_a_voice_that_goes_off_the_words_is_asked_once_more_and_the_second_try_is_said(self):
        self.voice = await Streaming.open("ws://unused", "voice", lambda: CLOCK)
        record = await self.turn(["Great! Let's get started.", DRAFT], task_service=T3SnapshotService,
                                 send_checked=self.send_checked)
        self.assertEqual((self.spoken, record["replies"][0]["delivery"]), ([DRAFT], "sent"))
        self.assertEqual(texts(self.voice), [prototype.relay_note(DRAFT), prototype.relay_note(DRAFT, 1)])
        attempts = [(e["attempt"], e["status"]) for e in record["timeline"] if e["kind"] == "t3_call_reply"]
        self.assertEqual(attempts, [(1, "cancelled"), (2, "completed")])

    async def test_a_reply_that_says_the_draft_is_never_stopped(self):
        self.voice = await Streaming.open("ws://unused", "voice", lambda: CLOCK)
        record = await self.turn([DRAFT], task_service=T3SnapshotService, send_checked=self.send_checked)
        self.assertEqual(sum(e.get("type") == "response.cancel" for e in self.voice.sent), 0)
        self.assertEqual((self.spoken, record["replies"][0]["delivery"]), ([DRAFT], "sent"))


class FloorTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_relay_cancelled_while_the_floor_is_held_leaves_nothing_in_the_session(self):
        session = await FakeSession.open("ws://unused", "voice", lambda: CLOCK)
        session.floor = asyncio.Event()
        task = asyncio.create_task(prototype.relay_draft(session, 24_000, 0, CHECKED.accept((STOP,), DRAFT,
                                                                                           "text_model").line))
        await asyncio.sleep(0)
        task.cancel()
        session.floor.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(texts(session), [], "the sound continued the turn, so its relay never reached the call")
        self.assertNotIn("response.create", [event.get("type") for event in session.sent])

    async def test_a_relay_whose_turn_ended_while_its_note_was_acknowledged_makes_no_response(self):
        """The note went in before the sound started; the floor closed while the server acknowledged it, and the
        sound then turned out to continue the turn."""
        muted = []

        class HeldDuringAck(FakeSession):
            async def send(self, **event):
                if event.get("type") == "conversation.item.create":
                    self.floor.clear()
                await super().send(**event)

        session = await HeldDuringAck.open("ws://unused", "voice", lambda: CLOCK)
        session.floor = asyncio.Event()
        session.floor.set()
        line = CHECKED.accept((STOP,), DRAFT, "text_model").line
        task = asyncio.create_task(prototype.relay_draft(session, 24_000, 0, line, abandoned=lambda: bool(muted)))
        while not texts(session):
            await asyncio.sleep(0)
        muted.append(True)
        task.cancel()
        session.floor.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertNotIn("response.create", [event.get("type") for event in session.sent])
