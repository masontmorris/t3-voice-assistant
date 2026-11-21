"""One case per row of "The watch lifecycle" (docs/voice/seam-design.md), against a fake seam and a fake clock.

Each test is named for its row and asserts that row's proof. The schema test fails when the seam schema
holds a watch_ended reason or a refusal code that lifecycle.py does not route.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from voice.t3 import fakes as F
from voice.t3 import lifecycle as L
from voice.t3 import types as T
from voice.t3.acts import Act
from voice.t3.acts import Kind as S
from voice.t3.journal import Kind, Status, T3Journal

SCHEMA = json.loads((Path(__file__).parent / "seam.schema.json").read_text(encoding="utf-8"))["$defs"]
NON_UNAVAILABLE = [c for c in T.RefusalCode if c is not T.RefusalCode.UNAVAILABLE]


class Harness:
    def __init__(self, test: unittest.TestCase, *entries, seam: F.FakeSeam | None = None):
        tmp = tempfile.TemporaryDirectory()
        test.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "journal.json"
        self.seam = seam or (entries[0] if entries and isinstance(entries[0], F.FakeSeam) else F.FakeSeam())
        self.connector = F.Connector(*(entries or (self.seam,)))
        self.journal = T3Journal(self.path)
        self.follow = L.Follow(self.journal, self.connector)
        self.heard: list[L.Update] = []

    def restart(self, *entries) -> None:
        self.seam = entries[0] if entries and isinstance(entries[0], F.FakeSeam) else self.seam
        self.connector = F.Connector(*(entries or (self.seam,)))
        self.journal = T3Journal(self.path)
        self.follow = L.Follow(self.journal, self.connector)

    def start(self, now: float = 0.0) -> T3Journal:
        self.follow.ensure(now)
        return self.journal

    def live(self, cursor: int | None = 10, thread: str = "t1", run: str = "r1"):
        task = self.journal.begin(Kind.START, "Fix the flaky test", "p1", "Voice sandbox")
        task.status, task.announced = Status.LIVE, True
        task.link.thread_id, task.link.run_id, task.link.cursor = thread, run, cursor
        self.journal.save()
        return task

    def unstarted(self, kind: Kind = Kind.START, thread: str | None = None):
        return self.journal.begin(kind, "Fix the flaky test", "p1", "Voice sandbox", thread_id=thread)

    def tick(self, now: float) -> list[str]:
        self.follow.tick(now)
        return self.lines()

    def lines(self) -> list[str]:
        """The lines the voice took and heard whole since the last call."""
        new = self.follow.take()
        self.follow.ack(new)
        self.heard.extend(new)
        return [kinds(u) for u in new]

    def said(self) -> list[str]:
        self.lines()
        return [kinds(u) for u in self.heard]

    def acts(self) -> list[Act]:
        self.lines()
        return [a for u in self.heard for a in u.acts]


def kinds(update: L.Update) -> str:
    """An update's acts by kind, joined by "+", which is what most rows assert. Slots get their own assertions."""
    return "+".join(a.kind.value for a in update.acts)


