"""Live mode's "note" turn: what text the voice session is given, and that no reply is held back.

The recorded call these stand for is 20261116-152653. Its voice session was given a JSON list of the person's
earlier turns before every reply, and the model answered the last line of that list instead of the audio. These
tests stand in for llama.cpp, so they pin what the turn sends and speaks, not what the model then says.
"""

import asyncio
import socket
import threading
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from types import SimpleNamespace

from voice.live.facts import ungrounded
from voice.live.replycheck import ReplyCheck
from voice.prototype import run as prototype
from voice.prototype.tasks import MockTaskService
from voice.prototype.tests import isolate_decider_cache
from voice.prototype.tests.test_run_shared_session import CLOCK, FakeSession, Scripted, Source, heard_by
from voice.segmentation.tasks import Orchestrator
from voice.t3 import fakes as F
from voice.t3.acts import act
from voice.t3.acts import Kind as S
from voice.t3.speech import Rendered, Speech
from voice.t3.test_verify import Failing
from voice.t3.verify import Claim, Decider
from voice.prototype.tests.test_clarification import Decider as SegmentDecider, pick
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
        self.assertIn("Tell them you're checking.", note)
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
        self.assertIn("Ask them for it, and don't guess one.", note)
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

    async def turn(self, script, source=None, decider=None, **more):
        self.voice.script, self.voice.asked = script, 0
        return await prototype.run_clip(
            source or Source(), "c", False, "ws://unused", decider, 0.0, Path(self.tmp.name), {}, "hybrid", None, "gated",
            voice=self.voice, snapshot_mode="note", prior_utterances=EARLIER, final_decider=True, recall="note",
            speak=heard_by(self.spoken), **more)


class NoteTurnTest(NoteTurn):
    async def test_a_turn_with_no_lookup_gives_the_model_no_text(self):
        record = await self.turn(["Glad to hear it."])
        self.assertEqual(texts(self.voice), [], "the reply follows the turn's audio and nothing else")
        self.assertEqual(self.spoken, ["Glad to hear it."])
        self.assertEqual(record["replies"][0]["text"], "Glad to hear it.")

    async def test_a_turn_the_decider_reads_as_asking_for_nothing_is_still_answered(self):
        from voice.segmentation.tests.test_hybrid import FakeDecider, choice

        class AsksNothing(FakeDecider):
            def call(self, route, body, label=""):
                record = super().call(route, body, label)
                for name, question in body["questions"].items():
                    if name.endswith("_kind"):
                        record["response"]["answers"][name] = choice("chitchat", list(question["criteria"]))
                return record

        FakeSession.transcript = "Can you, hmm, so, well, a few things."
        decider = AsksNothing(joins=lambda words: False)
        record = await self.turn(["What's on your mind?"], decider=decider)
        self.assertTrue(decider.bodies, "the decider read the turn")
        self.assertEqual(self.spoken, ["What's on your mind?"], "a turn with no request is answered, not silent")
        self.assertEqual([t for t in record.get("tasks", ()) if t.get("status") == "dispatched"], [])

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


class PlaceTest(unittest.TestCase):
    def test_a_place_they_named_without_a_preposition_is_theirs(self):
        """20261116-152653 turn 22: "what about San Diego?" and a reply that said "in San Diego" was refused."""
        reply = "I'm checking the weather in San Diego."
        self.assertEqual(ungrounded(reply, ["Okay, and what about San Diego?"]), [])
        self.assertEqual([fact.surface for fact in ungrounded(reply, ["Okay, and what about Portland?"])],
                         ["San"])


CORRECTION = act(S.CORRECTION, thread="clamp helper", status="working")
CORRECTED = "Sorry, I got that wrong. The clamp helper thread is still working."


