"""One case per row of "The watch lifecycle" (docs/voice/seam-design.md), against a fake seam and a fake clock.

Each test is named for its row and asserts that row's proof. The schema test fails when the seam schema
holds a watch_ended reason or a refusal code that lifecycle.py does not route.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from voice.t3 import fakes as F
from voice.t3 import lifecycle as L
from voice.t3 import types as T
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
        new = self.follow.take()
        self.heard.extend(new)
        return [u.line for u in new]

    def said(self) -> list[str]:
        self.lines()
        return [u.line for u in self.heard]


class WatchLifecycleRows(unittest.TestCase):
    def test_launch_fresh(self):
        h = Harness(self)
        task = h.unstarted()
        h.follow.launch(task, 0)
        self.assertEqual(h.seam.ops("watch"), [("watch", "t1", 93, "r1")])
        self.assertEqual(task.status, Status.LIVE)
        self.assertEqual(h.follow.outbox[0].sequences, (93,))
        self.assertEqual(h.said(), ["Started a thread in Voice sandbox."])

    def test_launch_replayed(self):
        seam = F.FakeSeam(read=F.state(as_of=120, pending=F.need()))
        seam.answer("launch", F.launched(sequence=118, replayed=True))
        h = Harness(self, seam)
        task = h.unstarted()
        h.follow.launch(task, 0)
        self.assertEqual(seam.ops("read"), [("read", "t1")])
        self.assertEqual(seam.ops("watch"), [("watch", "t1", 120, "r1")])
        self.assertEqual(h.said(), ["Started a thread in Voice sandbox.", "It needs you: Run the tests?"])
        seam.push(F.needs_you(130), F.ended("resync"))
        h.tick(1)
        self.assertEqual(h.said().count("It needs you: Run the tests?"), 1)
        self.assertEqual(seam.ops("watch")[-1], ("watch", "t1", 120, "r1"))

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
        self.assertEqual(lines[T.RefusalCode.GONE], ["Server says gone."])
        refused = F.FakeSeam().answer("launch", F.refusal("refused", "T3 refused it.", reason="mode too high"))
        bare = F.FakeSeam().answer("launch", F.refusal("refused", "T3 refused it."))
        for seam, line in ((refused, "T3 refused to start it: mode too high"), (bare, "T3 refused to start it.")):
            h = Harness(self, seam)
            task = h.unstarted()
            h.follow.launch(task, 0)
            self.assertEqual(h.said(), [line])
            self.assertNotIn("deleted", line)

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
        self.assertEqual(h.said(), ["I've asked T3 to start it.",
                                    "I asked T3 to start it, but I couldn't confirm that it started. Check the app."])
        self.assertEqual(task.status, Status.LOST)

    def test_tell(self):
        seam = F.FakeSeam(read=F.state(as_of=200, pending=F.need("q7", "Which branch?")))
        seam.answer("send", F.launched(sequence=190, replayed=False))
        h = Harness(self, seam)
        task = h.unstarted(Kind.TELL, thread="t1")
        h.follow.send(task, 0)
        self.assertEqual(seam.ops("watch"), [("watch", "t1", 200, "r1")])
        self.assertEqual(h.said(), ["I've passed that on.", "It needs you: Which branch?"])

    def test_tell_refused(self):
        for code in (c for c in NON_UNAVAILABLE if c is not T.RefusalCode.THREAD_BUSY):
            with self.subTest(code=code.value):
                seam = F.FakeSeam().answer("send", F.refusal(code.value, "Not that one."))
                h = Harness(self, seam)
                task = h.unstarted(Kind.TELL, thread="t1")
                h.follow.send(task, 0)
                self.assertEqual(task.status, Status.FAILED)
                expected = "T3 refused to pass that on." if code is T.RefusalCode.REFUSED else "Not that one."
                self.assertEqual(h.said(), [expected])

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
        self.assertEqual(h.said(), [L.ROWS["tell-unavailable"].line])

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
        self.assertEqual(h.said(), [L.HELD_LINE, "It needs you: Run the tests?"])
        self.assertEqual(len(seam.ops("send")), 1)

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
        self.assertEqual(h.said(), [L.HELD_LINE, L.HELD_LINE, "It finished. It said: I added clamp. The tests pass.",
                                    "I've passed that on.", "It finished. It said: Added the test.",
                                    "I've passed that on."])

    def test_held_dropped(self):
        h = Harness(self, F.FakeSeam(read=F.state(as_of=150)))
        task = self.held(h)
        h.follow.drop(task, 1)
        self.assertEqual(task.status, Status.CANCELLED)
        self.assertEqual(h.seam.ops("unwatch"), [("unwatch", "t1")])
        h.seam.push(F.done(160))
        h.tick(2)
        self.assertEqual(len(h.seam.ops("send")), 1)
        self.assertEqual(h.said(), [L.HELD_LINE, L.DROPPED_LINE])

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

    def test_ask(self):
        from voice.live.t3_tasks import T3Tasks

        seam = F.FakeSeam(read=F.state(thread="t5", title="Fix the flaky test", activity=T.ThreadActivity.WORKING))
        seam.listing = T.ThreadList((T.ThreadSummary("t5", "p1", "Fix the flaky test", T.ThreadActivity.WORKING,
                                                     "2026-11-17T10:00:00Z", False),), 1, False, 77)
        h = Harness(self, seam)
        tasks = T3Tasks(h.follow)
        tasks.about("b1", "t3_ask", "How is the flaky test thread going?", 0)
        answer = tasks.answer("yes", 1)
        self.assertEqual(seam.ops("watch"), [])
        self.assertEqual(answer.line, "Fix the flaky test is still working.")
        self.assertEqual(answer.sequences, (120,))
        broken = Harness(self, F.FakeSeam().answer("list", F.unavailable()))
        failed = T3Tasks(broken.follow).about("b2", "t3_ask", "How is the flaky test thread going?", 0)
        self.assertEqual((failed.status, failed.line), ("failed", L.UNREACHABLE_LINE))

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
            "cursor event": ([F.needs_you(11)], 0, "It needs you: Run the tests?"),
            "done": ([F.done(12)], 0, "It finished. It said: I added clamp. The tests pass."),
            "give-up line": ([F.ended("not_found")], 0, L.L2),
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
                spoken = [u.line for u in h.follow.outbox]
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
            F.needs_you(11), "It needs you: Run the tests?", (11,), watch_after=11,
        )

    def test_done_waiting_for_a_turn_survives_restart(self):
        self.assert_pending_line_survives_restart(
            F.done(12), "It finished. It said: I added clamp. The tests pass.", (12, 13), watch_after=None,
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
        self.assertEqual([(u.line, u.sequences) for u in h.follow.take()], [(line, evidence)])
        self.assertEqual(h.follow.take(), [])

        h.restart(F.FakeSeam())
        h.follow.resume(3)
        self.assertEqual(h.follow.take(), [])

    def test_slot_wait(self):
        h = Harness(self, F.FakeSeam(max_watches=16))
        tasks = [h.live(cursor=10 + i, thread=f"t{i}") for i in range(17)]
        h.start()
        self.assertEqual(len(h.seam.ops("watch")), 16)
        self.assertEqual(h.said(), [L.SLOT_LINE.format(n=16)])
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
                self.assertEqual(h.said(), [L.L1])
                self.assertEqual(task.status, Status.LOST)

    def test_watch_refused(self):
        codes = [c for c in NON_UNAVAILABLE if c is not T.RefusalCode.TOO_MANY_WATCHES]
        for code in codes:
            with self.subTest(code=code.value):
                h = Harness(self, F.FakeSeam().answer("watch", F.refusal(code.value)))
                task = h.live()
                h.start()
                expected = L.L2 if code.value in ("project_not_allowed", "not_found", "gone") else L.L1
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
        self.assertEqual(h.said(), ["It's working on it.", L.L1])
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
        self.assertEqual((task.status, h.said()), (Status.LOST, [L.L2]))
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
                self.assertEqual((task.status, h.said()), (Status.LOST, [L.L2]))

    def test_gone_event(self):
        h = Harness(self)
        task = h.live(cursor=10)
        h.start()
        h.seam.push(F.gone(14), F.ended("gone"))
        h.tick(0)
        h.tick(5)
        self.assertEqual((task.status, h.said()), (Status.FAILED, [L.DELETED_LINE]))
        self.assertEqual(len(h.seam.ops("watch")), 1)

    def test_done_event(self):
        h = Harness(self)
        task = h.live(cursor=10)
        h.start()
        h.seam.push(F.done(20))
        h.tick(0)
        h.seam.push(F.needs_you(22, "q2", "Merge it?"))
        h.tick(4)
        self.assertEqual(h.said(), ["It finished. It said: I added clamp. The tests pass.",
                                    "It also needs you: Merge it?"])
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
        self.assertEqual(h.said(), [L.ROWS["close-other"].line])
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
                self.assertEqual(h.said(), [L.ROWS["upgrade-401-or-403"].line])
        h = Harness(self, T.UPGRADE_403)
        h.live()
        h.start()
        h.tick(30)
        self.assertEqual((h.connector.count, h.said()), (1, [L.ROWS["upgrade-401-or-403"].line]))

    def test_upgrade_404(self):
        h = Harness(self, T.UPGRADE_404)
        task = h.live()
        h.start()
        h.tick(1)
        h.tick(100)
        self.assertEqual(h.connector.count, 1)
        self.assertEqual((task.status, h.said()), (Status.LOST, [L.ROWS["upgrade-404"].line]))

    def test_every_row_has_a_case(self):
        cases = {name[len("test_"):] for name in dir(self) if name.startswith("test_")}
        missing = [row for row in L.ROWS if row.replace("-", "_") not in cases]
        self.assertEqual(missing, [])
        self.assertEqual(len(L.ROWS), 31)


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
        self.assertEqual(set(L.ACTIONS), set(L.Act))
        for row in L.ROWS.values():
            self.assertIn(row.act, L.ACTIONS)


if __name__ == "__main__":
    unittest.main()