class WatchLifecycleRows(unittest.TestCase):
    def test_launch_fresh(self):
        h = Harness(self)
        task = h.unstarted()
        h.follow.launch(task, 0)
        self.assertEqual(h.seam.ops("watch"), [("watch", "t1", 93, "r1")])
        self.assertEqual(task.status, Status.LIVE)
        self.assertEqual(h.follow.outbox[0].sequences, (93,))
        self.assertEqual(h.acts(), [Act(S.LAUNCHED, {"project": "Voice sandbox"})])

    def test_launch_replayed(self):
        seam = F.FakeSeam(read=F.state(as_of=120, pending=F.need()))
        seam.answer("launch", F.launched(sequence=118, replayed=True))
        h = Harness(self, seam)
        task = h.unstarted()
        h.follow.launch(task, 0)
        self.assertEqual(seam.ops("read"), [("read", "t1")])
        self.assertEqual(seam.ops("watch"), [("watch", "t1", 120, "r1")])
        self.assertEqual(h.said(), ["launched", "needs_you"])
        self.assertEqual(h.acts()[1].slots, {"thread": "Fix the flaky test", "need": "command_approval",
                                             "prompt": "Run the tests?", "waiting_for": "approval",
                                             "after_end": False})
        seam.push(F.needs_you(130), F.ended("resync"))
        h.tick(1)
        self.assertEqual(h.said().count("needs_you"), 1)
        self.assertEqual(seam.ops("watch")[-1], ("watch", "t1", 120, "r1"))

    def test_a_need_carries_its_kind_from_what_raised_it(self):
        K, N = T.PendingKind, T.NeedSource
        cases = {(K.APPROVAL, N.COMMAND): "command_approval", (K.APPROVAL, N.FILE_CHANGE): "file_change",
                 (K.APPROVAL, N.FILE_READ): "file_read", (K.APPROVAL, N.PERMISSION): "permission",
                 (K.APPROVAL, N.MCP_ELICITATION): "tool_input", (K.QUESTION, N.USER_INPUT): "question",
                 (K.IN_APP, N.LINK_REQUEST): "link", (K.IN_APP, N.SECRET_REQUEST): "secret",
                 (K.IN_APP, N.CARD): "card", (K.IN_APP, N.COMMAND): "unanswerable"}
        for (kind, source), need in cases.items():
            with self.subTest(source=source.value, kind=kind.value):
                h = Harness(self, F.FakeSeam(read=F.state(as_of=120)))
                h.seam.answer("launch", F.launched(sequence=118))
                h.follow.launch(h.unstarted(), 0)
                h.seam.push(F.needs_you(130, prompt="cd app && make test", kind=kind, source=source))
                h.tick(1)
                self.assertEqual(h.acts()[-1].slots["need"], need)

    def test_launch_refused(self):
        lines = {}
        for code in NON_UNAVAILABLE:
            with self.subTest(code=code.value):
                seam = F.FakeSeam().answer("launch", F.refusal(code.value, f"Server says {code.value}."))
                h = Harness(self, seam)
                task = h.unstarted()
                h.follow.launch(task, 0)
                self.assertEqual(task.status, Status.FAILED)
                self.assertEqual(seam.ops("watch"), [])
                lines[code] = h.said()
        self.assertEqual(lines[T.RefusalCode.GONE], ["refused"])
        refused = F.FakeSeam().answer("launch", F.refusal("refused", "T3 refused it.", reason="mode too high"))
        bare = F.FakeSeam().answer("launch", F.refusal("refused", "T3 refused it."))
        for seam, reason in ((refused, "mode too high"), (bare, "T3 refused it.")):
            h = Harness(self, seam)
            task = h.unstarted()
            h.follow.launch(task, 0)
            self.assertEqual(h.acts(), [Act(S.REFUSED, {"op": "start", "code": "refused", "reason": reason})])

    def test_launch_unavailable(self):
        seam = F.FakeSeam().answer("launch", *[F.unavailable()] * 8)
        h = Harness(self, seam)
        task = h.unstarted()
        h.follow.launch(task, 0)
        now = 0.0
        for wait in L.RETRY_WAITS_S[:7]:
            now += wait
            h.tick(now)
        ids = {call[1] for call in seam.ops("launch")}
        self.assertEqual(len(seam.ops("launch")), 8)
        self.assertEqual(ids, {task.link.request_id})
        self.assertEqual(h.said(), ["launch_pending",
                                    "launch_unconfirmed"])
        self.assertEqual(task.status, Status.LOST)

    def test_tell(self):
        seam = F.FakeSeam(read=F.state(as_of=200, pending=F.need("q7", "Which branch?")))
        seam.answer("send", F.launched(sequence=190, replayed=False))
        h = Harness(self, seam)
        task = h.unstarted(Kind.TELL, thread="t1")
        h.follow.send(task, 0)
        self.assertEqual(seam.ops("watch"), [("watch", "t1", 200, "r1")])
        self.assertEqual(h.said(), ["sent_followup", "needs_you"])

    def test_tell_refused(self):
        for code in (c for c in NON_UNAVAILABLE if c is not T.RefusalCode.THREAD_BUSY):
            with self.subTest(code=code.value):
                seam = F.FakeSeam().answer("send", F.refusal(code.value, "Not that one."))
                h = Harness(self, seam)
                task = h.unstarted(Kind.TELL, thread="t1")
                h.follow.send(task, 0)
                self.assertEqual(task.status, Status.FAILED)
                self.assertEqual(h.acts(), [Act(S.REFUSED, {"op": "tell", "code": code.value,
                                                             "reason": "Not that one."})])

    def test_tell_unavailable(self):
        h = Harness(self)
        task = h.unstarted(Kind.TELL, thread="t1")
        h.follow.send(task, 0)
        now = 0.0
        for wait in L.RETRY_WAITS_S[:7]:
            now += wait
            h.tick(now)
        self.assertEqual({c[1] for c in h.seam.ops("send")}, {task.link.request_id})
        self.assertEqual(len(h.seam.ops("send")), 8)
        self.assertEqual(h.said(), ["tell_unconfirmed"])

    def held(self, h: Harness, thread: str = "t1"):
        task = h.unstarted(Kind.TELL, thread=thread)
        h.seam.answer("send", F.busy())
        h.follow.send(task, 0)
        return task

    def test_tell_busy(self):
        seam = F.FakeSeam(read=F.state(as_of=150))
        h = Harness(self, seam)
        task = h.unstarted(Kind.TELL, thread="t1")
        refused_id = task.link.request_id
        seam.answer("send", F.busy())
        h.follow.send(task, 0)
        self.assertEqual(task.status, Status.HELD)
        self.assertNotEqual(task.link.request_id, refused_id)
        self.assertEqual(T3Journal(h.path).tasks[task.task_id].link.request_id, task.link.request_id)
        self.assertEqual(seam.ops("watch"), [("watch", "t1", 150, None)])
        seam.push(F.run_event(151), F.reply(152), F.needs_you(153))
        h.tick(1)
        self.assertEqual(h.said(), ["held_followup", "needs_you"])
        self.assertEqual(len(seam.ops("send")), 1)

    def test_busy_follow_up_releases_when_the_read_already_shows_the_run_ended(self):
        for terminal in T.TerminalStatus:
            with self.subTest(status=terminal):
                ended = replace(F.state(as_of=150), run=T.RunInfo("r1", T.RunStatus(terminal.value), None))
                running = replace(F.state(as_of=170), run=T.RunInfo("r2", T.RunStatus.RUNNING, None))
                seam = F.FakeSeam().answer("read", ended, running)
                seam.answer("send", F.busy(), F.sent(run="r2", sequence=170))
                h = Harness(self, seam)
                task = h.unstarted(Kind.TELL, thread="t1")
                refused_id = task.link.request_id

                h.follow.send(task, 0)

                self.assertEqual(task.status, Status.LIVE)
                self.assertNotEqual(refused_id, task.link.request_id)
                self.assertEqual([call[1] for call in seam.ops("send")], [refused_id, task.link.request_id])
                self.assertEqual(seam.ops("watch"), [("watch", "t1", 170, "r2")])

                seam.push(F.done(150, run="r1", status=terminal))
                h.tick(1)
                self.assertEqual(len(seam.ops("send")), 2)
                self.assertEqual(T3Journal(h.path).tasks[task.task_id].status, Status.LIVE)
                self.assertEqual(h.said(), ["sent_followup"])

    def test_a_busy_release_rechecks_after_another_run_ends(self):
        ended = replace(F.state(as_of=150), run=T.RunInfo("r1", T.RunStatus.COMPLETED, None))
        later_ended = replace(ended, run=T.RunInfo("r2", T.RunStatus.COMPLETED, None), as_of_sequence=160)
        running = replace(ended, run=T.RunInfo("r3", T.RunStatus.RUNNING, None), as_of_sequence=170)
        seam = F.FakeSeam().answer("read", ended, later_ended, later_ended, running)
        seam.answer("send", F.busy(), F.busy(), F.sent(run="r3"))
        h = Harness(self, seam)
        task = h.unstarted(Kind.TELL, thread="t1")

        h.follow.send(task, 0)

        self.assertEqual(task.status, Status.HELD)
        self.assertEqual(len(seam.ops("send")), 2)
        saved = T3Journal(h.path).tasks[task.task_id].link.request_id
        h.tick(1)
        self.assertEqual([call[1] for call in seam.ops("send")][-1], saved)
        self.assertEqual(seam.ops("watch"), [("watch", "t1", 170, "r3")])
        self.assertEqual(h.said(), ["held_followup", "sent_followup"])

    def test_held_release(self):
        seam = F.FakeSeam(read=F.state(as_of=40))
        h = Harness(self, seam)
        start = h.live(cursor=10)
        h.start()
        first, second = self.held(h), self.held(h)
        saved = first.link.request_id
        self.assertEqual(seam.ops("watch"), [("watch", "t1", 10, "r1")])
        h.tick(1)
        self.assertEqual(len(seam.ops("send")), 2)

        seam.answer("send", F.sent(run="r2", sequence=30), F.busy())
        seam.push(F.done(20))
        h.tick(2)
        self.assertEqual(start.status, Status.COMPLETED)
        self.assertEqual((first.status, second.status), (Status.LIVE, Status.HELD))
        self.assertEqual(seam.ops("send")[2][1], saved)
        self.assertEqual(seam.ops("watch")[-1], ("watch", "t1", 40, "r2"))
        self.assertEqual(h.follow.watching, {"t1": first.task_id})

        seam.answer("send", F.sent(run="r3", sequence=60))
        seam.push(F.done(50, run="r2", text="Added the test."))
        h.tick(3)
        self.assertEqual(second.status, Status.LIVE)
        self.assertEqual(seam.ops("watch")[-1], ("watch", "t1", 40, "r3"))
        self.assertEqual(h.said(), ["held_followup", "held_followup", "done",
                                    "sent_followup", "done",
                                    "sent_followup"])

    def test_held_dropped(self):
        h = Harness(self, F.FakeSeam(read=F.state(as_of=150)))
        task = self.held(h)
        h.follow.drop(task, 1)
        self.assertEqual(task.status, Status.CANCELLED)
        self.assertEqual(h.seam.ops("unwatch"), [("unwatch", "t1")])
        h.seam.push(F.done(160))
        h.tick(2)
        self.assertEqual(len(h.seam.ops("send")), 1)
        self.assertEqual(h.said(), ["held_followup", "dropped_followup"])

    def test_restart_held(self):
        h = Harness(self, F.FakeSeam(read=F.state(as_of=150)))
        task = self.held(h)
        saved = task.link.request_id
        seam = F.FakeSeam().answer("send", F.sent(run="r2"))
        h.restart(seam)
        h.follow.resume(0)
        self.assertEqual(seam.ops("watch"), [("watch", "t1", 150, None)])
        seam.push(F.done(170))
        h.tick(1)
        self.assertEqual(seam.ops("send")[0][1:3], (saved, "t1"))
        self.assertEqual(h.follow.journal.tasks[task.task_id].status, Status.LIVE)

    def test_restart_releases_held_follow_up_when_the_read_shows_done(self):
        for cursor in (None, 100):
            with self.subTest(cursor=cursor):
                h = Harness(self)
                task = h.unstarted(Kind.TELL, thread="t1")
                task.status, task.link.cursor = Status.HELD, cursor
                h.journal.save()
                ended = replace(F.state(as_of=150), run=T.RunInfo("r1", T.RunStatus.COMPLETED, None))
                running = replace(F.state(as_of=170), run=T.RunInfo("r2", T.RunStatus.RUNNING, None))
                seam = F.FakeSeam().answer("read", ended, running).answer("send", F.sent(run="r2", sequence=170))
                h.restart(seam)

                h.follow.resume(0)
                h.tick(1)

                self.assertEqual(seam.ops("send"), [("send", task.link.request_id, "t1", task.instruction)])
                self.assertEqual(seam.ops("watch"), [("watch", "t1", 170, "r2")])
                self.assertEqual(T3Journal(h.path).tasks[task.task_id].status, Status.LIVE)
                self.assertEqual(h.said(), ["sent_followup"])

    def test_ask(self):
        from voice.live.t3_tasks import T3Tasks

        seam = F.FakeSeam(read=F.state(thread="t5", title="Fix the flaky test", activity=T.ThreadActivity.WORKING))
        seam.listing = T.ThreadList((T.ThreadSummary("t5", "p1", "Fix the flaky test", T.ThreadActivity.WORKING,
                                                     "2026-11-17T10:00:00Z", False),), 1, False, 77)
        h = Harness(self, seam)
        tasks = T3Tasks(h.follow, decider=F.Reader({"yes": "yes"}))
        tasks.about("b1", "t3_ask", "How is the flaky test thread going?", 0)
        answer = tasks.answer("yes", 1)
        self.assertEqual(seam.ops("watch"), [])
        self.assertEqual(answer.acts, (Act(S.THREAD_STATUS, {"thread": "Fix the flaky test", "status": "working",
                                                             "asked": "How is the flaky test thread going?"}),))
        self.assertEqual(answer.sequences, (120,))
        broken = Harness(self, F.FakeSeam().answer("list", F.unavailable()))
        failed = T3Tasks(broken.follow).about("b2", "t3_ask", "How is the flaky test thread going?", 0)
        self.assertEqual((failed.status, kinds(failed)), ("failed", "unreachable"))

    def test_restart_unstarted(self):
        seam = F.FakeSeam(read=F.state(as_of=140))
        h = Harness(self, seam)
        task = h.unstarted()
        h.restart(seam.answer("launch", F.launched(sequence=133, replayed=True)))
        h.follow.resume(0)
        self.assertEqual(seam.ops("launch")[0][1], task.link.request_id)
        self.assertIn(("restart-unstarted", task.task_id), h.follow.trail)
        self.assertIn(("launch-replayed", task.task_id), h.follow.trail)
        self.assertEqual(seam.ops("watch"), [("watch", "t1", 140, "r1")])

    def test_restart_live(self):
        h = Harness(self)
        h.live(cursor=10, thread="t1")
        h.live(cursor=55, thread="t2")
        h.restart(h.seam)
        h.follow.resume(0)
        self.assertEqual(sorted(h.seam.ops("watch")), [("watch", "t1", 10, "r1"), ("watch", "t2", 55, "r1")])

    def test_restart_ended(self):
        h = Harness(self)
        for status in (Status.COMPLETED, Status.FAILED, Status.CANCELLED, Status.LOST):
            h.live().status = status
        h.journal.save()
        h.restart(h.seam)
        h.follow.resume(0)
        self.assertEqual(h.seam.ops("watch"), [])
        self.assertEqual(h.seam.ops("read"), [])

    def test_crash_before_save(self):
        class Crash(Exception):
            pass

        def crash_on_save(journal):
            def save():
                raise Crash()
            journal.save = save

        cases = {
            "cursor event": ([F.needs_you(11)], 0, "needs_you"),
            "done": ([F.done(12)], 0, "done"),
            "give-up line": ([F.ended("not_found")], 0, "thread_gone"),
        }
        for label, (items, now, line) in cases.items():
            with self.subTest(label):
                h = Harness(self)
                h.live(cursor=10)
                h.start()
                crash_on_save(h.journal)
                h.seam.push(*items)
                with self.assertRaises(Crash):
                    h.tick(now)
                spoken = [kinds(u) for u in h.follow.outbox]
                h.restart(h.seam)
                h.follow.resume(0)
                h.seam.push(*items)
                h.tick(1)
                spoken += h.said()
                self.assertEqual(spoken.count(line), 2, spoken)

    def test_crash_after_save(self):
        h = Harness(self)
        h.live(cursor=10)
        h.start()
        h.seam.push(F.needs_you(11), F.done(12))
        h.tick(0)
        first = h.said()
        h.restart(h.seam)
        h.follow.resume(1)
        h.seam.push(F.needs_you(11), F.done(12))
        h.tick(2)
        self.assertEqual(len(first), 2)
        self.assertEqual(h.said(), first)
        self.assertEqual(len(h.seam.ops("watch")), 1)

    def test_needs_you_waiting_for_a_turn_survives_restart(self):
        self.assert_pending_line_survives_restart(
            F.needs_you(11), "needs_you", (11,), watch_after=11,
        )

    def test_done_waiting_for_a_turn_survives_restart(self):
        self.assert_pending_line_survives_restart(
            F.done(12), "done", (12, 13), watch_after=None,
        )

    def assert_pending_line_survives_restart(self, event, line, evidence, *, watch_after):
        h = Harness(self)
        h.live()
        h.start()
        h.seam.push(event)
        h.follow.tick(1)

        h.restart(F.FakeSeam())
        h.follow.resume(2)
        expected_watches = [] if watch_after is None else [("watch", "t1", watch_after, "r1")]
        self.assertEqual(h.seam.ops("watch"), expected_watches)
        taken = h.follow.take()
        self.assertEqual([(kinds(u), u.sequences) for u in taken], [(line, evidence)])
        self.assertEqual(h.follow.take(), [])
        h.follow.ack(taken)

        h.restart(F.FakeSeam())
        h.follow.resume(3)
        self.assertEqual(h.follow.take(), [])

    def test_slot_wait(self):
        h = Harness(self, F.FakeSeam(max_watches=16))
        tasks = [h.live(cursor=10 + i, thread=f"t{i}") for i in range(17)]
        h.start()
        self.assertEqual(len(h.seam.ops("watch")), 16)
        self.assertEqual(h.said(), ["watch_queued"])
        self.assertEqual(h.acts()[0].slots["following"], 16)
        h.seam.push(F.done(40, thread="t0"))
        h.tick(1)
        h.tick(1 + L.DONE_WINDOW_S)
        self.assertEqual(h.seam.ops("watch")[-1], ("watch", "t16", 26, "r1"))
        self.assertEqual(tasks[16].status, Status.LIVE)

    def test_watch_retry(self):
        for code in ("unavailable", "too_many_watches"):
            with self.subTest(code=code):
                answer = F.unavailable() if code == "unavailable" else F.refusal(code)
                seam = F.FakeSeam().answer("watch", *[answer] * 8)
                h = Harness(self, seam)
                task = h.live(cursor=10)
                h.start()
                now = 0.0
                for wait in L.RETRY_WAITS_S[:7]:
                    self.assertEqual(task.status, Status.LIVE)
                    now += wait
                    h.tick(now)
                self.assertEqual(len(seam.ops("watch")), 8)
                self.assertEqual({c[2] for c in seam.ops("watch")}, {10})
                self.assertEqual(h.said(), ["lost_track"])
                self.assertEqual(task.status, Status.LOST)

    def test_watch_refused(self):
        codes = [c for c in NON_UNAVAILABLE if c is not T.RefusalCode.TOO_MANY_WATCHES]
        for code in codes:
            with self.subTest(code=code.value):
                h = Harness(self, F.FakeSeam().answer("watch", F.refusal(code.value)))
                task = h.live()
                h.start()
                expected = "thread_gone" if code.value in ("project_not_allowed", "not_found", "gone") else "lost_track"
                self.assertEqual(h.said(), [expected])
                self.assertEqual(task.status, Status.LOST)
                self.assertEqual(len(h.seam.ops("watch")), 1)

    def test_ended_unavailable(self):
        h = Harness(self)
        task = h.live(cursor=10)
        h.start()
        h.seam.push(F.run_event(11), F.ended("unavailable"))
        h.tick(0)
        h.tick(0.5)
        self.assertEqual(len(h.seam.ops("watch")), 1)
        h.tick(1)
        self.assertEqual(h.seam.ops("watch")[-1], ("watch", "t1", 11, "r1"))
        now = 1.0
        for wait in L.RETRY_WAITS_S[1:7]:
            h.seam.push(F.ended("unavailable"))
            h.tick(now)
            now += wait
            h.tick(now)
        self.assertEqual(len(h.seam.ops("watch")), 8)
        self.assertEqual({c[2] for c in h.seam.ops("watch")[1:]}, {11})
        h.seam.push(F.ended("unavailable"))
        h.tick(now)
        self.assertEqual(h.said(), ["run_update", "lost_track"])
        self.assertEqual(task.status, Status.LOST)

    def test_ended_resync(self):
        seam = F.FakeSeam(read=F.state(as_of=300))
        h = Harness(self, seam)
        h.live(cursor=10)
        h.start()
        seam.push(F.ended("resync"))
        h.tick(0)
        self.assertEqual(seam.ops("read"), [("read", "t1")])
        self.assertEqual(seam.ops("watch")[-1], ("watch", "t1", 300, "r1"))

    def test_read_failed(self):
        h = Harness(self, F.FakeSeam().answer("read", F.refusal("not_found")))
        task = h.live(cursor=None)
        h.start()
        self.assertEqual((task.status, h.said()), (Status.LOST, ["thread_gone"]))
        seam = F.FakeSeam().answer("read", *[F.unavailable()] * 2)
        h = Harness(self, seam)
        task = h.live(cursor=None)
        h.start(0)
        h.tick(1)
        h.tick(3)
        self.assertEqual(len(seam.ops("read")), 3)
        self.assertEqual(seam.ops("watch"), [("watch", "t1", 120, "r1")])
        self.assertEqual(task.status, Status.LIVE)

    def test_ended_not_found(self):
        for reason in ("not_found", "gone"):
            with self.subTest(reason=reason):
                h = Harness(self)
                task = h.live()
                h.start()
                h.seam.push(F.ended(reason))
                h.tick(0)
                self.assertEqual((task.status, h.said()), (Status.LOST, ["thread_gone"]))

    def test_gone_event(self):
        h = Harness(self)
        task = h.live(cursor=10)
        h.start()
        h.seam.push(F.gone(14), F.ended("gone"))
        h.tick(0)
        h.tick(5)
        self.assertEqual((task.status, h.said()), (Status.FAILED, ["thread_deleted"]))
        self.assertEqual(len(h.seam.ops("watch")), 1)

    def test_done_event(self):
        h = Harness(self)
        task = h.live(cursor=10)
        h.start()
        h.seam.push(F.done(20))
        h.tick(0)
        h.seam.push(F.needs_you(22, "q2", "Merge it?"))
        h.tick(4)
        self.assertEqual(h.said(), ["done", "needs_you"])
        self.assertEqual([a.slots.get("status") for a in h.acts()], ["completed", None])
        self.assertEqual(h.acts()[1].slots["after_end"], True)
        self.assertEqual(task.status, Status.COMPLETED)
        self.assertEqual(h.seam.ops("unwatch"), [])
        h.tick(L.DONE_WINDOW_S)
        self.assertEqual(h.seam.ops("unwatch"), [("unwatch", "t1")])

    def test_ended_after_terminal(self):
        h = Harness(self)
        h.live(cursor=10)
        h.start()
        h.seam.push(F.done(20), F.ended("resync"))
        h.tick(0)
        calls = list(h.seam.calls)
        h.tick(1)
        h.tick(L.DONE_WINDOW_S + 1)
        self.assertEqual(h.seam.calls, calls)

    def test_close_1013(self):
        first, second, third = F.FakeSeam(), F.FakeSeam(), F.FakeSeam()
        h = Harness(self, first, second, third)
        h.live(cursor=10, thread="t1")
        h.live(cursor=44, thread="t2")
        h.start()
        first.push(T.CLOSE_1013)
        h.tick(0)
        self.assertEqual(sorted(second.ops("watch")), [("watch", "t1", 10, "r1"), ("watch", "t2", 44, "r1")])
        second.push(T.CLOSE_1013)
        h.tick(0)
        self.assertEqual(third.calls, [])
        h.tick(1)
        self.assertEqual(len(third.ops("watch")), 2)

    def test_close_other(self):
        first, after = F.FakeSeam(), F.FakeSeam()
        h = Harness(self, first, T.UNREACHABLE, T.UNREACHABLE, after)
        task = h.live(cursor=10)
        h.start()
        first.push(F.run_event(12), T.CloseOther(1006))
        h.tick(0)
        h.tick(1)
        h.tick(3)
        self.assertEqual(after.calls, [])
        h.tick(7)
        self.assertEqual(after.ops("watch"), [("watch", "t1", 12, "r1")])
        self.assertEqual(task.status, Status.LIVE)
        h = Harness(self, F.FakeSeam(), *[T.UNREACHABLE] * 8)
        h.live(cursor=10)
        h.live(cursor=20, thread="t2")
        h.start()
        h.seam.push(T.CloseOther(1006))
        now = 0.0
        h.tick(now)
        for wait in L.RETRY_WAITS_S[:7]:
            now += wait
            h.tick(now)
        self.assertEqual(h.said(), ["seam_down"])
        self.assertEqual({t.status for t in h.journal.tasks.values()}, {Status.LOST})

    def test_upgrade_401_or_403(self):
        for end in (T.CLOSE_1008, T.UPGRADE_401, T.UPGRADE_403):
            with self.subTest(end=end.label):
                h = Harness(self, F.FakeSeam(), F.FakeSeam())
                h.live()
                h.live(thread="t2")
                h.start()
                h.seam.push(end)
                h.tick(0)
                h.tick(60)
                self.assertEqual(h.connector.count, 1)
                self.assertEqual(h.said(), ["seam_down"])
        h = Harness(self, T.UPGRADE_403)
        h.live()
        h.start()
        h.tick(30)
        self.assertEqual((h.connector.count, h.said()), (1, ["seam_down"]))

    def test_upgrade_404(self):
        h = Harness(self, T.UPGRADE_404)
        task = h.live()
        h.start()
        h.tick(1)
        h.tick(100)
        self.assertEqual(h.connector.count, 1)
        self.assertEqual((task.status, h.said()), (Status.LOST, ["seam_down"]))

    def test_every_row_has_a_case(self):
        cases = {name[len("test_"):] for name in dir(self) if name.startswith("test_")}
        missing = [row for row in L.ROWS if row.replace("-", "_") not in cases]
        self.assertEqual(missing, [])
        self.assertEqual(len(L.ROWS), 31)