class CorrectingService(MockTaskService):
    """A T3 service that can say a correction, and holds one it cannot."""

    withdrawn_question = None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.news, self.own_lines, self.delivered, self.given_back = {}, {}, [], []
        self.t3 = SimpleNamespace(held=[], spoken=[], hold_news=lambda update: self.t3.held.append(update),
                                  spoke=lambda update: self.t3.spoken.append(update))

    def correction_entry(self, update):
        entry = {"type": "task_update", "task_id": "correction", "acts": [CORRECTION.to_json()], "row": "correction"}
        self.news[id(entry)] = update
        return entry

    def withdrawn(self):
        return self.withdrawn_question

    def say(self, entry, turns=()):
        return CHECKED.accept((CORRECTION,), CORRECTED, "text_model")

    def accept(self, entry, text, draft):
        return CHECKED.recite(draft, text)

    def refuse(self, entry, rendered):
        pass

    def record_delivery(self, entry, state):
        self.delivered.append(state)

    def withdrawn_entry(self, question):
        entry = {"type": "task_update", "task_id": "withdrawn", "acts": [CORRECTION.to_json()], "row": "withdrawn"}
        self.own_lines[id(entry)] = "withdrawn"
        return entry

    def give_back_withdrawn(self, question):
        self.given_back.append(question)


class ReplyCheckTurn(NoteTurn):
    """A free reply that says something T3 does not support is corrected after it, never held in front of it."""

    async def send_checked(self, line, pcm):
        self.spoken.append(line.text)
        self.events.append("correction spoken")
        return pcm, CLOCK

    def checker(self, **state):
        self.stopped = []
        self.events = []
        state = {"muted": False, "speaking": False, **state}
        return ReplyCheck(facts=lambda: ("t1", {"thread": "clamp helper"}), judge=lambda text, facts: Claim(0.9, 0.05),
                          correction=lambda thread, text: "update", playing=lambda: (1.0, 6.0),
                          muted=lambda: state["muted"], speaking=lambda: state["speaking"],
                          stop_playback=lambda: self.events.append("audio stopped"))

    async def test_a_wrong_reply_is_spoken_whole_then_its_rest_is_stopped_and_a_held_line_covers_the_correction(self):
        holding = "Hold on, let me double check that."
        record = await self.turn(["That thread will carry on now.", holding, CORRECTED], task_service=CorrectingService,
                                 send_checked=self.send_checked, reply_check=self.checker())
        self.assertEqual(self.spoken, ["That thread will carry on now.", holding, CORRECTED])
        self.assertEqual(self.events, ["audio stopped", "correction spoken"])
        self.assertEqual([r["kind"] for r in record["replies"]], ["snapshot", "transition", "correction"])
        self.assertEqual(texts(self.voice), [prototype.TRANSITION_NOTE, prototype.relay_note(CORRECTED)],
                         "the holding line is worded by the voice from the note's meaning, then the checked line "
                         "is relayed")
        self.assertEqual([e["action"] for e in record["timeline"] if e["kind"] == "claim_check"],
                         ["replaced the rest"])
        transition = next(e for e in record["timeline"] if e["kind"] == "reply" and e["reply"] == "transition")
        self.assertIn("flag_to_audio_s", transition)

    async def test_a_reply_the_check_finds_fine_gets_no_correction(self):
        check = self.checker()
        check.judge = lambda text, facts: Claim(0.1, 0.05)
        record = await self.turn(["It is waiting on you."], task_service=CorrectingService,
                                 send_checked=self.send_checked, reply_check=check)
        self.assertEqual(self.spoken, ["It is waiting on you."])
        self.assertEqual(self.voice.asked, 1)
        self.assertEqual([r["kind"] for r in record["replies"]], ["snapshot"])

    async def test_a_correction_while_the_caller_talks_is_held_for_a_quiet_moment_and_not_spoken(self):
        holder = {}

        class Holding(CorrectingService):
            def __init__(inner, *args, **kwargs):
                super().__init__(*args, **kwargs)
                holder["service"] = inner

        await self.turn(["That thread will carry on now."], task_service=Holding, send_checked=self.send_checked,
                        reply_check=self.checker(speaking=True))
        self.assertEqual(self.spoken, ["That thread will carry on now."])
        self.assertEqual(self.voice.asked, 1, "no holding line and no relay is made over the caller")
        self.assertEqual(holder["service"].t3.held, ["update"])
        self.assertEqual(self.events, [], "and the barge-in path is theirs, not ours")

    async def test_a_correction_that_is_not_heard_goes_back_to_be_said_when_the_call_is_quiet(self):
        async def unheard(line, pcm):
            return b"", None

        holder = {}

        class Unheard(CorrectingService):
            def __init__(inner, *args, **kwargs):
                super().__init__(*args, **kwargs)
                holder["service"] = inner

        record = await self.turn(["That thread will carry on now.", "Let me check.", CORRECTED], task_service=Unheard,
                                 send_checked=unheard, reply_check=self.checker())
        self.assertEqual(record["replies"][-1]["delivery"], "unsent")
        self.assertEqual(holder["service"].t3.spoken, [], "a line the caller never heard is not spoken of")


