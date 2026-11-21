"""The T3 task service confirms writes, resolves reads, and attaches evidence to thread updates."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from voice.live.t3_tasks import (APPROVE, ASK, CONFIRM_TTL_S, DENY, DESCRIBE_REQUEST, START, STOP, T3_TOOLS, T3_WAITING, TELL, Script, T3Registry, T3TaskService,
                                 T3Tasks, status_act, tell_parts, transcript_rows)
from voice.prototype.run import say_acts, task_note
from voice.prototype.tests.test_run_shared_session import Scripted
from voice.t3 import fakes as F
from voice.t3 import lifecycle as L
from voice.t3 import types as T
from voice.t3.acts import QUESTIONS, RULES, Act, act, check
from voice.t3.acts import Kind as S
from voice.t3.journal import Kind, Status, T3Journal
from voice.t3.speech import RENDER_SYSTEM, Lane, Speech, kept_exact
from voice.t3.test_replies import Recorded
from voice.t3.verify import ELSEWHERE, Decider, Picked
from voice.segmentation.gate import PLAN_A
from voice.tools import registry as tool_registry

TWO_PROJECTS = (("p1", "Voice sandbox", ("sandbox", "voice sandbox")),
                ("p2", "Voice server", ("voice server", "the server")))
THREADS = (("t5", "Add a clamp helper"), ("t6", "Fix the flaky ASR test"), ("t7", "Upgrade the toolchain"),
           ("t8", "Write the release notes"))


class ReplayReading(F.Reader):
    """The speech decider, with reply readings replayed from the recorded d1.2 answers (voice/t3/test_replies.py)."""

    def __init__(self):
        super().__init__({})
        self.reader = Decider(Recorded())

    def reads(self, said, reply, options=()):
        return self.reader.reads(said, reply, options)


# How the scripted decider reads the replies these tests say. Anything else reads as unrelated, which answers nothing.
READINGS = {"yes": "yes", "Yes.": "yes", "Yes, send it.": "yes", "Yes, stop it.": "yes", "Yes, start it.": "yes",
            "Yes, go ahead.": "yes", "No.": "no", "No, don't.": "no", "No, never mind.": "no",
            "No, the toolchain one.": "no", "What do you mean?": "question", "The voice server.": "yes",
            "yes, start it in the voice server": "other", "start it in the other project": ("other", "Voice server"),
            "yes please": "yes"}


def tasks_over(test: unittest.TestCase, seam: F.FakeSeam, model=None, path: Path | None = None,
               decider=None) -> T3Tasks:
    if path is None:
        tmp = tempfile.TemporaryDirectory()
        test.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "journal.json"
    return T3Tasks(L.Follow(T3Journal(path), F.Connector(seam)), model, decider or F.Reader(READINGS))


def said(update) -> list[tuple]:
    """Each act as its kind, the named facts its line must say, and its status, op or reason."""
    return [(a.kind.value, *[a.slots[n] for n in RULES[a.kind].named if n in a.slots],
             *[a.slots[n] for n in ("status", "op", "reason") if n in a.slots]) for a in update.acts]


class FakeText:
    """The voice's text model: a line built from the acts' exact facts, which passes the slot check."""

    model = "fake/text:free"
    GIST = "It added the helper."

    def __init__(self, line=None):
        self.line, self.calls = line, 0
        self.summarised = threading.Event()

    def complete(self, messages):
        self.calls += 1
        self.seen = [*getattr(self, "seen", []), *messages]
        if messages[0]["content"] != RENDER_SYSTEM:
            self.summarised.set()
            return {"text": self.GIST, "ms": 1, "tokens": {}}
        body = json.loads(messages[1]["content"])
        if self.line is not None:
            return {"text": self.line, "ms": 1, "tokens": {}}
        return {"text": self.wording(body["keep_exact"], body["ask"]), "ms": 1, "tokens": {}}

    @staticmethod
    def wording(exact, ask):
        facts = ", ".join(exact) or "your threads"
        return f"About {facts}, any thoughts?" if ask else f"About {facts}, noted."


def worded(t3: T3Tasks, text: FakeText) -> T3Tasks:
    t3.lane = Lane(text)
    t3.speech = Speech(t3.lane, t3._read_quiet, F.Agree())
    return t3


def listing(*rows, truncated=False, as_of=77):
    threads = tuple(T.ThreadSummary(tid, "p1", title, T.ThreadActivity.WORKING, "2026-11-17T10:00:00Z", False)
                    for tid, title in rows)
    return T.ThreadList(threads, len(threads) + (5 if truncated else 0), truncated, as_of)


def busy_seam(read_as_of=150) -> F.FakeSeam:
    seam = F.FakeSeam(read=F.state(thread="t5", title="Add a clamp helper", as_of=read_as_of))
    seam.listing = listing(*THREADS)
    return seam.answer("send", F.busy())


def held_follow_up(test, seam, path=None) -> T3Tasks:
    t3 = tasks_over(test, seam, path=path)
    t3.about("c1", TELL, "Tell the clamp helper thread to also add a test.", 0)
    test.assertEqual(said(t3.answer("Yes, send it.", 1)), [("held_followup", "Add a clamp helper")])
    return t3


class TwinA(unittest.TestCase):
    def test_a_launch_without_a_named_project_defaults_to_a_confirmation(self):
        seam = F.FakeSeam()
        t3 = tasks_over(self, seam, decider=ReplayReading())
        request = "Start a thread to fix the flaky test"
        asked = t3.start("k1", request, 0)
        self.assertEqual((said(asked), asked.status), ([("confirm_launch", "Voice sandbox")], "needs_clarification"))
        self.assertEqual(seam.ops("launch"), [])
        self.assertEqual(said(t3.answer("Please do.", 1)), [("launched", "Voice sandbox")])
        self.assertEqual(seam.ops("launch")[0][2:], ("p1", request))

    UNLISTED = "Start a thread for a little project that makes game exports cleaner, with long pauses and hedging."

    def test_an_unmatched_project_with_one_available_is_asked_as_a_yes_or_no(self):
        """The 2026-11-18 call: the request said "project" but named none, and the voice only stated that nothing
        matched, so the person's "please do" had nothing to answer."""
        seam = F.FakeSeam()
        t3 = tasks_over(self, seam, decider=ReplayReading())
        asked = t3.start("k1", self.UNLISTED, 0)
        self.assertEqual((said(asked), asked.status), ([("confirm_launch", "Voice sandbox")], "needs_clarification"))
        self.assertEqual(seam.ops("launch"), [])
        self.assertEqual(said(t3.answer("Please do?", 1)), [("launched", "Voice sandbox")])
        self.assertEqual(seam.ops("launch")[0][2:], ("p1", self.UNLISTED))

    def test_a_no_to_the_only_available_project_starts_nothing(self):
        seam = F.FakeSeam()
        t3 = tasks_over(self, seam)
        t3.start("k1", self.UNLISTED, 0)
        self.assertEqual(said(t3.answer("No, never mind.", 1)), [("cancelled", "start")])
        self.assertEqual(seam.ops("launch"), [])
        self.assertIsNone(t3.question)

    def test_an_unrelated_reply_to_the_only_available_project_drops_the_question_without_asking_again(self):
        seam = F.FakeSeam()
        t3 = tasks_over(self, seam)
        t3.start("k1", self.UNLISTED, 0)
        self.assertIsNone(t3.answer("What's the weather like?", 1))
        self.assertIsNone(t3.question)
        self.assertIsNone(t3.answer("Yes, please do.", 2), "a yes after an unrelated turn answers nothing")
        self.assertEqual(seam.ops("launch"), [])

    def test_an_unmatched_project_with_several_available_asks_which_and_the_answer_is_confirmed(self):
        seam = F.FakeSeam(projects=TWO_PROJECTS)
        t3 = tasks_over(self, seam)
        asked = t3.start("k1", self.UNLISTED, 0)
        self.assertEqual((said(asked), asked.status),
                         ([("ask_which", ["Voice sandbox", "Voice server"])], "needs_clarification"))
        self.assertEqual(said(t3.answer("The voice server.", 1)), [("confirm_launch", "Voice server")])
        self.assertEqual(seam.ops("launch"), [])
        self.assertEqual(said(t3.answer("Yes.", 2)), [("launched", "Voice server")])
        self.assertEqual(seam.ops("launch")[0][2:], ("p2", self.UNLISTED))

    def test_an_unmatched_project_with_none_available_says_so_and_waits_for_nothing(self):
        t3 = tasks_over(self, F.FakeSeam(projects=()))
        update = t3.start("k1", self.UNLISTED, 0)
        self.assertEqual(([a.kind.value for a in update.acts], update.status), (["no_project"], "failed"))
        self.assertEqual(update.acts[0].slots, {})
        self.assertIsNone(t3.question)

    def test_the_decider_reads_the_shapes_of_yes_and_nothing_else_launches(self):
        yes = ["Please do.", "Please do?", "Yeah, go ahead.", "Sure.", "Yes, do it.", "Go for it.", "Yep."]
        never = ["No.", "Wait.", "What do you mean?", "How long will it take?", "Start it in the voice server instead.",
                 "Yes, but in the server project.", "What's the weather like?", "Maybe later.", "Thanks."]
        for reply, launches in [(r, True) for r in yes] + [(r, False) for r in never]:
            with self.subTest(reply=reply):
                seam = F.FakeSeam()
                t3 = tasks_over(self, seam, decider=ReplayReading())
                t3.start("k1", "Add a clamp helper in the sandbox project", 0)
                t3.answer(reply, 1)
                self.assertEqual(len(seam.ops("launch")), int(launches))

    def test_a_yes_with_a_changed_target_is_not_a_yes_and_never_launches_the_old_project(self):
        seam = F.FakeSeam(projects=TWO_PROJECTS)
        t3 = tasks_over(self, seam, decider=ReplayReading())
        t3.start("k1", "Add a clamp helper in the sandbox project", 0)
        again = t3.answer("The other project please.", 1)
        self.assertEqual(seam.ops("launch"), [], "the old project was not committed before the target was chosen")
        self.assertEqual(said(again), [("confirm_launch", "Voice server")], "the retarget asks its own question")

    def test_a_no_that_points_at_the_only_other_project_asks_about_that_one(self):
        seam = F.FakeSeam(projects=TWO_PROJECTS)
        t3 = tasks_over(self, seam, decider=ReplayReading())
        t3.start("k1", "Add a clamp helper in the sandbox project", 0)
        self.assertEqual(said(t3.answer("No, the other one.", 1)), [("confirm_launch", "Voice server")])
        self.assertEqual(seam.ops("launch"), [])

    def test_a_change_with_several_other_projects_asks_which(self):
        seam = F.FakeSeam(projects=(*TWO_PROJECTS, ("p3", "Docs site", ("docs site",))))
        t3 = tasks_over(self, seam, decider=ReplayReading())
        t3.start("k1", "Add a clamp helper in the sandbox project", 0)
        again = t3.answer("The other one.", 1)
        self.assertEqual(said(again), [("ask_which", ["Voice server", "Docs site"])])
        self.assertEqual((t3.question.kind, seam.ops("launch")), ("project", []))

    def test_without_a_decider_that_reads_no_reply_acts(self):
        seam = F.FakeSeam()
        t3 = tasks_over(self, seam, decider=F.Agree())
        t3.start("k1", "Add a clamp helper in the sandbox project", 0)
        self.assertIsNone(t3.answer("Yes.", 1))
        self.assertEqual(seam.ops("launch"), [])

    def test_a_yes_whose_change_the_decider_cannot_place_launches_nothing(self):
        seam = F.FakeSeam(projects=TWO_PROJECTS)
        t3 = tasks_over(self, seam, decider=ReplayReading())
        t3.start("k1", "Add a clamp helper in the sandbox project", 0)
        self.assertIsNone(t3.answer("Yes, but in the other project.", 1))
        self.assertEqual(seam.ops("launch"), [])

    def test_a_conditional_yes_launches_nothing(self):
        for reply in ("Yes, but only after the tests pass.", "Okay, if it's quick.", "Yeah, but make it about the build instead."):
            with self.subTest(reply=reply):
                seam = F.FakeSeam()
                t3 = tasks_over(self, seam, decider=ReplayReading())
                t3.start("k1", "Add a clamp helper in the sandbox project", 0)
                self.assertIsNone(t3.answer(reply, 1))
                self.assertEqual(seam.ops("launch"), [])

    def test_a_reply_the_decider_cannot_read_answers_nothing(self):
        class Silent(F.Agree):
            def reads(self, said, reply, options=()):
                from voice.t3.verify import Reading as Read
                return Read(None, error="TimeoutError: timed out")

        seam = F.FakeSeam()
        t3 = tasks_over(self, seam, decider=Silent())
        t3.start("k1", "Add a clamp helper in the sandbox project", 0)
        self.assertIsNone(t3.answer("Please do?", 1))
        self.assertEqual(seam.ops("launch"), [], "a reply nothing can read answers nothing, and the question is gone")
        self.assertIsNone(t3.answer("Yes, go ahead.", 2))

    def test_a_lone_match_is_asked_back_before_any_launch(self):
        seam = F.FakeSeam()
        t3 = tasks_over(self, seam)
        asked = t3.start("k1", "Add a clamp helper in the sandbox project", 0)
        self.assertEqual((said(asked), asked.status), ([("confirm_launch", "Voice sandbox")], "needs_clarification"))
        self.assertEqual(asked.sequences, (1,))
        self.assertEqual(seam.ops("launch"), [])
        started = t3.answer("Yes, go ahead.", 1)
        self.assertEqual(said(started), [("launched", "Voice sandbox")])
        self.assertEqual((started.status, started.sequences, started.thread_id), ("running", (93,), "t1"))
        self.assertEqual(seam.ops("launch")[0][2:], ("p1", "Add a clamp helper in the sandbox project"))
        self.assertEqual(seam.ops("watch"), [("watch", "t1", 93, "r1")])
        self.assertEqual(t3.take(), [])

    def test_no_starts_nothing(self):
        seam = F.FakeSeam()
        t3 = tasks_over(self, seam)
        t3.start("k1", "Add a clamp helper in the sandbox project", 0)
        self.assertEqual(said(t3.answer("No, don't.", 1)), [("cancelled", "start")])
        self.assertEqual(seam.ops("launch"), [])
        self.assertIsNone(t3.question)

    def test_an_unclear_reply_drops_the_request_and_is_never_asked_again(self):
        seam = F.FakeSeam()
        t3 = tasks_over(self, seam)
        t3.start("k1", "Add a clamp helper in the sandbox project", 0)
        self.assertIsNone(t3.answer("What do you mean?", 1))
        self.assertIsNone(t3.answer("yes", 2))
        self.assertEqual(seam.ops("launch"), [])

    def test_a_confirmation_can_be_retargeted_but_still_needs_a_yes(self):
        for reply in ("start it in the other project", "yes, start it in the voice server"):
            with self.subTest(reply=reply):
                seam = F.FakeSeam(projects=TWO_PROJECTS)
                t3 = tasks_over(self, seam)
                t3.start("k1", "Add a clamp helper in the sandbox project", 0)

                again = t3.answer(reply, 1)
                self.assertEqual((said(again), again.status), ([("confirm_launch", "Voice server")],
                                                               "needs_clarification"))
                self.assertEqual(seam.ops("launch"), [])
                self.assertEqual(said(t3.answer("yes please", 2)), [("launched", "Voice server")])
                self.assertEqual(seam.ops("launch")[0][2], "p2")

    def test_a_project_named_in_the_answer_is_still_confirmed(self):
        seam = F.FakeSeam(projects=TWO_PROJECTS)
        t3 = tasks_over(self, seam)
        asked = t3.start("k1", "Start a thread that fixes the flaky test", 0)
        self.assertEqual(said(asked), [("ask_which", ["Voice sandbox", "Voice server"])])
        again = t3.answer("The voice server.", 1)
        self.assertEqual(said(again), [("confirm_launch", "Voice server")])
        self.assertEqual(seam.ops("launch"), [])
        self.assertEqual(said(t3.answer("yes", 2)), [("launched", "Voice server")])
        self.assertEqual(seam.ops("launch")[0][2], "p2")

    def test_a_refused_launch_says_why(self):
        seam = F.FakeSeam().answer("launch", F.refusal("refused", "T3 refused it.", reason="Model is off."))
        t3 = tasks_over(self, seam)
        t3.start("k1", "Add a clamp helper in the sandbox project", 0)
        failed = t3.answer("yes", 1)
        self.assertEqual((said(failed), failed.status), ([("refused", "start", "Model is off.")], "failed"))

    def test_a_dead_seam_says_so_and_launches_nothing(self):
        t3 = tasks_over(self, F.FakeSeam())
        t3.follow = L.Follow(t3.follow.journal, F.Connector(T.UPGRADE_404))
        update = t3.start("k1", "Add a clamp helper in the sandbox project", 0)
        self.assertEqual((said(update), update.status), ([("seam_down", "seam_off")], "failed"))