class Questions(unittest.TestCase):
    """A thread's question is told with its options, and answered once through the seam."""

    ASKS = (F.question("db", "Which database should it use?", ("Postgres", "SQLite")),
            F.question("name", "What should the table be called?", ("users", "accounts"), own_words=True))

    def test_a_question_the_voice_can_ask_is_told_with_every_option_of_its_first_question(self):
        seam = F.FakeSeam(read=F.state(as_of=120)).answer("launch", F.launched(sequence=118))
        h = Harness(self, seam)
        h.follow.launch(h.unstarted(), 0)
        seam.push(F.needs_you(130, kind=T.PendingKind.QUESTION, source=T.NeedSource.USER_INPUT, questions=self.ASKS))
        h.tick(1)
        self.assertEqual(h.acts()[-1], Act(S.ASK_QUESTION, {"question": "Which database should it use?",
                                                           "options": ["Postgres", "SQLite"], "number": 1, "count": 2}))

    def test_a_question_with_its_own_words_allowed_says_so_and_one_question_has_no_count(self):
        seam = F.FakeSeam(read=F.state(as_of=120)).answer("launch", F.launched(sequence=118))
        h = Harness(self, seam)
        h.follow.launch(h.unstarted(), 0)
        seam.push(F.needs_you(130, kind=T.PendingKind.QUESTION, source=T.NeedSource.USER_INPUT,
                              questions=self.ASKS[1:]))
        h.tick(1)
        self.assertEqual(h.acts()[-1].slots["own_words"], True)
        self.assertNotIn("count", h.acts()[-1].slots)

    def test_a_question_the_voice_cannot_ask_is_still_relayed_as_one_the_thread_needs_answered(self):
        seam = F.FakeSeam(read=F.state(as_of=120)).answer("launch", F.launched(sequence=118))
        h = Harness(self, seam)
        h.follow.launch(h.unstarted(), 0)
        seam.push(F.needs_you(130, prompt="Which one?", kind=T.PendingKind.QUESTION, source=T.NeedSource.USER_INPUT))
        h.tick(1)
        self.assertEqual((h.acts()[-1].kind, h.acts()[-1].slots["need"]), (S.NEEDS_YOU, "question"))

    def answering(self, seam: F.FakeSeam | None = None):
        seam = seam or F.FakeSeam()
        h = Harness(self, seam)
        task = h.unstarted(Kind.ANSWER, thread="t1")
        task.title = "Fix the flaky test"
        h.journal.ensure_answer(task, "q1", [{"questionId": "db", "kind": "option", "text": "Postgres"}],
                                ["Postgres"], "question")
        return h, seam, task

    def test_an_answer_goes_to_the_named_request_with_its_saved_request_id_and_the_line_says_what_was_confirmed(self):
        h, seam, task = self.answering(F.FakeSeam().answer("answer_question", F.answered("t1", "q1", (("db", "pg"),))))
        said = h.follow.answer(task, 0)
        self.assertEqual(seam.ops("answer_question"),
                         [("answer_question", task.link.answer["request_id"], "t1", "q1",
                           [{"questionId": "db", "kind": "option", "text": "Postgres"}])])
        self.assertEqual(said, Act(S.ANSWERED, {"thread": "Fix the flaky test", "answers": ["Postgres"]}))
        self.assertEqual((task.status, task.link.answer["answered"], task.link.answer["sequence"]),
                         (Status.COMPLETED, True, 97))

    def test_a_retry_after_the_answer_was_taken_reports_it_and_sends_nothing_more(self):
        h, seam, task = self.answering(F.FakeSeam().answer("answer_question", F.answered(sequence=97)))
        h.follow.answer(task, 0)
        h.follow.answer(task, 1)
        self.assertEqual(len(seam.ops("answer_question")), 1)

    def test_a_retry_after_a_failure_sends_the_same_request_id(self):
        h, seam, task = self.answering(F.FakeSeam().answer("answer_question", F.unavailable(), F.answered()))
        failed = h.follow.answer(task, 0)
        self.assertEqual((failed.kind, task.status), (S.UNREACHABLE, Status.FAILED))
        h.follow.answer(task, 1)
        ids = [call[1] for call in seam.ops("answer_question")]
        self.assertEqual(ids, [task.link.answer["request_id"]] * 2)

    def test_a_refusal_is_reported_as_t3s_and_nothing_is_said_given(self):
        h, seam, task = self.answering(F.FakeSeam().answer(
            "answer_question", F.refusal("invalid_request", "That request is not waiting any more.")))
        said = h.follow.answer(task, 0)
        self.assertEqual((said.kind, said.slots["op"], task.status), (S.REFUSED, "answer", Status.FAILED))
        self.assertFalse(task.link.answer["answered"])

    def test_answers_t3_recorded_for_other_questions_are_never_reported_given(self):
        h, seam, task = self.answering(F.FakeSeam().answer("answer_question", F.answered(answers=(("other", "x"),))))
        said = h.follow.answer(task, 0)
        self.assertEqual((said.kind, task.link.answer["answered"]), (S.UNREACHABLE, False))

    def test_a_restart_never_answers_for_a_confirmation_nobody_is_there_for(self):
        h, seam, task = self.answering(F.FakeSeam().answer("answer_question", F.unavailable()))
        h.follow.answer(task, 0)
        task.status = Status.UNSTARTED
        h.follow.resume(1)
        self.assertEqual((task.status, len(seam.ops("answer_question"))), (Status.FAILED, 1))