class Segments(SegmentDecider):
    """The decider's reading of the turn's segments: one, ready to answer now or not."""

    def __init__(self, ready):
        super().__init__(tool="none")
        self.ready = ready

    def call(self, route, body, label=""):
        out = super().call(route, body, label)
        for name, question in body["questions"].items():
            if name.split("_", 1)[1] == "ready":
                out["response"]["answers"][name] = pick(self.ready, question)
        return out


TAKEN_BACK = SimpleNamespace(took_back=True, candidates=("t1", "t2"),
                             titles={"t1": "Tidy the Save Slots", "t2": "Tidy the Save Menu"})


class WithdrawnReplyCheckTurn(ReplyCheckTurn):
    """The free reply on the turn that took a pick question back is held until the check reads it: one that offers the
    taken-back threads as choices again is never heard. A turn that asks nothing else hears the checked line that
    drops the question instead; one that asks something else is answered."""

    OFFER = "Which thread do you mean, Tidy the Save Slots or Tidy the Save Menu?"
    ANSWER = "Two plus two is four."

    async def withdrawing(self, script, reads, ready="clarify", service=None):
        """`reads` is the check's answers by reply, each a list taken in turn (None: it could not be read)."""
        asked = self.asked = []
        holder = self.holder = {}
        reads = {text: list(answers) for text, answers in reads.items()}

        class Dropped(service or CorrectingService):
            withdrawn_question = TAKEN_BACK

            def __init__(inner, *args, **kwargs):
                super().__init__(*args, **kwargs)
                holder["service"] = inner

        def reoffers(text, threads):
            asked.append(text)
            return reads[text].pop(0), 1.0

        check = self.checker()
        check.judge = lambda text, facts: Claim(0.0, 1.0)
        check.reoffers = reoffers
        return await self.turn(script, task_service=Dropped, send_checked=self.send_checked, reply_check=check,
                               decider=Segments(ready))

    async def test_a_reply_that_offers_the_choices_again_is_never_heard_and_a_checked_line_is_said_instead(self):
        record = await self.withdrawing([self.OFFER, CORRECTED], {self.OFFER: [0.9]})
        self.assertEqual(self.asked, [self.OFFER])
        self.assertEqual(self.spoken, [CORRECTED])
        self.assertEqual([(r["kind"], r.get("delivery")) for r in record["replies"]], [("snapshot", "refused"),
                                                                                         ("t3", "sent")])

    async def test_a_reply_that_does_not_offer_them_is_spoken_whole_once_read(self):
        record = await self.withdrawing(["Okay, no problem."], {"Okay, no problem.": [0.1]})
        self.assertEqual(self.spoken, ["Okay, no problem."])
        self.assertEqual([r["kind"] for r in record["replies"]], ["snapshot"])

    async def test_a_check_that_cannot_be_read_twice_counts_as_an_offer(self):
        await self.withdrawing(["Okay, no problem.", CORRECTED], {"Okay, no problem.": [None, None]})
        self.assertEqual(self.asked, ["Okay, no problem."] * 2, "an unreadable answer is asked once more")
        self.assertEqual(self.spoken, [CORRECTED])

    async def test_every_checked_line_refused_still_ends_in_words_the_caller_hears(self):
        drafted = []

        class Refusing(CorrectingService):
            def say(self, entry, turns=()):
                drafted.append(entry["task_id"])
                return Rendered(None, ["refused"], "a refused draft")

            def unanswered(self, *, has_audio=False):
                entry = {"type": "task_update", "task_id": "unanswered", "acts": [CORRECTION.to_json()]}
                return entry, self, lambda state: None

        sorry = "Sorry, I couldn't get you an answer just now."
        record = await self.withdrawing([self.OFFER, sorry], {self.OFFER: [0.9]}, service=Refusing)
        self.assertEqual(drafted, ["withdrawn", "withdrawn", "unanswered", "unanswered"],
                         "the line is drafted again, then the turn's own fallback is tried")
        self.assertEqual(self.spoken, [sorry])
        self.assertEqual(record["replies"][-1]["kind"], "no_answer")
        self.assertEqual(self.holder["service"].given_back, [])

    async def test_a_turn_that_also_asks_something_is_answered_without_the_choices(self):
        record = await self.withdrawing([self.OFFER, self.ANSWER], {self.OFFER: [0.9], self.ANSWER: [0.01]},
                                        ready="dispatch_now")
        self.assertEqual(self.spoken, [self.ANSWER])
        self.assertIn(prototype.REOFFERED_NOTE, texts(self.voice), "the note says why the reply was not said")
        self.assertNotIn("t3", [r["kind"] for r in record["replies"]], "no checked line takes the answer's place")

    async def test_a_check_that_fails_once_does_not_lose_the_answer_to_the_other_request(self):
        await self.withdrawing([self.ANSWER], {self.ANSWER: [None, 0.01]}, ready="dispatch_now")
        self.assertEqual(self.spoken, [self.ANSWER])

    async def test_an_unreadable_check_on_a_turn_that_asks_more_words_the_reply_again_rather_than_replace_it(self):
        again = "Sure, two plus two makes four."
        await self.withdrawing([self.ANSWER, again], {self.ANSWER: [None, None], again: [0.01]},
                               ready="dispatch_now")
        self.assertEqual(self.spoken, [again])
        self.assertIn(prototype.UNREAD_NOTE, texts(self.voice), "a reply the check could not read is asked for again")

    async def test_a_reply_refused_twice_is_replaced_by_the_checked_line_and_no_free_reply_follows_it(self):
        record = await self.withdrawing([self.OFFER, self.OFFER, CORRECTED], {self.OFFER: [0.9, 0.9]},
                                        ready="dispatch_now")
        self.assertEqual(self.spoken, [CORRECTED])
        self.assertEqual([(r["kind"], r.get("delivery")) for r in record["replies"]],
                         [("snapshot", "refused"), ("snapshot", "refused"), ("t3", "sent")])

    async def cut_off(self, blocking):
        """The turn is cancelled, as the live server cancels one the caller's next words continue, while `blocking`
        (`reoffers` or `say`) waits; nothing of it may be heard, and the withdrawal goes to the next turn."""
        reached, release = asyncio.Event(), threading.Event()
        self.addCleanup(release.set)
        loop = asyncio.get_running_loop()
        holder = {}

        def wait(result):
            loop.call_soon_threadsafe(reached.set)
            release.wait()
            return result

        class Cut(CorrectingService):
            withdrawn_question = TAKEN_BACK

            def __init__(inner, *args, **kwargs):
                super().__init__(*args, **kwargs)
                holder["service"] = inner

            def say(inner, entry, turns=()):
                return wait(super().say(entry, turns)) if blocking == "say" else super().say(entry, turns)

        check = self.checker()
        check.judge = lambda text, facts: Claim(0.0, 1.0)
        check.reoffers = lambda text, threads: wait((0.9, 1.0)) if blocking == "reoffers" else (0.9, 1.0)
        turn = asyncio.ensure_future(self.turn([self.OFFER, CORRECTED], task_service=Cut,
                                               send_checked=self.send_checked, reply_check=check,
                                               decider=Segments("clarify")))
        await reached.wait()
        turn.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await turn
        self.assertEqual(self.spoken, [])
        self.assertEqual(holder["service"].given_back, [TAKEN_BACK])

    async def test_the_caller_speaking_during_the_hold_hears_nothing_of_that_turn(self):
        await self.cut_off("reoffers")

    async def test_the_caller_speaking_while_the_checked_line_is_drafted_hears_nothing_of_that_turn(self):
        await self.cut_off("say")