class Wordings:
    """A text model that answers renders from a script, one wording per request, and keeps each request body."""

    model = "fake/text:free"

    def __init__(self, *lines):
        self.lines, self.bodies = list(lines), []

    def complete(self, messages):
        self.bodies.append(json.loads(messages[1]["content"]))
        return {"text": self.lines.pop(0)}


class Retries(unittest.TestCase):
    def test_a_refused_act_is_asked_again_differently_each_time(self):
        """Measured in the smoke run: a retry sent the request its refusal came from, so the model's cache gave the
        same line back, the decider's cache the same refusal, and the act never left the journal."""
        refused = "I've stopped Add a clamp helper."
        text = Wordings(refused, refused, "Want me to stop Add a clamp helper?")
        t3 = worded(tasks_over(self, F.FakeSeam()), FakeText())
        t3.speech.lane = Lane(text)
        update = L.Update("k1", (act(S.CONFIRM_STOP, thread="Add a clamp helper"),), "needs_clarification")
        self.assertEqual([t3.say(update).text for _ in range(3)], [None, None, "Want me to stop Add a clamp helper?"])
        self.assertEqual(len({json.dumps(body, sort_keys=True) for body in text.bodies}), 3,
                         "no retry repeats an earlier request")
        self.assertEqual([body.get("refused wordings") for body in text.bodies], [None, [refused], [refused]])
        restored = L.Update.from_json(json.loads(json.dumps(update.to_json())))
        self.assertEqual((restored.tries, restored.refused), (2, (refused,)), "a restart keeps counting")


class RefusesReplies(FakeText):
    """The text model, except that every wording of a replied act fails the slot check, so it waits."""

    def complete(self, messages):
        if messages[0]["content"] == RENDER_SYSTEM and any(
                a["act"] == "replied" for a in json.loads(messages[1]["content"])["acts"]):
            return {"text": ""}
        return super().complete(messages)