class SchemaCoverage(unittest.TestCase):
    def test_every_watch_ended_reason_has_a_row(self):
        for reason in SCHEMA["WatchEndReason"]["enum"]:
            self.assertIn(L.WATCH_ENDED_ROWS[T.WatchEndReason(reason)], L.ROWS)

    def test_every_refusal_code_has_a_row_per_call(self):
        for table in (L.LAUNCH_ROWS, L.SEND_ROWS, L.WATCH_CALL_ROWS, L.READ_ROWS):
            for code in SCHEMA["RefusalCode"]["enum"]:
                self.assertIn(table[T.RefusalCode(code)], L.ROWS)

    def test_every_socket_end_has_a_row(self):
        ends = [T.CLOSE_1013, T.CLOSE_1008, T.CLOSE_1007, T.CLOSE_1011, T.CloseOther(4000),
                T.UPGRADE_401, T.UPGRADE_403, T.UPGRADE_404, T.UNREACHABLE]
        for end in ends:
            self.assertIn(L.SOCKET_END_ROWS[end.label], L.ROWS)

    def test_every_row_has_an_action(self):
        self.assertEqual(set(L.ACTIONS), set(L.Do))
        for row in L.ROWS.values():
            self.assertIn(row.do, L.ACTIONS)


if __name__ == "__main__":
    unittest.main()