class HoldDeadline(unittest.IsolatedAsyncioTestCase):
    """The hold ends at its deadline, the wait for the check's lock included, and a reply not read by then does not
    pass."""

    async def hold(self, reoffers):
        logged = []
        check = SimpleNamespace(reoffers=reoffers)
        hold = prototype.withdrawn_hold(TAKEN_BACK, check, object(), lambda kind, **fields: logged.append(fields),
                                        deadline_s=0)
        return await hold("Okay, no problem."), logged

    async def test_a_check_that_never_answers_does_not_pass_the_reply(self):
        release = threading.Event()
        self.addCleanup(release.set)
        passed, logged = await self.hold(lambda text, threads: (release.wait(), (0.0, 1.0))[1])
        self.assertFalse(passed)
        self.assertTrue(logged[0]["past_deadline"])

    async def test_a_check_held_up_by_its_lock_does_not_pass_the_reply(self):
        class Answers:
            def call(self, route, body, label=""):
                return {"status": 200, "response": {"answers": {"answers": {"type": "noul", "noul": 0.0}}}}

        decider = Decider(Answers())
        decider.lock.acquire()
        self.addCleanup(decider.lock.release)
        passed, logged = await self.hold(decider.reoffers)
        self.assertFalse(passed)
        self.assertTrue(logged[0]["past_deadline"])