class Superseded(unittest.TestCase):
    """PM 2, 2026-11-18: nothing about a thread is ever spoken after a later act about that thread."""

    def service(self, t3):
        return T3TaskService(SimpleNamespace(records=lambda: [], dispatch_log=[]), "conv_turn_001", 0.0, None,
                             holds_side_effects=False, t3=t3)

    def waiting_reply(self, path):
        seam = F.FakeSeam()
        t3 = worded(tasks_over(self, seam, path=path), RefusesReplies())
        t3.start("a1", "Add a clamp helper in the sandbox project", 0)
        t3.ack_news(t3.answer("yes", 1))
        seam.push(F.reply(150, text="I added clamp next to sum. The tests pass."))
        t3.poll(2)
        turn = self.service(t3)
        entry, = turn.take_updates()
        self.assertIsNone(turn.say(entry).line, "the reply's wording is refused, so it waits")
        turn.close()
        return seam, t3

    def kinds(self, entries):
        return [[a["kind"] for a in entry["acts"]] for entry in entries]

    def test_a_waiting_reply_followed_by_done_speaks_only_done(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        seam, t3 = self.waiting_reply(Path(tmp.name) / "journal.json")
        seam.push(F.done(160))
        t3.poll(3)
        self.assertEqual(self.kinds(self.service(t3).take_updates()), [["done"]])
        self.assertFalse(t3.has_news(idle=True), "the waiting reply is never said after its thread's done")
        replied, = [u for u in t3.follow.journal.pending_lines if u.acts[0].kind is S.REPLIED]
        self.assertTrue(replied.superseded, "kept in the journal, marked, never deleted")

    def test_a_superseded_act_survives_a_restart_and_is_not_replayed(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "journal.json"
        seam, t3 = self.waiting_reply(path)
        seam.push(F.done(160))
        t3.poll(3)
        saved = T3Journal(path)
        self.assertEqual([(u.acts[0].kind, u.superseded) for u in saved.pending_lines],
                         [(S.REPLIED, True), (S.DONE, False)])
        restarted = worded(tasks_over(self, F.FakeSeam(), path=path), FakeText())
        self.assertEqual([[a.kind for a in u.acts] for u in restarted.take()], [[S.DONE]])

    def test_a_reply_in_flight_when_done_arrives_is_never_given_back(self):
        seam = F.FakeSeam()
        t3 = worded(tasks_over(self, seam), RefusesReplies())
        t3.start("a1", "Add a clamp helper in the sandbox project", 0)
        t3.ack_news(t3.answer("yes", 1))
        seam.push(F.reply(150, text="I added clamp next to sum. The tests pass."))
        t3.poll(2)
        turn = self.service(t3)
        entry, = turn.take_updates()
        seam.push(F.done(160))
        t3.poll(3)
        self.assertIsNone(turn.say(entry).line)
        turn.close()
        self.assertEqual(self.kinds(self.service(t3).take_updates()), [["done"]],
                         "a later act arrived while the reply was being worded, so the reply is not retried")


class TwinB(unittest.TestCase):
    def test_a_lone_pick_is_asked_back_and_a_yes_reads_it(self):
        seam = F.FakeSeam(read=F.state(thread="t5", title="Add a clamp helper", as_of=140,
                                       activity=T.ThreadActivity.FINISHED,
                                       last_reply=T.LastReply("Done. I added clamp. Tests pass.", "m1", None,
                                                              "r1", "2026-11-17T10:00:00Z")))
        seam.listing = listing(("t5", "Add a clamp helper"), ("t6", "Fix the flaky ASR test"))
        t3 = tasks_over(self, seam)
        asked = t3.about("k2", ASK, "How is the clamp helper thread going?", 0)
        self.assertEqual((said(asked), asked.sequences), ([("confirm_thread", "Add a clamp helper")], (77,)))
        self.assertEqual(seam.ops("read"), [])
        update = t3.answer("yes", 1)
        self.assertEqual(said(update), [("thread_status", "Add a clamp helper", "finished")])
        self.assertEqual((update.sequences, update.status), ((140,), "completed"))
        self.assertEqual(seam.ops("watch"), [])

    def test_none_depends_on_truncation(self):
        for truncated, expected in ((False, act(S.NO_THREAD)), (True, act(S.NO_THREAD, recent=1))):
            seam = F.FakeSeam()
            seam.listing = listing(("t6", "Fix the flaky ASR test"), truncated=truncated)
            update = tasks_over(self, seam).about("k2", ASK, "How is the billing thread going?", 0)
            self.assertEqual((update.acts, update.sequences), ((expected,), (77,)))

    def test_a_tie_asks_and_the_answer_reads(self):
        seam = F.FakeSeam(read=F.state(thread="t6", title="Fix the flaky ASR test", as_of=150))
        seam.listing = listing(("t5", "Fix the flaky UI test"), ("t6", "Fix the flaky ASR test"))
        t3 = tasks_over(self, seam)
        asked = t3.about("k2", ASK, "How is the flaky test thread going?", 0)
        self.assertEqual(asked.status, "needs_clarification")
        self.assertIn("Fix the flaky ASR test", asked.acts[0].slots["candidates"])
        answered = t3.answer("The ASR one.", 1)
        self.assertEqual((said(answered), answered.sequences),
                         ([("thread_status", "Fix the flaky ASR test", "working")], (150,)))

    def flaky(self, readings=None, picking=None, *more):
        seam = F.FakeSeam(read=F.state(thread="t6", title="Fix the flaky ASR test", as_of=150))
        seam.listing = listing(("t5", "Fix the flaky UI test"), ("t6", "Fix the flaky ASR test"), *more)
        t3 = tasks_over(self, seam, decider=F.Reader(readings or {}, picking))
        return seam, t3, t3.about("k2", ASK, "How is the flaky test thread going?", 0)

    def test_which_thread_is_never_asked_twice(self):
        """A reply that chooses none, or that asks for another thread in words fitting both again, drops the question."""
        for reply, label in (("Hold on.", "unrelated"), ("What are those?", "question"), ("None of those.", "no"),
                             ("The flaky test.", ("other", ELSEWHERE))):
            with self.subTest(reply=reply):
                seam, t3, asked = self.flaky({reply: label})
                self.assertEqual(asked.status, "needs_clarification")

                self.assertIsNone(t3.answer(reply, 1))

                self.assertIsNone(t3.live_question())
                self.assertEqual(seam.ops("read"), [])

    def test_which_thread_takes_the_one_the_decider_reads(self):
        """ "The second one" names no title: the decider's choice among the titles it was given is the answer."""
        seam, t3, asked = self.flaky({"The second one.": ("other", "Fix the flaky ASR test")})

        answered = t3.answer("The second one.", 1)

        self.assertEqual(t3.decider.asked[-1][2], tuple(asked.acts[0].slots["candidates"]))
        self.assertEqual(said(answered), [("thread_status", "Fix the flaky ASR test", "working")])

    def test_which_thread_answered_with_another_thread_confirms_that_one(self):
        seam, t3, _ = self.flaky({"I was after the checkout button one.": ("other", ELSEWHERE)},
                                 {"I was after the checkout button one.": "t7"},
                                 ("t7", "Add Checkout Button"))

        again = t3.answer("I was after the checkout button one.", 1)

        self.assertEqual(again.acts, (act(S.CONFIRM_THREAD, thread="Add Checkout Button"),))
        self.assertEqual(seam.ops("read"), [])

    def test_threads_with_one_title_are_one_choice(self):
        """Three threads called the same thing cannot be told apart by voice: the newest is confirmed, not listed."""
        seam = F.FakeSeam(read=F.state(thread="t3", title="Add Clamp Helper", as_of=150))
        seam.listing = listing(("t3", "Add Clamp Helper"), ("t2", "Add clamp helper"), ("t1", "Add Clamp Helper"),
                               ("t0", "Add Sum Helper"))
        t3 = tasks_over(self, seam)

        asked = t3.about("k2", ASK, "How is the clamp helper thread going?", 0)

        self.assertEqual((asked.acts, asked.thread_id), ((act(S.CONFIRM_THREAD, thread="Add Clamp Helper"),), "t3"))
        self.assertEqual(t3.answer("yes", 1).thread_id, "t3")

    def test_a_read_names_a_pending_need(self):
        state = F.state(activity=T.ThreadActivity.WAITING_ON_YOU, pending=F.need(prompt="Run the tests?"))
        self.assertEqual(status_act(state), act(S.THREAD_STATUS, thread="Fix the flaky test", status="waiting_on_you",
                                                need="command_approval", pending="Run the tests?",
                                                waiting_for="approval"))


class BlockingPicks(F.Reader):
    """A decider whose pick waits until the test lets it go."""

    def __init__(self, picking):
        super().__init__(READINGS, picking)
        self.entered, self.release = threading.Event(), threading.Event()

    def picks(self, request, threads, recent=None):
        self.entered.set()
        self.release.wait(5)
        return super().picks(request, threads, recent)


class Silent(F.Reader):
    """A decider that gives no pick: a timeout, a spend stop, a dropped connection."""

    def picks(self, request, threads, recent=None):
        self.picked.append((request, tuple(threads), recent))
        return Picked(None, error="TimeoutError: timed out")


class Summarisers(unittest.TestCase):
    """The model that writes thread summaries can be changed while the server runs."""

    class Text:
        def __init__(self, model):
            self.model = model

        def complete(self, messages):
            return {"text": self.model, "ms": 1, "tokens": {}}

    def test_choosing_one_swaps_the_model_behind_the_lane_and_an_unknown_one_changes_nothing(self):
        first, second = self.Text("first:free"), self.Text("second:free")
        t3 = tasks_over(self, F.FakeSeam(), first)
        t3.summarisers = {"first:free": ("First", lambda: first), "second:free": ("Second", lambda: second)}
        self.assertEqual(t3.summariser(), "first:free")
        self.assertTrue(t3.use_summariser("second:free"))
        self.assertEqual((t3.summariser(), t3.lane.submit([]).result(timeout=2)["text"]), ("second:free", "second:free"))
        self.assertFalse(t3.use_summariser("third"))
        self.assertEqual(t3.summariser(), "second:free")

    def test_a_server_that_started_with_no_text_model_gets_one_when_chosen(self):
        t3 = tasks_over(self, F.FakeSeam())
        t3.summarisers = {"first:free": ("First", lambda: self.Text("first:free"))}
        self.assertIsNone(t3.summariser())
        self.assertTrue(t3.use_summariser("first:free"))
        self.assertIs(t3.speech.lane, t3.lane)
        self.assertEqual(t3.summariser(), "first:free")

    def test_the_local_model_is_offered_only_when_its_server_is_up(self):
        from voice.live.t3_tasks import LOCAL_SUMMARISER, summariser_choices

        with patch.dict("os.environ", {"OPENROUTER_API_KEY": "", "VOICE_ENV_FILE": ""}):
            self.assertEqual(list(summariser_choices(None)), [])
            self.assertEqual(list(summariser_choices("http://127.0.0.1:1")), [LOCAL_SUMMARISER])


class DeciderPick(unittest.TestCase):
    """The decider picks the thread a request means. No title has to match a word of it."""

    MISS = "How is the clamping work coming along?"

    def seam(self):
        seam = F.FakeSeam(read=F.state(thread="t7", title="Upgrade the toolchain", as_of=160))
        seam.listing = listing(*THREADS)
        return seam

    def over(self, seam, picking=None, decider=None):
        return tasks_over(self, seam, decider=decider or F.Reader(READINGS, picking or {self.MISS: "t5"}))

    def test_a_pick_it_leans_to_is_asked_back(self):
        seam = self.seam()
        t3 = self.over(seam)
        asked = t3.about("k3", ASK, self.MISS, 0)
        self.assertEqual((said(asked), asked.status), ([("confirm_thread", "Add a clamp helper")],
                                                       "needs_clarification"))
        request, threads, recent = t3.decider.picked[0]
        self.assertEqual((request, [t.title for t in threads], recent), (self.MISS, [t for _, t in THREADS], None))
        self.assertEqual(seam.ops("read"), [])
        self.assertEqual(said(t3.answer("Yes.", 1)), [("thread_status", "Upgrade the toolchain", "working")])
        self.assertEqual(seam.ops("read"), [("read", "t5")])

    def test_a_pick_it_is_sure_of_answers_a_question_at_once(self):
        """Only a question, only when very sure, and never when another thread has the title."""
        twin = ("t9", "Add a clamp helper")
        for op, share, rows, reads in ((ASK, 0.95, (), True), (ASK, 0.85, (), False), (STOP, 0.95, (), False),
                                       (ASK, 0.95, (twin,), False)):
            with self.subTest(op=op, share=share, rows=rows):
                seam = self.seam()
                seam.listing = listing(*THREADS, *rows)
                update = self.over(seam, {self.MISS: {"t5": share}}).about("k3", op, self.MISS, 0)
                self.assertEqual(update.status == "needs_clarification", not reads)
                self.assertEqual(seam.ops("read"), [("read", "t5")] if reads else [])

    def test_a_message_or_a_stop_for_a_title_two_threads_share_is_not_offered(self):
        """The question could only name the title, so a yes to it could act on the wrong one of them."""
        for op, words in ((STOP, "Stop the clamp helper thread."), (TELL, "Tell the clamp helper thread to rerun.")):
            with self.subTest(op=op):
                seam = self.seam()
                seam.listing = listing(*THREADS, ("t9", "Add a Clamp Helper"))
                t3 = self.over(seam, {words: {"t5": 0.97}, "the clamp helper thread": {"t5": 0.97}})
                update = t3.about("k3", op, words, 0)
                self.assertEqual([a.kind for a in update.acts], [S.SAME_TITLE])
                self.assertEqual((update.acts[0].slots["thread"], update.status), ("Add a clamp helper", "completed"))
                self.assertIsNone(t3.live_question())
                self.assertIsNone(t3.answer("Yes.", 1))
                self.assertEqual((seam.ops("interrupt"), seam.ops("send")), ([], []))

    def test_not_listed_says_how_many_it_checked_when_it_did_not_see_them_all(self):
        seam = self.seam()
        seam.listing = listing(*[(f"t{i}", f"Thread number {i}") for i in range(45)])
        update = self.over(seam, {self.MISS: "none"}).about("k3", ASK, self.MISS, 0)
        self.assertEqual(update.acts, (act(S.NO_THREAD, recent=40),))

    def test_a_pick_that_comes_back_after_the_call_ended_asks_and_reads_nothing(self):
        seam, decider = self.seam(), BlockingPicks({self.MISS: {"t5": 0.97}})
        t3 = self.over(seam, decider=decider)
        t3.begin_call()
        with ThreadPoolExecutor(max_workers=1) as workers:
            pick = workers.submit(t3.about, "k3", ASK, self.MISS, 0)
            try:
                self.assertTrue(decider.entered.wait(2))
                t3.end_call()
                t3.begin_call()
            finally:
                decider.release.set()
            self.assertEqual(pick.result(timeout=2).status, "failed")
        self.assertEqual(seam.ops("read"), [])
        self.assertIsNone(t3.live_question())

    def test_words_that_fit_several_ask_which_of_those(self):
        seam = self.seam()
        fits = {"unclear": 0.5, "t7": 0.3, "t5": 0.15, "t6": 0.04}
        asked = self.over(seam, {self.MISS: fits}).about("k3", ASK, self.MISS, 0)
        self.assertEqual(said(asked), [("ask_which", ["Upgrade the toolchain", "Add a clamp helper"])])
        self.assertEqual(seam.ops("read"), [])

    def test_a_thread_that_is_not_listed_is_said_to_be_missing(self):
        asked = self.over(self.seam(), {self.MISS: "none"}).about("k3", ASK, self.MISS, 0)
        self.assertEqual((asked.acts, asked.status), ((act(S.NO_THREAD),), "completed"))

    def test_it_means_the_thread_the_call_last_spoke_about(self):
        seam = self.seam()
        t3 = self.over(seam)
        t3.about("k3", ASK, self.MISS, 0)
        status = t3.answer("Yes.", 1)
        t3.about("k4", STOP, "Stop it.", 2)
        self.assertIsNone(t3.decider.picked[-1][2], "a status the caller never heard does not become 'it'")
        t3.spoke(status)
        asked = t3.about("k5", STOP, "Stop it.", 3)
        self.assertEqual(t3.decider.picked[-1][2], "Add a clamp helper")
        self.assertEqual(said(asked), [("confirm_stop", "Add a clamp helper")])

    def test_a_no_asks_which_one_by_up_to_three_other_titles(self):
        seam = self.seam()
        t3 = self.over(seam)
        t3.about("k3", ASK, self.MISS, 0)
        which = t3.answer("No.", 1)
        self.assertEqual(said(which), [("ask_which", ["Fix the flaky ASR test", "Upgrade the toolchain",
                                                      "Write the release notes"])])
        self.assertNotIn("Add a clamp helper", [t.title for t in t3.decider.picked[-1][1]])
        self.assertEqual(seam.ops("read"), [])
        self.assertEqual(said(t3.answer("The toolchain one.", 2)),
                         [("thread_status", "Upgrade the toolchain", "working")])
        self.assertEqual(seam.ops("read"), [("read", "t7")])

    def test_a_no_that_names_another_thread_asks_that_one_back(self):
        t3 = self.over(self.seam())
        t3.about("k3", ASK, self.MISS, 0)
        self.assertEqual(said(t3.answer("No, the toolchain one.", 1)), [("confirm_thread", "Upgrade the toolchain")])

    def test_a_lifecycle_event_is_saved_while_the_decider_is_picking(self):
        seam, decider = self.seam(), BlockingPicks({self.MISS: "t5"})
        t3 = self.over(seam, decider=decider)
        t3.start("k1", "Add a clamp helper in the sandbox project", 0)
        t3.ack_news(t3.answer("yes", 1))
        running = next(iter(t3.follow.journal.tasks.values()))

        with ThreadPoolExecutor(max_workers=2) as workers:
            pick = workers.submit(t3.about, "k3", ASK, self.MISS, 2)
            try:
                self.assertTrue(decider.entered.wait(2))
                seam.push(F.done(160))
                workers.submit(t3.poll, 3).result(timeout=2)
                saved = T3Journal(t3.follow.journal.path)
                self.assertEqual(saved.tasks[running.task_id].status, Status.COMPLETED)
                self.assertEqual(said(saved.pending_lines[0]), [("done", "completed")])
                self.assertFalse(pick.done())
            finally:
                decider.release.set()
            self.assertEqual(said(pick.result(timeout=2)), [("confirm_thread", "Add a clamp helper")])

    def test_no_pick_asks_which_of_the_newest_and_never_guesses(self):
        for decider in (Silent(READINGS), None):
            with self.subTest(decider=type(decider).__name__):
                seam = self.seam()
                t3 = T3Tasks(L.Follow(T3Journal(Path(self.enterContext(tempfile.TemporaryDirectory())) / "j.json"),
                                      F.Connector(seam)), None, decider)
                asked = t3.about("k3", ASK, self.MISS, 0)
                self.assertEqual(said(asked), [("ask_which", [title for _, title in THREADS[:3]])])
                self.assertEqual(t3.question.candidates, ("t5", "t6", "t7"))
                self.assertEqual(seam.ops("read"), [])


class Tell(unittest.TestCase):
    def test_a_follow_up_is_asked_back_and_a_yes_sends_it(self):
        seam = F.FakeSeam(read=F.state(thread="t5", title="Add a clamp helper", as_of=200))
        seam.listing = listing(*THREADS)
        seam.answer("send", F.sent(thread="t5", run="r2", sequence=190))
        t3 = tasks_over(self, seam)
        asked = t3.about("c1", TELL, "Tell the clamp helper thread to also add a test.", 0)
        self.assertEqual(said(asked), [("confirm_tell", "Add a clamp helper", "also add a test")])
        self.assertEqual(seam.ops("send"), [])
        sent = t3.answer("Yes, send it.", 1)
        self.assertEqual((said(sent), sent.sequences, sent.status),
                         ([("sent_followup", "Add a clamp helper")], (190,), "running"))
        self.assertEqual(seam.ops("send")[0][2:], ("t5", "Also add a test"))
        self.assertEqual(seam.ops("watch"), [("watch", "t5", 200, "r2")])

    def test_a_no_drops_the_follow_up(self):
        seam = busy_seam()
        t3 = tasks_over(self, seam)
        t3.about("c1", TELL, "Tell the clamp helper thread to also add a test.", 0)
        self.assertEqual(said(t3.answer("No, don't.", 1)), [("cancelled", "Add a clamp helper", "tell")])
        self.assertEqual(seam.ops("send"), [])

    def test_a_busy_thread_holds_the_follow_up_until_its_run_is_done(self):
        seam = busy_seam()
        t3 = held_follow_up(self, seam)
        task = next(t for t in t3.follow.journal.tasks.values() if t.kind is Kind.TELL)
        refused = seam.ops("send")[0][1]
        self.assertEqual((task.status, len(seam.ops("send"))), (Status.HELD, 1))
        seam.push(F.run_event(151, thread="t5"), F.reply(152, thread="t5"))
        t3.poll(2)
        self.assertEqual((t3.take(), len(seam.ops("send"))), ([], 1))

        seam.answer("send", F.sent(thread="t5", run="r2", sequence=170))
        seam.push(F.done(160, thread="t5"))
        t3.poll(3)
        self.assertEqual([c[1] for c in seam.ops("send")], [refused, task.link.request_id])
        self.assertNotEqual(refused, task.link.request_id)
        self.assertEqual(seam.ops("watch")[-1], ("watch", "t5", 150, "r2"))
        self.assertEqual([(u.task_id, said(u)) for u in t3.take()], [("c1", [("sent_followup", "Add a clamp helper")])])
        seam.push(F.done(180, thread="t5", run="r2", text="Added the test."))
        t3.poll(4)
        self.assertEqual([said(u) for u in t3.take()], [[("done", "Add a clamp helper", "completed")]])

    def test_a_held_follow_up_survives_a_restart(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "journal.json"
        held_follow_up(self, busy_seam(), path)
        saved = next(iter(T3Journal(path).held())).link.request_id
        seam = F.FakeSeam().answer("send", F.sent(thread="t5", run="r2"))
        restarted = tasks_over(self, seam, path=path)
        restarted.resume(0)
        self.assertTrue(restarted.holding())
        seam.push(F.done(160, thread="t5"))
        restarted.poll(1)
        self.assertEqual(seam.ops("send")[0][1:3], (saved, "t5"))
        self.assertFalse(restarted.holding())

    def test_mason_can_take_back_a_held_follow_up(self):
        cases = (("never mind", (), "dropped_followup"),
                 ("Don't pass that on.", ("What's the weather in Lisbon?",), "dropped_followup"),
                 ("never mind", ("What's the weather in Lisbon?",), None))
        for words, before, line in cases:
            with self.subTest(words=words, before=before):
                seam = busy_seam()
                t3 = held_follow_up(self, seam)
                for other in before:
                    self.assertIsNone(t3.answer(other, 2))
                dropped = t3.answer(words, 3)
                self.assertEqual(said(dropped)[0][0] if dropped else None, line)
                self.assertEqual(t3.holding(), line is None)
                seam.push(F.done(160, thread="t5"))
                t3.poll(4)
                self.assertEqual(len(seam.ops("send")), 1 if line else 2)


class Stop(unittest.TestCase):
    def seam(self):
        seam = F.FakeSeam()
        seam.listing = listing(*THREADS)
        return seam.answer("interrupt", F.requested(run="r1", sequence=95), T.NothingRunning())

    def test_stop_says_stopping_then_that_nothing_is_running(self):
        seam = self.seam()
        t3 = tasks_over(self, seam)
        asked = t3.about("s1", STOP, "Stop the clamp helper thread.", 0)
        self.assertEqual((said(asked), seam.ops("interrupt")), ([("confirm_stop", "Add a clamp helper")], []))
        first = t3.answer("Yes, stop it.", 1)
        self.assertEqual((said(first), first.sequences, first.thread_id),
                         ([("stopping", "Add a clamp helper")], (95,), "t5"))
        t3.about("s2", STOP, "Stop that thread.", 2)
        second = t3.answer("yes", 3)
        self.assertEqual((said(second), second.sequences), ([("nothing_running", "Add a clamp helper")], ()))
        ids = [c[1] for c in seam.ops("interrupt")]
        self.assertEqual((len(ids), len(set(ids)), {c[2] for c in seam.ops("interrupt")}), (2, 2, {"t5"}))

    def test_stopping_a_thread_drops_the_follow_up_held_for_it(self):
        seam = busy_seam().answer("interrupt", F.requested(run="r1", sequence=95))
        t3 = held_follow_up(self, seam)
        t3.about("s1", STOP, "Stop the clamp helper thread.", 2)
        self.assertEqual(said(t3.answer("yes", 3)), [("stopping", "Add a clamp helper"),
                                                     ("dropped_followup", "Add a clamp helper")])
        seam.push(F.done(160, thread="t5", status=T.TerminalStatus.INTERRUPTED))
        t3.poll(4)
        self.assertEqual((len(seam.ops("send")), t3.holding(), t3.take()), (1, False, []))


class CrossTool(unittest.TestCase):
    def test_stop_it_right_after_a_start_means_the_thread_just_started(self):
        seam = F.FakeSeam()
        t3 = tasks_over(self, seam)
        t3.start("a1", "Add a clamp helper in the sandbox project", 0)
        t3.answer("yes", 1)
        seam.listing = listing(("t9", "Fix the flaky ASR test"), ("t1", "Add a clamp helper"))
        self.assertEqual(said(t3.about("s1", STOP, "Stop it.", 2)), [("confirm_stop", "Add a clamp helper")])
        self.assertEqual(t3.question.candidates, ("t1",))
        self.assertEqual(said(t3.about("c1", TELL, "Tell it to also add a test.", 3)),
                         [("confirm_tell", "Add a clamp helper", "also add a test")])

    def test_it_with_no_thread_of_this_voice_asks_which(self):
        seam = F.FakeSeam()
        seam.listing = listing(*THREADS)
        update = tasks_over(self, seam).about("s1", STOP, "Stop it.", 0)
        self.assertEqual(said(update), [("ask_which", [title for _, title in THREADS[:3]])])


class ExplicitYes(unittest.TestCase):
    UNCLEAR = ("What do you mean?", "do you mean the other one", "hmm", "yes?", "sure, how long will it take",
               "could you stop it", "I said yes earlier", "maybe")

    def test_nothing_is_sent_or_stopped_without_an_explicit_yes(self):
        for op, words in ((TELL, "Tell the clamp helper thread to also add a test."),
                          (STOP, "Stop the clamp helper thread.")):
            for reply in self.UNCLEAR:
                with self.subTest(op=op, reply=reply):
                    seam = busy_seam().answer("interrupt", F.requested())
                    t3 = tasks_over(self, seam)
                    t3.about("k1", op, words, 0)
                    statuses = [getattr(t3.answer(reply, n), "status", None) for n in (1, 2)]
                    self.assertIn(statuses, ([None, None], ["cancelled", None]), "a reply that is not a yes drops it")
                    self.assertIsNone(t3.answer("yes", 3))
                    self.assertEqual((seam.ops("send"), seam.ops("interrupt")), ([], []))

    def test_a_tell_title_answer_requires_a_yes_before_sending(self):
        seam = self.tied_threads().answer("send", F.sent(thread="t6", run="r2"))
        t3 = tasks_over(self, seam)
        t3.about("k1", TELL, "Tell the flaky test thread to rerun.", 0)
        self.assertEqual(said(t3.answer("The ASR one.", 1)), [("confirm_tell", "Fix the flaky ASR test", "rerun")])
        self.assertEqual(seam.ops("send"), [])
        self.assertEqual(said(t3.answer("Yes, send it.", 2)), [("sent_followup", "Fix the flaky ASR test")])
        self.assertEqual([call[2:] for call in seam.ops("send")], [("t6", "Rerun")])

    def test_a_stop_title_answer_requires_a_yes_before_interrupting(self):
        seam = self.tied_threads().answer("interrupt", F.requested())
        t3 = tasks_over(self, seam)
        t3.about("k1", STOP, "Stop the flaky test thread.", 0)
        self.assertEqual(said(t3.answer("The ASR one.", 1)), [("confirm_stop", "Fix the flaky ASR test")])
        self.assertEqual(seam.ops("interrupt"), [])
        self.assertEqual(said(t3.answer("Yes, stop it.", 2)), [("stopping", "Fix the flaky ASR test")])
        self.assertEqual([call[2] for call in seam.ops("interrupt")], ["t6"])

    def tied_threads(self):
        seam = F.FakeSeam()
        seam.listing = listing(("t5", "Fix the flaky UI test"), ("t6", "Fix the flaky ASR test"))
        return seam


class Authorization(unittest.TestCase):
    """A yes authorizes the one prompt the person heard, in this call, on the very next turn, before it expires."""

    def turn(self, t3, n, records=()):
        segmenter = SimpleNamespace(records=lambda: list(records),
                                    dispatch_log=[{"task_id": r["task_id"], "instruction": r["instruction"]}
                                                  for r in records])
        return T3TaskService(segmenter, f"conv_turn_{n:03d}", 0.0, None, holds_side_effects=False, t3=t3)

    def ask(self, t3, n, tool, words, deliver=True):
        record = {"task_id": "task_1", "instruction": words, "status": "dispatched", "depends_on": [],
                  "side_effect": True, "sources": ["orchestrator"], "history": [], "tool": {"tool": tool, "confidence": 0.9}}
        service = self.turn(t3, n, [record])
        service.sync(0)
        entry = service.snapshot("utt_1", words)["tasks"][0]
        if deliver:
            service.record_delivery(entry, "sent")
        service.close()
        return service

    def say_yes(self, t3, n, reply="Yes."):
        service = self.turn(t3, n)
        waiting = service.open_question()
        events = service.answer(waiting, reply, 1) if waiting is not None else []
        service.close()
        return events

    def stop_setup(self):
        seam = busy_seam().answer("interrupt", F.requested())
        t3 = tasks_over(self, seam, decider=ReplayReading())
        t3.begin_call()
        return seam, t3

    def test_a_heard_prompt_is_answered_by_the_next_turn(self):
        seam, t3 = self.stop_setup()
        self.ask(t3, 1, STOP, "Stop the clamp helper thread.")
        self.assertEqual(len(self.say_yes(t3, 2, "Yes, stop it.")), 1)
        self.assertEqual(len(seam.ops("interrupt")), 1)

    def test_a_prompt_from_a_call_that_ended_cannot_be_answered_on_the_next_one(self):
        seam, t3 = self.stop_setup()
        self.ask(t3, 1, STOP, "Stop the clamp helper thread.")
        t3.end_call()
        t3.begin_call()
        self.assertEqual(self.say_yes(t3, 2, "Yeah, stop it."), [])
        self.assertEqual(seam.ops("interrupt"), [])

    def test_hanging_up_drops_the_question_even_if_nobody_reconnects(self):
        seam, t3 = self.stop_setup()
        self.ask(t3, 1, STOP, "Stop the clamp helper thread.")
        t3.end_call()
        self.assertIsNone(t3.question)

    def test_an_unrelated_turn_between_drops_the_question(self):
        seam, t3 = self.stop_setup()
        self.ask(t3, 1, STOP, "Stop the clamp helper thread.")
        self.assertEqual(self.say_yes(t3, 2, "What's the weather like?"), [])
        self.assertEqual(self.say_yes(t3, 3, "Yes, stop it."), [])
        self.assertEqual(seam.ops("interrupt"), [])

    def test_a_turn_that_ended_without_answering_expires_it(self):
        seam, t3 = self.stop_setup()
        self.ask(t3, 1, STOP, "Stop the clamp helper thread.")
        self.turn(t3, 2).close()
        self.assertEqual(self.say_yes(t3, 3, "Yes, stop it."), [])
        self.assertEqual(seam.ops("interrupt"), [])

    def test_a_question_expires_after_its_time(self):
        seam, t3 = self.stop_setup()
        self.ask(t3, 1, STOP, "Stop the clamp helper thread.")
        t3.question.heard_at -= 2 * CONFIRM_TTL_S
        self.assertEqual(self.say_yes(t3, 2, "Yes, stop it."), [])
        self.assertEqual(seam.ops("interrupt"), [])

    def test_a_question_replaced_while_the_decider_reads_the_reply_is_not_answered_by_it(self):
        from voice.t3.verify import Reading as Read

        class HangsUpMidRead(F.Agree):
            def reads(self, said, reply, options=()):
                def elsewhere():
                    free.append(t3.lock.acquire(blocking=False))
                    if free[-1]:
                        t3.lock.release()
                    t3.end_call()
                    t3.begin_call()
                    test.ask(t3, 9, STOP, "Stop the flaky ASR test thread.")

                other = threading.Thread(target=elsewhere)
                other.start()
                other.join()
                return Read("yes", {"yes": 0.99})

        free, test = [], self
        seam = busy_seam().answer("interrupt", F.requested())
        t3 = tasks_over(self, seam, decider=HangsUpMidRead())
        t3.begin_call()
        self.ask(t3, 1, STOP, "Stop the clamp helper thread.")
        self.assertEqual(self.say_yes(t3, 2, "Please do?"), [], "the yes was read for a prompt that has gone")
        self.assertEqual(free, [True], "the T3 lock is free while the decider is asked")
        self.assertEqual(seam.ops("interrupt"), [])

    def test_a_prompt_that_was_never_heard_is_asked_again_and_not_acted_on(self):
        seam, t3 = self.stop_setup()
        self.ask(t3, 1, STOP, "Stop the clamp helper thread.", deliver=False)
        events = self.say_yes(t3, 2, "Yes, stop it.")
        self.assertEqual(seam.ops("interrupt"), [])
        self.assertEqual([e["event"] for e in events], ["completed"], "the question comes back to be asked again")

    def test_a_prompt_that_was_never_heard_goes_back_to_be_said_when_the_call_is_quiet(self):
        seam, t3 = self.stop_setup()
        self.ask(t3, 1, STOP, "Stop the clamp helper thread.", deliver=False)
        self.assertTrue(t3.has_news(idle=True))
        update, = t3.reserve_news(idle=True)
        self.assertEqual(update.task_id, t3.question.key)
        t3.spoke(update)
        self.assertEqual(len(self.say_yes(t3, 2, "Yes, stop it.")), 1, "once said, a yes answers it")
        self.assertEqual(len(seam.ops("interrupt")), 1)

    def test_an_unheard_prompt_the_next_turn_dropped_is_never_said_later(self):
        seam, t3 = self.stop_setup()
        self.ask(t3, 1, STOP, "Stop the clamp helper thread.", deliver=False)
        self.say_yes(t3, 2, "What's the weather like?")
        self.assertFalse(t3.has_news(idle=True))
        self.assertEqual(t3.reserve_news(idle=True), [])
        self.assertEqual(t3.follow.journal.pending_lines, [], "and it leaves the journal")

    def test_an_unheard_prompt_asked_again_and_heard_in_a_turn_is_not_said_again(self):
        seam, t3 = self.stop_setup()
        self.ask(t3, 1, STOP, "Stop the clamp helper thread.", deliver=False)
        service = self.turn(t3, 2)
        service.answer(service.open_question(), "Yes, stop it.", 1)
        entry, = service.snapshot("utt_1", "Yes, stop it.")["tasks"]
        service.record_delivery(entry, "sent")
        service.close()
        self.assertFalse(t3.has_news(idle=True))

    def test_two_prompts_queued_in_a_turn_leave_only_the_last_one_heard(self):
        seam, t3 = self.stop_setup()
        records = [{"task_id": "task_1", "instruction": "Stop the clamp helper thread.", "status": "dispatched",
                    "depends_on": [], "side_effect": True, "sources": ["orchestrator"], "history": [],
                    "tool": {"tool": STOP, "confidence": 0.9}},
                   {"task_id": "task_2", "instruction": "Tell the clamp helper thread to add a test.",
                    "status": "dispatched", "depends_on": [], "side_effect": True, "sources": ["orchestrator"],
                    "history": [], "tool": {"tool": TELL, "confidence": 0.9}}]
        service = self.turn(t3, 1, records)
        service.sync(0)
        first, second = service.snapshot("utt_1", "...")["tasks"]
        service.record_delivery(first, "sent")
        self.assertEqual(t3.question.key, "conv_turn_001:task_2", "the later prompt replaced the first")
        self.assertFalse(t3.question.heard, "so a yes cannot reach the one that was heard first")
        service.close()
        self.say_yes(t3, 2, "Yes, stop it.")
        self.assertEqual((seam.ops("interrupt"), seam.ops("send")), ([], []))


class Service(unittest.IsolatedAsyncioTestCase):
    def service(self, t3, records):
        segmenter = SimpleNamespace(records=lambda: records,
                                    dispatch_log=[{"task_id": r["task_id"], "instruction": r["instruction"]}
                                                  for r in records])
        return T3TaskService(segmenter, "conv_turn_001", 0.0, None, holds_side_effects=False, t3=t3)

    def record(self, task_id, instruction, tool=None, confidence=0.9):
        return {"task_id": task_id, "instruction": instruction, "status": "dispatched", "depends_on": [],
                "side_effect": tool in (START, TELL, STOP), "sources": ["orchestrator"], "history": [],
                "tool": {"tool": tool, "confidence": confidence} if tool else None}

    def test_t3_records_go_to_t3_and_others_to_the_mock(self):
        seam = F.FakeSeam()
        seam.listing = listing(*THREADS)
        t3 = tasks_over(self, seam)
        records = [self.record("task_1", "Add a clamp helper in the sandbox project", START),
                   self.record("task_2", "What's the weather in Lisbon?"),
                   self.record("task_3", "Stop the toolchain thread.", STOP),
                   self.record("task_4", "Tell me, uh, the fix our toolchain thread been going?", TELL,
                               confidence=0.2)]
        service = self.service(t3, records)
        service.sync(0)
        snapshot = service.snapshot("utt_1", "...")
        by_id = {t["task_id"]: t for t in snapshot["tasks"]}
        self.assertEqual(by_id["task_1"]["acts"], [act(S.CONFIRM_LAUNCH, project="Voice sandbox").to_json()])
        self.assertEqual(by_id["task_1"]["evidence"], {"thread_id": None, "project_id": "p1", "sequences": [1]})
        self.assertEqual(by_id["task_2"]["status"], "running")
        self.assertNotIn("acts", by_id["task_2"])
        self.assertEqual(by_id["task_3"]["acts"], [act(S.CONFIRM_STOP, thread="Upgrade the toolchain").to_json()])
        self.assertIn(by_id["task_4"]["acts"][0]["kind"], {"thread_status", "confirm_thread", "ask_which", "no_thread"},
                      "an unsure pick reaches T3 as a lookup, never as a message to send")
        self.assertIs(service.open_question(), T3_WAITING)
        self.assertEqual(service.executions["task_1"].state.value, "completed")
        self.assertEqual((seam.ops("launch"), seam.ops("interrupt")), ([], []))

    def test_one_request_cut_into_several_launch_segments_asks_one_question_and_launches_all_of_it(self):
        said_whole = ("Start a thread for the importer logging. Can you kick that off? Keep it under an hour, and use "
                      "my old config as the base. Also look at the retry counts. And keep it organized.")
        seam = F.FakeSeam()
        t3 = tasks_over(self, seam)
        service = self.service(t3, [self.record("task_1", "Start a thread for the importer logging.", START),
                                    self.record("task_2", "Can you kick that off?", START),
                                    self.record("task_3", "Also look at the retry counts.", START),
                                    self.record("task_4", "And keep it organized.", TELL)])
        service.sync(0)
        tasks = {t["task_id"]: t for t in service.snapshot("utt_1", said_whole)["tasks"]}
        self.assertEqual([a["kind"] for a in tasks["task_1"]["acts"]], ["confirm_launch"])
        self.assertNotIn("acts", tasks["task_2"], "the second segment is the same request, already asked")
        self.assertNotIn("acts", tasks["task_3"])
        self.assertNotIn("acts", tasks["task_4"], "an instruction for the thread being started is part of the launch")
        service.record_delivery(tasks["task_1"], "sent")
        self.assertEqual(said(t3.answer("yes", 1)), [("launched", "Voice sandbox")])
        launch, = seam.ops("launch")
        self.assertEqual(launch[2:], ("p1", said_whole), "the launch carries every constraint and the second request")
        self.assertEqual(t3.follow.journal.tasks[next(iter(t3.follow.journal.tasks))].instruction, said_whole)

    def test_a_message_for_a_thread_there_is_stays_its_own_request_beside_a_launch(self):
        seam = F.FakeSeam()
        seam.listing = listing(*THREADS)
        t3 = tasks_over(self, seam)
        service = self.service(t3, [self.record("task_1", "Start a thread for the importer logging.", START),
                                    self.record("task_2", "Tell the toolchain thread to retry.", TELL)])
        service.sync(0)
        tasks = {t["task_id"]: t for t in service.snapshot("utt_1", "...")["tasks"]}
        self.assertEqual([a["kind"] for a in tasks["task_2"]["acts"]], ["confirm_tell"])

    def test_a_message_that_may_mean_several_threads_is_asked_about_beside_a_launch(self):
        seam = F.FakeSeam()
        seam.listing = listing(("t5", "Fix the flaky UI test"), ("t6", "Fix the flaky ASR test"))
        t3 = tasks_over(self, seam)
        service = self.service(t3, [self.record("task_1", "Start a thread for the importer logging.", START),
                                    self.record("task_2", "Tell the flaky test thread to retry.", TELL)])
        service.sync(0)
        tasks = {t["task_id"]: t for t in service.snapshot("utt_1", "...")["tasks"]}
        self.assertEqual([a["kind"] for a in tasks["task_2"]["acts"]], ["ask_which"])

    def test_a_launch_question_from_an_earlier_turn_does_not_swallow_a_new_request(self):
        t3 = tasks_over(self, F.FakeSeam())
        t3.start("conv_turn_000:task_1", "Start a thread for the importer logging.", 0)
        service = self.service(t3, [self.record("task_1", "Start a thread for the save slot writer.", START)])
        service.sync(0)
        entry = service.snapshot("utt_1", "...")["tasks"][0]
        self.assertEqual([a["kind"] for a in entry["acts"]], ["confirm_launch"])

    def test_the_next_turn_answers_and_later_news_arrives_as_updates(self):
        seam = F.FakeSeam()
        t3 = tasks_over(self, seam)
        first = self.service(t3, [self.record("task_1", "Add a clamp helper in the sandbox project", START)])
        first.sync(0)
        first.record_delivery(first.snapshot("utt_1", "...")["tasks"][0], "sent")
        turn2 = self.service(t3, [])
        waiting = turn2.open_question()
        events = turn2.answer(waiting, "yes", 1)
        self.assertEqual(events[0]["event"], "completed")
        entry = next(t for t in turn2.snapshot("utt_1", "yes")["tasks"] if "acts" in t)
        self.assertEqual(([a["kind"] for a in entry["acts"]], entry["evidence"]["sequences"]), (["launched"], [93]))
        seam.push(F.done(160))
        t3.poll(2)
        turn3 = self.service(t3, [])
        updates = turn3.take_updates()
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0]["acts"], [act(S.DONE, status="completed").to_json()])
        self.assertEqual(updates[0]["evidence"], {"thread_id": "t1", "sequences": [160, 161]})
        self.assertEqual(updates[0]["task_id"], "conv_turn_001:task_1")
        self.assertEqual(turn3.update_entries(updates), updates)
        self.assertIsNone(task_note(updates[0]), "a T3 entry is worded by say_acts, never by a task note")

    async def test_unspoken_stop_confirmation_is_reasked_before_a_yes_can_act(self):
        seam = F.FakeSeam()
        seam.listing = listing(*THREADS)
        t3 = worded(tasks_over(self, seam), FakeText("Should I stop Add a clamp helper?"))
        first = self.service(t3, [self.record("task_1", "Stop the clamp helper thread.", STOP)])
        first.sync(0)
        entry = first.snapshot("utt_1", "...")["tasks"][0]
        voice = await Scripted.open("ws://unused", "voice", lambda: 0)
        voice.asked = 0
        voice.script = ["I stopped Add a clamp helper."]
        spoken = []

        async def send(line, pcm):
            spoken.append(line.text)
            return pcm, 0

        await say_acts(voice, 24_000, 0, entry, first, [], lambda *args, **kwargs: None, send_audio=send,
                       record_delivery=lambda state: first.record_delivery(entry, state))
        self.assertEqual(spoken, [])
        self.assertNotIn("spoken", entry)
        first.close()

        retry = self.service(t3, [])
        retry.answer(retry.open_question(), "yes", 1)
        self.assertEqual(seam.ops("interrupt"), [])
        self.assertIsNotNone(t3.question)
        entry = retry.snapshot("utt_1", "yes")["tasks"][0]
        self.assertEqual(entry["acts"], [act(S.CONFIRM_STOP, thread="Add a clamp helper").to_json()])
        line = "Should I stop Add a clamp helper?"
        voice.script.append(line)
        await say_acts(voice, 24_000, 0, entry, retry, [], lambda *args, **kwargs: None, send_audio=send,
                       record_delivery=lambda state: retry.record_delivery(entry, state))
        self.assertEqual(spoken, [line])
        third = self.service(t3, [])
        third.answer(third.open_question(), "yes", 2)
        self.assertEqual(len(seam.ops("interrupt")), 1)

    def test_a_new_status_request_replaces_an_unspoken_stop_confirmation(self):
        question = "How's the clamp helper thread going?"
        for dispatch_before_final in (False, True):
            with self.subTest(dispatch_before_final=dispatch_before_final):
                seam = busy_seam()
                t3 = tasks_over(self, seam)
                first = self.service(t3, [self.record("task_1", "Stop the toolchain thread.", STOP)])
                first.sync(0)
                first.close()

                records = [self.record("task_2", question, ASK)] if dispatch_before_final else []
                turn = self.service(t3, records)
                waiting = turn.open_question()
                if dispatch_before_final:
                    turn.sync(1)
                self.assertEqual(turn.answer(waiting, question, 1), [])
                if not dispatch_before_final:
                    records.append(self.record("task_2", question, ASK))
                    turn.segmenter.dispatch_log.append({"task_id": "task_2", "instruction": question})
                    turn.sync(1)
                entry, = turn.snapshot("utt_2", question)["tasks"]
                self.assertEqual([a["kind"] for a in entry["acts"]], ["confirm_thread"])
                turn.record_delivery(entry, "sent")
                answer = self.service(t3, [])
                answer.answer(answer.open_question(), "yes", 2)
                status, = answer.snapshot("utt_3", "yes")["tasks"]
                self.assertEqual([a["kind"] for a in status["acts"]], ["thread_status"])
                self.assertEqual(status["evidence"]["thread_id"], "t5")
                self.assertEqual(seam.ops("interrupt"), [])

    def test_news_a_turn_never_spoke_is_taken_again_even_after_a_restart(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "journal.json"
        seam = F.FakeSeam()
        t3 = tasks_over(self, seam, path=path)
        t3.start("a1", "Add a clamp helper in the sandbox project", 0)
        t3.ack_news(t3.answer("yes", 1))
        seam.push(F.done(160))
        t3.poll(2)
        taken = t3.take()
        self.assertTrue(taken and not t3.has_news())
        t3.give_back(taken)
        self.assertTrue(t3.has_news())
        restarted = tasks_over(self, F.FakeSeam(), path=path)
        again = restarted.take()
        self.assertEqual([(u.acts, u.sequences) for u in again], [(u.acts, u.sequences) for u in taken])
        self.assertEqual(len(T3Journal(path).pending_lines), len(again), "given back once, saved once")

    def journaled(self, t3):
        return [[a.kind.value for a in u.acts] for u in T3Journal(t3.follow.journal.path).pending_lines]

    def test_a_reserved_line_survives_a_crash_before_its_audio_is_sent(self):
        seam = F.FakeSeam()
        t3 = tasks_over(self, seam)
        first = self.service(t3, [self.record("task_1", "Add a clamp helper in the sandbox project", START)])
        first.sync(0)
        first.record_delivery(first.snapshot("utt_1", "...")["tasks"][0], "sent")
        answer = self.service(t3, [])
        answer.answer(answer.open_question(), "yes", 1)
        self.assertEqual(self.journaled(t3), [["launched"]], "the write's own answer is reserved, not removed")
        entry = next(t for t in answer.snapshot("utt_1", "yes")["tasks"] if "acts" in t)
        answer.record_delivery(entry, "sent")
        self.assertEqual(self.journaled(t3), [], "marked delivered once its audio was sent")
        seam.push(F.done(160))
        t3.poll(2)
        turn = self.service(t3, [])
        update, = turn.take_updates()
        self.assertEqual(self.journaled(t3), [["done"]], "a crash here finds the line on restart")
        taken = t3.take()
        self.assertEqual((taken, self.journaled(t3)), ([], [["done"]]))
        turn.record_delivery(update, "sent")
        self.assertEqual(self.journaled(t3), [])

    def test_a_line_cut_off_part_way_is_said_again_once_then_waits_for_a_turn(self):
        seam = F.FakeSeam()
        t3 = tasks_over(self, seam)
        t3.start("a1", "Add a clamp helper in the sandbox project", 0)
        t3.ack_news(t3.answer("yes", 1))
        seam.push(F.done(160))
        t3.poll(2)
        turn = self.service(t3, [])
        update, = turn.take_updates()
        turn.record_delivery(update, "partial")
        turn.close()
        self.assertEqual(self.journaled(t3), [["done"]], "part of a line is not the line")
        self.assertTrue(t3.has_news(idle=True), "the next quiet moment says it again")
        again = t3.reserve_news(idle=True)
        self.assertEqual([[a.kind for a in u.acts] for u in again], [[S.DONE]])
        t3.retry_news(again)
        self.assertFalse(t3.has_news(idle=True), "said again once, then it waits for the person's turn")
        later = self.service(t3, [])
        self.assertEqual([u["acts"][0]["kind"] for u in later.take_updates()], ["done"])

    def test_a_held_follow_up_keeps_the_answer_path_open(self):
        t3 = held_follow_up(self, busy_seam())
        turn = self.service(t3, [])
        self.assertIs(turn.open_question(), T3_WAITING)
        self.assertEqual(turn.answer(T3_WAITING, "What's the weather in Lisbon?", 2), [])
        self.assertEqual(self.service(tasks_over(self, F.FakeSeam()), []).open_question(), None)

    def test_a_mid_utterance_t3_ask_starts_the_read_and_the_summary(self):
        seam = F.FakeSeam(read=F.state(thread="t5", title="Add a clamp helper", as_of=140,
                                       activity=T.ThreadActivity.FINISHED,
                                       last_reply=T.LastReply("Done. Tests pass.", "m1", None, "r1", "2026")))
        seam.listing = listing(*THREADS)
        text = FakeText()
        t3 = worded(tasks_over(self, seam), text)
        t3.follow.ensure(0)
        asking = {**self.record("task_1", "How is the clamp helper thread", ASK), "status": "pending"}
        service = T3TaskService(SimpleNamespace(records=lambda: [asking], dispatch_log=[]), "conv_turn_001", 0.0,
                                None, holds_side_effects=False, t3=t3)
        service.sync(0)
        self.assertTrue(text.summarised.wait(2), "the summary starts before the decider dispatches the ask")
        self.assertEqual(seam.ops("read"), [("read", "t5")])
        self.assertEqual(service.executions, {}, "nothing is dispatched or said yet")
        service.sync(1)
        self.assertEqual(text.calls, 1, "one speculative start per marked ask")

    def test_the_registry_offers_the_four_t3_tools_before_none(self):
        base = SimpleNamespace(choices=lambda: {"weather": "w", "agent": "a", "none": "n"}, tool_threshold=0.6)
        registry = T3Registry(base)
        self.assertEqual(list(registry.choices()),
                         ["weather", "agent", "t3_start", "t3_ask", "t3_tell", "t3_stop", "none"])
        self.assertEqual(registry.tool_threshold, 0.6)
        self.assertEqual(set(T3_TOOLS), tool_registry.T3_TOOLS, "the gate and the kind hint name these tools")
        self.assertEqual(PLAN_A.confirmed_tools, tool_registry.T3_TOOLS)

    def test_the_transcript_names_each_spoken_line_its_evidence_and_its_record(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        records = Path(tmp.name)
        start = {"clip": "turn_001", "final_transcript": "Start a thread in the sandbox.",
                 "snapshot_after_tools": {"tasks": [{"task_id": "task_1", "spoken": "Start it in Voice sandbox?",
                                                     "acts": [act(S.CONFIRM_LAUNCH, project="Voice sandbox").to_json()],
                                                     "evidence": {"thread_id": None, "sequences": [681]}}]},
                 "replies": [{"text": "Should I start it in Voice sandbox?"}]}
        news = {"clip": "turn_002", "idle": True, "final_transcript": None,
                "task_updates": [{"spoken": "It finished.", "row": "done-event",
                                  "acts": [act(S.DONE, status="completed").to_json()],
                                  "evidence": {"thread_id": "t1", "sequences": [750, 751]}}],
                "replies": [{"text": "It finished."}]}
        for record in (start, news):
            (records / f"{record['clip']}.json").write_text(json.dumps(record), encoding="utf-8")
        rows = transcript_rows(records)
        self.assertEqual([r["record"] for r in rows], ["turn_001.json", "turn_002.json"])
        self.assertEqual(rows[0]["t3"], [{"line": "Start it in Voice sandbox?", "acts": ["confirm_launch"],
                                          "thread_id": None, "sequences": [681]}])
        self.assertEqual((rows[1]["idle"], rows[1]["t3"][0]["sequences"], rows[1]["said"]),
                         (True, [750, 751], ["It finished."]))


class WholeDialogue(unittest.TestCase):
    def test_every_spoken_line_is_the_text_model_s_wording_of_its_acts(self):
        seam = F.FakeSeam(read=F.state(thread="t1", title="Add a clamp helper", as_of=210,
                                       activity=T.ThreadActivity.FINISHED,
                                       last_reply=T.LastReply("Added clamp. Tests pass.", "m9", None, "r1", "2026")))
        seam.listing = listing(("t1", "Add a clamp helper"), ("t6", "Fix the flaky ASR test"))
        t3 = worded(tasks_over(self, seam), FakeText())
        script = Script(t3)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            script.tool("a1", START, "Add a clamp helper in the sandbox project")
            script.reply("a1", "Yes, start it.")
            seam.push(F.run_event(100), F.reply(101, text="Working on clamp."), F.needs_you(102),
                      F.resolved(103), F.done(200))
            t3.poll(script.clock())
            for update in t3.take():
                script.show("watch", None, update)
            script.tool("b1", ASK, "How is the clamp helper thread going?")
            seam.answer("send", F.sent(thread="t1"))
            script.tool("c1", TELL, "Tell the clamp helper thread to also add a test.")
            script.reply("c1", "Yes, send it.")
            seam.answer("interrupt", F.requested())
            script.tool("s1", STOP, "Stop the clamp helper thread.")
            script.reply("s1", "Yes, stop it.")
        rows = [json.loads(line) for line in out.getvalue().splitlines()]
        spoken = [row for row in rows if row.get("acts")]
        self.assertEqual([row for row in spoken if row["line"] is None], [], "nothing waits")
        self.assertEqual([a["kind"] for row in spoken for a in row["acts"]],
                         ["confirm_launch", "launched", "run_update", "replied", "needs_you", "need_resolved",
                          "done", "thread_status", "confirm_tell", "sent_followup", "confirm_stop",
                          "stopping"])
        for row in spoken:
            acts = [Act.from_json(a) for a in row["acts"]]
            with self.subTest(acts=[a.kind.value for a in acts]):
                self.assertEqual(row["line"], FakeText.wording(kept_exact(acts),
                                                               any(a.kind in QUESTIONS for a in acts)))
                self.assertEqual(check(row["line"], acts), [])
        done = next(row for row in spoken if row["acts"][0]["kind"] == "done")
        self.assertEqual((done["acts"][0]["slots"]["summary"], done["evidence"]["sequences"]),
                         (FakeText.GIST, [200, 201, 210]), "the done carries its summary and the read it came from")


class ReplyWords(unittest.TestCase):
    def test_tell_parts(self):
        cases = {
            "Tell it to also add a test.": ("it", "also add a test", "to"),
            "tell the clamp helper thread to also add a test": ("the clamp helper thread", "also add a test", "to"),
            "Okay, can you ask the PR thread to rebase on main?": ("the PR thread", "rebase on main", "to"),
            "Let it know that the tests are flaky.": ("it", "the tests are flaky", "that"),
            "Also add a test to the clamp thread.": ("Also add a test to the clamp thread.",
                                                     "Also add a test to the clamp thread.", None),
        }
        for text, parts in cases.items():
            with self.subTest(text=text):
                self.assertEqual(tell_parts(text), parts)


class WithdrawnQuestion(unittest.TestCase):
    """A question the person heard and then did not answer is reported once, so the turn can tell the voice model to
    leave it behind: its session still holds the words, and a free reply says them again."""

    def asked(self, decider=None):
        seam = F.FakeSeam(read=F.state(thread="t6", title="Fix the flaky ASR test", as_of=150))
        seam.listing = listing(("t5", "Fix the flaky UI test"), ("t6", "Fix the flaky ASR test"))
        t3 = tasks_over(self, seam, decider=decider or F.Reader({"never mind": "unrelated"}))
        update = t3.about("k2", ASK, "How is the flaky test thread going?", 0)
        t3.spoke(update)
        return t3

    def test_a_heard_question_dropped_unanswered_is_reported_once(self):
        t3 = self.asked()
        self.assertIsNone(t3.answer("never mind", 1))
        self.assertEqual(t3.take_withdrawn().kind, "thread")
        self.assertIsNone(t3.take_withdrawn(), "it is reported to one turn only")

    def test_an_answered_question_is_not_withdrawn(self):
        t3 = self.asked(F.Reader({"The ASR one.": ("other", "Fix the flaky ASR test")}))
        self.assertIsNotNone(t3.answer("The ASR one.", 1))
        self.assertIsNone(t3.take_withdrawn())

    def test_a_question_nobody_heard_is_not_withdrawn(self):
        seam = F.FakeSeam()
        seam.listing = listing(("t5", "Fix the flaky UI test"), ("t6", "Fix the flaky ASR test"))
        t3 = tasks_over(self, seam, decider=F.Reader({"never mind": "unrelated"}))
        t3.about("k2", ASK, "How is the flaky test thread going?", 0)
        t3.answer("never mind", 1)
        self.assertIsNone(t3.take_withdrawn())

    def test_a_question_that_ran_out_of_time_is_withdrawn(self):
        t3 = self.asked()
        with patch("voice.live.t3_tasks.time.monotonic", return_value=t3.question.heard_at + CONFIRM_TTL_S + 1):
            self.assertIsNone(t3.live_question())
        self.assertEqual(t3.take_withdrawn().kind, "thread")

    def test_a_new_call_forgets_it(self):
        t3 = self.asked()
        t3.answer("never mind", 1)
        t3.begin_call()
        self.assertIsNone(t3.take_withdrawn())


class ClaimFacts(unittest.TestCase):
    def tasks(self, **state):
        seam = F.FakeSeam(read=F.state(thread="t6", title="Fix the flaky ASR test", as_of=150, **state))
        seam.listing = listing(("t6", "Fix the flaky ASR test"))
        t3 = tasks_over(self, seam)
        t3.follow.ensure(0)
        return t3

    def test_no_thread_in_view_gives_no_facts_and_a_thread_of_an_earlier_call_is_not_in_view(self):
        t3 = self.tasks()
        self.assertIsNone(t3.claim_facts())
        t3.recent = "t6"
        self.assertIsNone(t3.claim_facts(), "`recent` outlives the call; a reply is judged against this call's thread")

    def test_the_facts_are_the_thread_the_call_last_spoke_of(self):
        t3 = self.tasks(activity=T.ThreadActivity.WAITING_ON_YOU, pending=F.need(prompt="Run the formatter?"),
                        last_reply=T.LastReply("I added it.", "m1", None, "r1", "2026-11-17T10:00:00Z"))
        t3.spoke(L.Update("k1", (act(S.THREAD_STATUS, thread="Fix the flaky ASR test", status="working"),),
                          "completed", "ask", "t6"))
        thread_id, facts = t3.claim_facts()
        self.assertEqual(thread_id, "t6")
        self.assertEqual(facts, {"thread": "Fix the flaky ASR test", "status": "waiting_on_you", "run": None,
                                 "waiting_on_the_person": {"for": "command_approval", "request": "Run the formatter?"},
                                 "last_reply": "I added it.", "the_assistant_did": []})

    def test_what_the_voice_sent_to_the_thread_is_in_the_facts(self):
        t3 = self.tasks()
        t3.spoken_id = "t6"
        task = t3.follow.journal.begin(Kind.TELL, "Also add a test.", "p1", "Voice sandbox", thread_id="t6")
        task.status = Status.COMPLETED
        _, facts = t3.claim_facts()
        self.assertEqual(facts["the_assistant_did"], [{"did": "tell", "text": "Also add a test.", "outcome": "completed"}])

    def test_a_correction_is_a_typed_act_from_a_fresh_read(self):
        t3 = self.tasks(activity=T.ThreadActivity.WAITING_ON_YOU, pending=F.need(prompt="Run the formatter?"))
        update = t3.correction("t6", "turn_001")
        self.assertEqual(update.acts, (act(S.CORRECTION, thread="Fix the flaky ASR test", status="waiting_on_you",
                                           need="command_approval", pending="Run the formatter?",
                                           waiting_for="approval"),))
        self.assertEqual((update.thread_id, update.sequences, update.row), ("t6", (150,), "correction"))

    def test_a_held_correction_is_news_for_the_quiet_moment_and_is_not_journaled(self):
        t3 = self.tasks()
        update = t3.correction("t6", "turn_001")
        t3.hold_news(update)
        self.assertTrue(t3.has_news(idle=True))
        self.assertEqual(t3.reserve_news(idle=True), [update])
        self.assertEqual(t3.follow.journal.pending_lines, [])


def waiting_listing(*rows):
    """A listing in which each (id, title) waits on the person."""
    threads = tuple(T.ThreadSummary(tid, "p1", title, T.ThreadActivity.WAITING_ON_YOU, "2026-11-17T10:00:00Z", False)
                    for tid, title in rows)
    return T.ThreadList(threads, len(threads), False, 77)


class Respond(unittest.TestCase):
    """Approving or denying what a thread waits on: confirmed back with the thread and what it is for, answered once on a
    yes, and never an answer to a request that was not the one named."""

    def seam(self, *rows, **state):
        state = {"pending": F.need("q1", "Run the formatter?"), **state}
        seam = F.FakeSeam(read=F.state(thread="t5", title="Add a clamp helper", as_of=140,
                                       activity=T.ThreadActivity.WAITING_ON_YOU, **state))
        seam.listing = waiting_listing(*(rows or (("t5", "Add a clamp helper"),)))
        return seam

    def asked(self, op=APPROVE, seam=None, decider=None):
        seam = seam or self.seam()
        t3 = tasks_over(self, seam, decider=decider)
        update = t3.about("k1", op, "I approve." if op == APPROVE else "No, do not allow that.", 0)
        t3.spoke(update)
        return seam, t3, update

    def test_the_confirmation_names_the_thread_and_what_is_asked_and_answers_nothing_yet(self):
        seam, t3, update = self.asked()
        self.assertEqual(update.acts, (act(S.CONFIRM_RESPOND, thread="Add a clamp helper", decision="approve",
                                           need="command_approval", pending="Run the formatter?",
                                           asked=DESCRIBE_REQUEST),))
        self.assertEqual((update.status, seam.ops("respond")), ("needs_clarification", []))

    def test_the_confirmation_carries_a_description_of_this_request_from_its_own_records(self):
        seam = self.seam()
        text = FakeText()
        t3 = worded(tasks_over(self, seam), text)
        update = t3.about("k1", APPROVE, "I approve.", 0)
        t3.say(update)
        self.assertEqual(update.acts[0].kind, S.CONFIRM_RESPOND)
        self.assertEqual(update.acts[0].slots["summary"], FakeText.GIST)
        described = [m["content"] for m in text.seen if "Run the formatter?" in m["content"]]
        self.assertTrue(described, "the summariser was given this request's own prompt")
        self.assertIn(DESCRIBE_REQUEST, described[0], "and asked what approving it would let the thread do")

    def test_a_request_with_nothing_stored_about_what_it_does_cannot_be_approved_by_voice(self):
        seam = F.FakeSeam(read=F.state(thread="t5", title="Add a clamp helper", as_of=140,
                                       activity=T.ThreadActivity.WAITING_ON_YOU, pending_detailed=False,
                                       pending=F.need("q1", "It wants your approval to run a command.")))
        seam.listing = waiting_listing(("t5", "Add a clamp helper"))
        t3 = tasks_over(self, seam)
        update = t3.about("k1", APPROVE, "I approve.", 0)
        self.assertEqual(update.acts, (act(S.NEEDS_THE_APP, thread="Add a clamp helper", decision="approve"),))
        self.assertEqual((update.status, t3.live_question(), seam.ops("respond")), ("completed", None, []))
        self.assertIsNone(t3.answer("Yes.", 1), "there is nothing to say yes to")
        self.assertEqual(seam.ops("respond"), [])

    def test_denying_a_request_with_nothing_stored_is_still_confirmed_because_nothing_runs(self):
        seam = F.FakeSeam(read=F.state(thread="t5", title="Add a clamp helper", as_of=140,
                                       activity=T.ThreadActivity.WAITING_ON_YOU, pending_detailed=False,
                                       pending=F.need("q1", "It wants your approval to run a command.")))
        seam.listing = waiting_listing(("t5", "Add a clamp helper"))
        update = tasks_over(self, seam).about("k1", DENY, "No, do not allow that.", 0)
        self.assertEqual((update.acts[0].kind, update.acts[0].slots["decision"]), (S.CONFIRM_RESPOND, "deny"))

    def test_an_approval_whose_description_did_not_pass_is_sent_to_the_app_and_asks_nothing(self):
        class NoGist(FakeText):
            def complete(self, messages):
                record = super().complete(messages)
                return {**record, "text": ""} if messages[0]["content"] != RENDER_SYSTEM else record

        seam = self.seam()
        t3 = worded(tasks_over(self, seam), NoGist())
        update = t3.about("k1", APPROVE, "I approve.", 0)
        t3.spoke(update)
        t3.say(update)
        self.assertEqual(update.acts, (act(S.NEEDS_THE_APP, thread="Add a clamp helper", decision="approve"),))
        self.assertEqual((update.status, t3.live_question()), ("completed", None))
        self.assertIsNone(t3.answer("Yes.", 1))
        self.assertEqual(seam.ops("respond"), [])

    def test_a_request_that_became_a_question_before_the_yes_is_read_out_and_not_answered(self):
        seam, t3, _ = self.asked()
        seam.read = F.state(thread="t5", title="Add a clamp helper", as_of=150,
                            activity=T.ThreadActivity.WAITING_ON_YOU,
                            pending=F.need("q2", "Which fixture?", kind=T.PendingKind.QUESTION,
                                           source=T.NeedSource.USER_INPUT))
        update = t3.answer("Yes.", 1)
        self.assertEqual((update.acts[0].kind, seam.ops("respond")), (S.THREAD_STATUS, []))

    def test_a_yes_approves_the_request_that_was_named_and_the_line_reports_what_t3_returned(self):
        seam, t3, _ = self.asked()
        seam.answer("respond", F.responded("t5", "q1", "approve", sequence=141))
        update = t3.answer("Yes.", 1)
        self.assertEqual([c[2:] for c in seam.ops("respond")], [("t5", "q1", "approve")])
        self.assertEqual(update.acts, (act(S.RESPONDED, thread="Add a clamp helper", decision="approve",
                                           need="command_approval"),))
        self.assertEqual((update.status, update.sequences), ("completed", (141,)))

    def test_denying_works_as_approving_does(self):
        seam, t3, update = self.asked(DENY)
        self.assertEqual(update.acts[0].slots["decision"], "deny")
        seam.answer("respond", F.responded("t5", "q1", "deny", sequence=141))
        done = t3.answer("Yes.", 1)
        self.assertEqual([c[2:] for c in seam.ops("respond")], [("t5", "q1", "deny")])
        self.assertEqual(done.acts[0].slots["decision"], "deny")

    def test_a_no_or_an_unrelated_reply_answers_nothing(self):
        for reply in ("No.", "What do you mean?", "Hold on."):
            with self.subTest(reply=reply):
                seam, t3, _ = self.asked()
                t3.answer(reply, 1)
                self.assertEqual(seam.ops("respond"), [])

    def test_a_request_that_changed_before_the_yes_is_asked_about_again_and_not_answered(self):
        seam, t3, _ = self.asked()
        seam.read = F.state(thread="t5", title="Add a clamp helper", as_of=150, activity=T.ThreadActivity.WAITING_ON_YOU,
                            pending=F.need("q2", "Change the config file?", source=T.NeedSource.FILE_CHANGE))
        again = t3.answer("Yes.", 1)
        self.assertEqual(seam.ops("respond"), [])
        self.assertEqual(again.acts[0].kind, S.CONFIRM_RESPOND)
        self.assertEqual(again.acts[0].slots["need"], "file_change")

    def test_a_request_that_is_gone_before_the_yes_says_nothing_is_waiting(self):
        seam, t3, _ = self.asked()
        seam.read = F.state(thread="t5", title="Add a clamp helper", as_of=150, activity=T.ThreadActivity.WORKING)
        update = t3.answer("Yes.", 1)
        self.assertEqual((seam.ops("respond"), update.acts), ([], (act(S.NOTHING_PENDING, thread="Add a clamp helper"),)))

    def test_with_no_thread_waiting_nothing_is_confirmed_or_answered(self):
        seam = F.FakeSeam()
        seam.listing = listing(("t5", "Add a clamp helper"))
        update = tasks_over(self, seam).about("k1", APPROVE, "I approve.", 0)
        self.assertEqual((update.acts, seam.ops("respond")), ((act(S.NOTHING_PENDING),), []))

    def test_a_thread_waiting_on_a_question_is_read_out_and_not_approved(self):
        seam = self.seam(pending=F.need("q1", "Which fixture?", kind=T.PendingKind.QUESTION,
                                        source=T.NeedSource.USER_INPUT))
        update = tasks_over(self, seam).about("k1", APPROVE, "I approve.", 0)
        self.assertEqual(update.acts[0].kind, S.THREAD_STATUS)
        self.assertEqual(update.acts[0].slots["need"], "question")
        self.assertEqual(seam.ops("respond"), [])

    def test_several_threads_waiting_ask_which_and_the_answer_is_still_confirmed(self):
        seam = self.seam(("t5", "Add a clamp helper"), ("t6", "Fix the flaky ASR test"))
        decider = F.Reader({"The ASR one.": ("other", "Fix the flaky ASR test")},
                           {"I approve.": {"unclear": 0.4, "t5": 0.3, "t6": 0.3}})
        t3 = tasks_over(self, seam, decider=decider)
        update = t3.about("k1", APPROVE, "I approve.", 0)
        self.assertEqual(update.acts[0].kind, S.ASK_WHICH)
        t3.spoke(update)
        seam.read = F.state(thread="t6", title="Fix the flaky ASR test", as_of=150,
                            activity=T.ThreadActivity.WAITING_ON_YOU, pending=F.need("q9", "Run the tests?"))
        confirm = t3.answer("The ASR one.", 1)
        self.assertEqual((confirm.acts[0].kind, confirm.acts[0].slots["thread"]),
                         (S.CONFIRM_RESPOND, "Fix the flaky ASR test"))
        self.assertEqual(seam.ops("respond"), [], "a choice is not a yes")

    def test_two_threads_with_one_title_are_never_offered_for_an_answer(self):
        seam = self.seam(("t5", "Add a clamp helper"), ("t6", "Add a clamp helper"))
        update = tasks_over(self, seam).about("k1", APPROVE, "I approve.", 0)
        self.assertEqual((update.acts[0].kind, seam.ops("respond")), (S.SAME_TITLE, []))

    def test_a_refusal_is_reported_as_t3s_and_a_dead_seam_as_unreachable(self):
        for failure, kind in ((F.refusal("invalid_request", "That request is not waiting any more."), S.REFUSED),
                              (F.unavailable(), S.UNREACHABLE)):
            with self.subTest(kind=kind):
                seam, t3, _ = self.asked()
                seam.answer("respond", failure)
                update = t3.answer("Yes.", 1)
                self.assertEqual(update.acts[0].kind, kind)
                self.assertEqual(update.status, "failed")

    def test_one_confirmation_answers_one_request_and_a_second_yes_answers_nothing(self):
        seam, t3, _ = self.asked()
        seam.answer("respond", F.responded("t5", "q1", "approve"))
        t3.answer("Yes.", 1)
        self.assertIsNone(t3.answer("Yes.", 2))
        self.assertEqual(len(seam.ops("respond")), 1)

    def test_a_retry_of_the_journaled_answer_sends_the_same_request_id(self):
        seam, t3, _ = self.asked()
        seam.answer("respond", F.unavailable(), F.responded("t5", "q1", "approve"))
        t3.answer("Yes.", 1)
        task = next(t for t in t3.follow.journal.tasks.values() if t.kind is Kind.RESPOND)
        t3.follow.respond(task, 2)
        ids = [c[1] for c in seam.ops("respond")]
        self.assertEqual((len(ids), ids[0] == ids[1] == task.link.respond["request_id"]), (2, True))

    def test_a_restart_never_answers_for_a_confirmation_nobody_is_there_for(self):
        seam, t3, _ = self.asked()
        seam.answer("respond", F.unavailable())
        t3.answer("Yes.", 1)
        task = next(t for t in t3.follow.journal.tasks.values() if t.kind is Kind.RESPOND)
        task.status = Status.UNSTARTED
        t3.follow.resume(3)
        self.assertEqual((task.status, len(seam.ops("respond"))), (Status.FAILED, 1))

    def test_a_second_segment_of_the_utterance_that_asked_is_part_of_it_and_asks_nothing_more(self):
        seam = self.seam()
        t3 = tasks_over(self, seam)
        t3.about("conv_turn_001:task_1", APPROVE, "Go ahead", 0)
        self.assertTrue(t3.asking_start("conv_turn_001:"), "the next segment of this turn is covered by that question")
        self.assertFalse(t3.asking_start("conv_turn_002:"), "a later turn is a new request")

    def test_the_tools_are_offered_only_while_a_thread_waits_and_the_decider_is_shown_what_for(self):
        base = SimpleNamespace(choices=lambda: {"weather": "w", "none": "n"})
        waiting = [{"thread": "Add a clamp helper", "asks_for": "approval to run a command"}]
        registry = T3Registry(base, lambda: [])
        self.assertTrue({APPROVE, DENY}.isdisjoint(registry.choices()))
        registry = T3Registry(base, lambda: waiting)
        offered = registry.choices()
        self.assertTrue({APPROVE, DENY} <= offered.keys())
        self.assertIn("Add a clamp helper asks for approval to run a command.", offered[APPROVE])
        self.assertEqual(registry.approvals, waiting)

    def test_the_pending_requests_the_decider_sees_are_approvals_the_voice_can_answer(self):
        seam = self.seam(("t5", "Add a clamp helper"))
        t3 = tasks_over(self, seam)
        t3.follow.ensure(0)
        self.assertEqual(t3.pending_requests(), [{"thread": "Add a clamp helper", "asks_for": "approval to run a command"}])
        seam.read = F.state(thread="t5", title="Add a clamp helper", activity=T.ThreadActivity.WAITING_ON_YOU,
                            pending=F.need("q1", "Which?", kind=T.PendingKind.QUESTION, source=T.NeedSource.USER_INPUT))
        self.assertEqual(t3.pending_requests(), [])


if __name__ == "__main__":
    unittest.main()


class SpokenAbout(unittest.TestCase):
    """The thread a line was about is what "it" means on the next turn, whoever's line it was: an answer, a launch,
    a message or news."""

    def test_the_last_line_about_a_thread_names_it_and_a_new_call_forgets_it(self):
        t3 = tasks_over(self, F.FakeSeam())
        t3.begin_call()
        self.assertIsNone(t3.spoken_about())
        t3.spoke(L.Update("conv_turn_001:task_1", (act(S.LAUNCHED, project="Voice sandbox"),), "completed", "launch"))
        self.assertEqual(t3.spoken_about(), "the new thread in Voice sandbox")
        t3.spoke(L.Update("conv_turn_003:task_1", (act(S.THREAD_STATUS, thread="Add a Retry Button", status="working"),),
                          "completed", "ask", "thread-7"))
        self.assertEqual((t3.spoken_about(), t3.recent), ("Add a Retry Button", "thread-7"))
        t3.spoke(L.Update("conv_turn_004:task_1", (act(S.NO_THREAD, recent=3),), "completed", "ask"))
        self.assertEqual(t3.spoken_about(), "Add a Retry Button", "a line about no thread changes nothing")
        t3.begin_call()
        self.assertIsNone(t3.spoken_about())

    def test_a_status_asked_again_with_nothing_new_says_so(self):
        seam = F.FakeSeam(read=F.state(thread="t5", title="Add a clamp helper", as_of=140))
        seam.listing = listing(("t5", "Add a clamp helper"), ("t6", "Fix the flaky ASR test"))
        t3 = tasks_over(self, seam)
        t3.begin_call()

        def ask(n):
            update = t3.about(f"k{n}", ASK, "How is the clamp helper thread going?", n)
            if update.acts[0].kind is S.CONFIRM_THREAD:
                t3.spoke(update)
                update = t3.answer("yes", n)
            t3.spoke(update)
            return update.acts[0].slots.get("since_last_asked")

        self.assertIsNone(ask(1))
        self.assertIsNotNone(ask(2), "the same read as the status just said")
        again = t3.about("k9", ASK, "How is it doing?", 9)
        self.assertEqual(said(again), [("thread_status", "Add a clamp helper", "working")],
                         "a leaning pick of the thread just spoken about is not asked back")
        seam.read = F.state(thread="t5", title="Add a clamp helper", as_of=141)
        self.assertIsNone(ask(3), "the thread reported something since")
        t3.begin_call()
        self.assertIsNone(ask(4), "a new call has heard nothing yet")