class WithdrawnQuestionTurn(NoteTurn):
    async def test_a_dropped_question_tells_the_voice_to_answer_fresh_and_names_no_choice(self):
        class Dropped(CorrectingService):
            withdrawn_question = SimpleNamespace(took_back=True)

        record = await self.turn(["Sure, no problem."], task_service=Dropped)
        self.assertEqual(texts(self.voice), [prototype.WITHDRAWN_NOTE])
        self.assertNotIn("thread", prototype.WITHDRAWN_NOTE.lower())
        self.assertEqual(self.spoken, ["Sure, no problem."])

    async def test_any_other_dropped_question_keeps_the_note_it_always_had(self):
        class Dropped(CorrectingService):
            withdrawn_question = SimpleNamespace(took_back=False)

        await self.turn(["Sure, no problem."], task_service=Dropped)
        self.assertEqual(texts(self.voice), [prototype.DROPPED_NOTE])
        self.assertNotIn("took", prototype.DROPPED_NOTE)

    async def test_no_dropped_question_sends_no_note(self):
        await self.turn(["Sure, no problem."], task_service=CorrectingService)
        self.assertEqual(texts(self.voice), [])

    def relayed(self, about):
        asked = self.asked = []

        class Relayed(CorrectingService):
            def said_source(self, words=""):
                asked.append(words)
                return {"thread": "Tidy the Save Slots", "from": "reply", "about": about}

        return Relayed

    async def test_a_doubt_about_a_relayed_thread_line_is_told_where_that_line_came_from(self):
        await self.turn(["It came from the thread's own last reply."], task_service=self.relayed(True))
        note, = texts(self.voice)
        self.assertEqual(self.asked, [FakeSession.transcript], "the decider reads this turn's own words")
        self.assertIn("Tidy the Save Slots", note)
        self.assertIn("own last reply", note)

    def test_each_source_gets_its_own_note(self):
        notes = {kind: prototype.said_note({"thread": "Tidy the Save Slots", "from": kind, "about": True})
                 for kind in ("reply", "status", "news", "thread_question", "question")}
        for kind in ("reply", "status", "news", "thread_question"):
            self.assertIn(prototype.SAID_FROM[kind], notes[kind])
        self.assertEqual(notes["question"], prototype.ASKED_NOTE)
        self.assertNotIn("status", notes["question"])
        self.assertEqual(len(set(notes.values())), 5)

    async def test_a_turn_not_about_the_line_gets_a_note_that_names_no_thread(self):
        """The repeat call: a note naming the thread on every free turn had the voice say the line again, goodbye
        included."""
        await self.turn(["You're welcome, bye."], task_service=self.relayed(False))
        self.assertEqual(texts(self.voice), [prototype.FREE_NOTE])
        self.assertNotIn("thread", prototype.FREE_NOTE.lower())
        self.assertEqual(self.spoken, ["You're welcome, bye."])

    def voiced(self, ms):
        source = Source()
        source.voiced_ms = ms
        return source

    async def test_a_short_noise_with_a_clean_empty_transcript_says_nothing_and_is_answered_unheard(self):
        """The repeat call's turn 7: 1.5 s of audio, an empty transcript, and the thread line said again."""
        FakeSession.transcript = ""
        record = await self.turn(["The Tidy the Save Slots thread is still working."], source=self.voiced(120),
                                 task_service=self.relayed(True))
        self.assertEqual((self.spoken, record["replies"], self.asked), ([], [], []))
        self.assertEqual(texts(self.voice), [], "no note: nothing is asked of a turn with no words")
        self.assertEqual(self.voice.sent[-1].get("response", {}).get("output_modalities"), ["text"])

    async def test_voiced_speech_with_no_words_is_told_it_was_not_caught(self):
        FakeSession.transcript = ""
        for source in (self.voiced(1400), Source()):  # clear voiced audio, and a turn nothing measured
            with self.subTest(voiced_ms=getattr(source, "voiced_ms", None)):
                self.spoken = []
                record = await self.turn(["Sorry, I didn't catch that."], source=source, task_service=self.relayed(True))
                self.assertEqual(texts(self.voice)[-1], prototype.NOT_CAUGHT_NOTE)
                self.assertEqual(([r["kind"] for r in record["replies"]], self.spoken),
                                 (["not_caught"], ["Sorry, I didn't catch that."]))
                self.assertEqual(self.asked, [], "nothing about the thread line")

    async def test_a_transcript_that_never_arrived_is_told_it_was_not_caught(self):
        FakeSession.completes = False
        self.addCleanup(setattr, FakeSession, "completes", True)
        with patch.object(prototype, "ASR_FINAL_TIMEOUT_S", 0.05):
            record = await self.turn(["Sorry, I didn't catch that."], source=self.voiced(100),
                                     task_service=self.relayed(True))
        self.assertEqual([r["kind"] for r in record["replies"]], ["not_caught"])


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

    async def test_a_turn_with_no_delivered_line_says_a_checked_acknowledgment(self):
        fallback = "I couldn't give you an answer just now."
        deliveries = []

        class Unanswered(T3SnapshotService):
            def unanswered(self, *, has_audio):
                if has_audio:
                    return None
                acts = (act(S.UNANSWERED),)
                return {"acts": [a.to_json() for a in acts]}, SimpleNamespace(
                    say=lambda entry, turns: CHECKED.accept(acts, fallback, "text_model"),
                    accept=self.accept, refuse=self.refuse), lambda _: None

            def record_delivery(self, entry, state):
                deliveries.append((entry["task_id"], state))

        for draft, script in (
            ("I stopped the clamp helper thread.", ["Unheard.", fallback]),
            (DRAFT, ["I stopped it.", "I stopped it.", fallback]),
        ):
            with self.subTest(draft=draft):
                self.spoken.clear()
                deliveries.clear()
                Unanswered.draft = draft
                record = await self.turn(script, task_service=Unanswered, send_checked=self.send_checked)
                self.assertEqual(self.spoken, [fallback])
                self.assertEqual([r["delivery"] for r in record["replies"]], ["unsent", "sent"])
                self.assertEqual(deliveries, [("t3_1", "unsent")])

    async def test_fallback_respects_delivery_and_never_releases_failed_recitals(self):
        fallback = "I couldn't give you an answer."
        fallback_act = act(S.UNANSWERED)

        class WithFallback(T3SnapshotService):
            def unanswered(inner, *, has_audio):
                if has_audio:
                    return None
                return {"acts": [fallback_act.to_json()]}, SimpleNamespace(
                    say=lambda entry, turns: CHECKED.accept((fallback_act,), fallback, "text_model"),
                    accept=inner.accept, refuse=inner.refuse), lambda _: None

        async def send_partial(line, pcm):
            self.spoken.append(line.text)
            return pcm[:2], CLOCK

        for name, script, service, send, muted, heard, deliveries in (
            ("muted", [], WithFallback, self.send_checked, True, [], ["unsent"]),
            ("partial", [DRAFT], WithFallback, send_partial, False, [DRAFT], ["partial"]),
            ("no capability", ["Wrong.", "Wrong."], T3SnapshotService, self.send_checked, False, [], ["unsent"]),
        ):
            with self.subTest(name=name):
                self.spoken.clear()
                attempts = len(script)
                record = await self.turn(script, task_service=service, send_checked=send, is_muted=lambda: muted)
                self.assertEqual(self.spoken, heard)
                self.assertEqual([r["delivery"] for r in record["replies"]], deliveries)
                self.assertEqual(self.voice.asked, attempts)

        # Every checked line refused, the fallback's too: the voice says it has no answer in its own words, so the turn
        # is never silent, and that reply is told to say nothing about a thread.
        self.spoken.clear()
        record = await self.turn(["Wrong.", "Wrong.", "Wrong.", "Wrong.", "Sorry, I couldn't get that just now."],
                                 task_service=WithFallback, send_checked=self.send_checked)
        self.assertEqual([r["kind"] for r in record["replies"]], ["t3", "t3", "no_answer"])
        self.assertEqual([r.get("delivery") for r in record["replies"][:2]], ["unsent", "unsent"])
        self.assertEqual(self.spoken, ["Sorry, I couldn't get that just now."])
        self.assertIn(prototype.UNHEARD_NOTE, texts(self.voice))

        class NoOrdinaryAudio(Scripted):
            async def push(inner, event):
                if event["type"] != "response.output_audio.delta" or inner.asked > 1:
                    await super().push(event)

        class Ordinary(WithFallback):
            snapshot = MockTaskService.snapshot

        self.spoken.clear()
        self.voice = await NoOrdinaryAudio.open("ws://unused", "voice", lambda: CLOCK)
        record = await self.turn(["Ordinary text.", fallback], task_service=Ordinary, send_checked=self.send_checked)
        self.assertEqual(self.spoken, [fallback])
        self.assertEqual(record["replies"][-1]["delivery"], "sent")

    async def test_a_relay_that_ends_incomplete_retries_before_releasing_audio(self):
        choice = act(S.ASK_WHICH, of="thread", candidates=["Mend Rail Maps", "Add a Rail Legend"])
        draft = "Which thread do you mean, Mend Rail Maps or Add a Rail Legend?"

        refused = []

        class Choosing(asked_in_order({**T3_ENTRY, "acts": [choice.to_json()]})):
            def say(self, entry, turns=()):
                return CHECKED.accept((choice,), draft, "text_model")

            def refuse(self, entry, rendered):
                refused.append(rendered.raw)

        class Incomplete(Scripted):
            async def push(inner, event):
                if event["type"] == "response.done" and inner.asked == 1:
                    event["response"]["status"] = "incomplete"
                    event["response"]["status_details"] = {"reason": inner.reason}
                await super().push(event)

        for reason, expected in (("max_output_tokens", [draft]), ("turn_detected", [])):
            with self.subTest(reason=reason):
                self.spoken.clear()
                refused.clear()
                self.voice = await Incomplete.open("ws://unused", "voice", lambda: CLOCK)
                self.voice.reason = reason
                record = await self.turn([draft, draft], task_service=Choosing, send_checked=self.send_checked)
                self.assertEqual(self.spoken, expected)
                self.assertEqual(record["replies"][0]["delivery"], "sent" if expected else "unsent")
                self.assertEqual(self.voice.asked, 2 if expected else 1)
                self.assertEqual(refused, [None])

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
        first, retry = texts(self.voice)
        self.assertIn('as "I" to "you"', first)
        self.assertNotIn('as "I" to "you"', retry)
        self.assertIn("Reply with only the quoted words", retry)
        self.assertEqual([note.count(DRAFT) for note in (first, retry)], [1, 1])
        self.assertTrue(all(note.endswith(f'"{DRAFT}"]') for note in (first, retry)))
        wire = [event["type"] for event in self.voice.sent
                if event["type"] in ("response.cancel", "response.create", "conversation.item.create")]
        self.assertEqual(wire, ["conversation.item.create", "response.create", "response.cancel",
                                "conversation.item.create", "response.create"])
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
