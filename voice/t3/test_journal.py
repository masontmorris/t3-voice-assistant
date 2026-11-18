"""The journal and the cursor rule: request ids, restarts, done, and the dedupe that survives them."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from voice.t3 import fakes as F
from voice.t3 import lifecycle as L
from voice.t3 import types as T
from voice.t3.acts import Act
from voice.t3.acts import Kind as S
from voice.t3.journal import Kind, Status, T3Journal, ulid


class JournalCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "journal.json"
        self.seam = F.FakeSeam()

    def follow(self) -> L.Follow:
        return L.Follow(T3Journal(self.path), F.Connector(self.seam))

    def live(self, follow: L.Follow, cursor: int = 10):
        task = follow.journal.begin(Kind.START, "Fix the flaky test", "p1", "Voice sandbox")
        task.status, task.announced = Status.LIVE, True
        task.link.thread_id, task.link.run_id, task.link.cursor = "t1", "r1", cursor
        follow.journal.save()
        follow.ensure(0)
        return task

    def lines(self, follow: L.Follow) -> list[str]:
        """Each update as its act kinds, with a status slot after a colon."""
        return ["+".join(a.kind.value + (f":{a.slots['status']}" if "status" in a.slots else "") for a in u.acts)
                for u in follow.take()]


class RequestIds(JournalCase):
    def test_two_turns_never_share_a_request_id(self):
        same_ms = [ulid(now_ms=1_700_000_000_000) for _ in range(2000)]
        self.assertEqual(len(set(same_ms)), 2000)
        self.assertEqual(same_ms, sorted(same_ms))
        self.assertTrue(all(len(i) == 26 for i in same_ms))
        journal = T3Journal(self.path)
        first = journal.begin(Kind.START, "Fix the flaky test", "p1")
        second = journal.begin(Kind.START, "Fix the flaky test", "p1")
        self.assertNotEqual(first.link.request_id, second.link.request_id)
        self.assertNotEqual(first.task_id, second.task_id)

    def test_the_request_id_is_saved_before_the_call(self):
        seen = {}

        class Peek(F.FakeSeam):
            def launch(inner, request_id, project_id, instruction):
                seen["saved"] = {t.link.request_id for t in T3Journal(self.path).tasks.values()}
                return super().launch(request_id, project_id, instruction)

        self.seam = Peek()
        follow = self.follow()
        task = follow.journal.begin(Kind.START, "Fix the flaky test", "p1")
        follow.launch(task, 0)
        self.assertEqual(seen["saved"], {task.link.request_id})


class CursorRule(JournalCase):
    def test_done_comes_only_from_a_done_event(self):
        follow = self.follow()
        task = self.live(follow)
        self.seam.push(F.run_event(11, T.RunStatus.COMPLETED), F.reply(12), F.run_event(13, T.RunStatus.WAITING))
        follow.tick(0)
        self.assertEqual(task.status, Status.LIVE)
        self.seam.push(F.done(14))
        follow.tick(1)
        self.assertEqual(task.status, Status.COMPLETED)

    def test_a_done_never_moves_the_cursor(self):
        follow = self.follow()
        task = self.live(follow, cursor=10)
        self.seam.push(F.done(50, as_of=60))
        follow.tick(0)
        self.assertEqual(task.link.cursor, 10)
        self.assertEqual(T3Journal(self.path).tasks[task.task_id].link.cursor, 10)
        self.seam.push(F.needs_you(20, "q2", "Merge it?"))
        follow.tick(1)
        self.assertEqual(self.lines(follow)[-1], "needs_you")
        self.assertEqual(task.link.cursor, 20)

    def test_events_at_or_below_the_cursor_are_dropped(self):
        follow = self.follow()
        task = self.live(follow, cursor=30)
        self.seam.push(F.needs_you(29), F.reply(30), F.run_event(31))
        follow.tick(0)
        self.assertEqual(self.lines(follow), ["run_update:running"])
        self.assertEqual(task.link.cursor, 31)

    def test_a_done_after_its_reply_carries_only_its_status(self):
        follow = self.follow()
        self.live(follow)
        self.seam.push(F.reply(12, message="m9", text="Done. I added clamp."), F.done(13, message="m9"))
        follow.tick(0)
        self.assertEqual(self.lines(follow), ["replied", "done:completed"])

    def test_a_done_with_empty_text_carries_only_its_status(self):
        follow = self.follow()
        self.live(follow)
        self.seam.push(T.DoneEvent("t1", "r1", T.TerminalStatus.FAILED, T.RunResult("", None, None), 12, 13))
        follow.tick(0)
        self.assertEqual(self.lines(follow), ["done:failed"])

    def test_an_applied_request_id_survives_a_restart(self):
        follow = self.follow()
        task = self.live(follow)
        self.seam.push(F.needs_you(11, "q1", "Run the tests?"))
        follow.tick(0)
        self.assertEqual(self.lines(follow), ["needs_you"])
        restarted = self.follow()
        restarted.resume(1)
        self.seam.push(F.needs_you(15, "q1", "Run the tests?"))
        restarted.tick(2)
        self.assertEqual(self.lines(restarted), [])
        self.assertEqual(restarted.journal.tasks[task.task_id].link.requests, {"q1": "needs_you"})

    def test_a_resolved_never_announced_is_saved_but_not_spoken(self):
        follow = self.follow()
        task = self.live(follow)
        self.seam.push(F.resolved(11, "q9"))
        follow.tick(0)
        self.assertEqual(self.lines(follow), [])
        self.assertEqual(T3Journal(self.path).tasks[task.task_id].link.requests, {"q9": "resolved"})
        self.seam.push(F.needs_you(12, "q3"), F.resolved(13, "q3"))
        follow.tick(1)
        self.assertEqual(self.lines(follow), ["needs_you", "need_resolved:resolved"])

    def test_a_repeated_run_status_pair_is_spoken_once(self):
        follow = self.follow()
        self.live(follow)
        self.seam.push(F.run_event(11), F.run_event(12), F.run_event(13, run="r2"))
        follow.tick(0)
        self.assertEqual(self.lines(follow), ["run_update:running", "run_update:running"])

    def test_a_duplicate_final_reply_is_spoken_once(self):
        follow = self.follow()
        self.live(follow)
        self.seam.push(F.reply(11), F.reply(12))
        follow.tick(0)
        self.assertEqual(len(self.lines(follow)), 1)


class Interrupt(JournalCase):
    def stop_task(self, follow: L.Follow):
        return follow.journal.begin(Kind.STOP, "Stop it", "p1", "Voice sandbox", thread_id="t1")

    def test_the_saved_interrupt_result_survives_a_retry(self):
        self.seam.answer("interrupt", F.requested(sequence=77), T.NothingRunning())
        follow = self.follow()
        task = self.stop_task(follow)
        self.assertEqual(follow.stop(task, 0), Act(S.STOPPING))
        self.assertEqual(follow.stop(task, 1), Act(S.STOPPING))
        restarted = self.follow()
        self.assertEqual(restarted.stop(restarted.journal.tasks[task.task_id], 2), Act(S.STOPPING))
        self.assertEqual(len(self.seam.ops("interrupt")), 1)
        self.assertEqual(restarted.journal.tasks[task.task_id].link.stop["sequence"], 77)
        self.assertEqual(restarted.journal.tasks[task.task_id].status, Status.COMPLETED)

    def test_a_stop_interrupted_before_its_answer_is_reissued_on_restart(self):
        follow = self.follow()
        task = self.stop_task(follow)
        follow.journal.ensure_stop(task)
        self.seam.answer("interrupt", T.NothingRunning())
        restarted = self.follow()
        restarted.resume(0)
        self.assertEqual([c[1] for c in self.seam.ops("interrupt")], [task.link.stop["request_id"]])
        self.assertEqual(self.lines(restarted), ["nothing_running"])
        self.assertEqual(self.seam.ops("send"), [])

    def test_an_unavailable_interrupt_retries_with_the_same_request_id(self):
        follow = self.follow()
        task = self.stop_task(follow)
        self.assertEqual(follow.stop(task, 0).kind, S.STOP_UNCONFIRMED)
        restarted = self.follow()
        self.assertEqual(restarted.stop(restarted.journal.tasks[task.task_id], 1).kind, S.STOP_UNCONFIRMED)
        ids = [call[1] for call in self.seam.ops("interrupt")]
        self.assertEqual(len(ids), 2)
        self.assertEqual(len(set(ids)), 1)


class PendingActs(JournalCase):
    def test_pending_acts_round_trip_and_version_1_text_becomes_a_legacy_update(self):
        follow = self.follow()
        task = self.live(follow)
        follow.speak(task, Act(S.NEEDS_YOU, {"prompt": "Merge it?", "waiting_for": "approval"}), "needs_you", (21,))
        follow.journal.save()
        reread = T3Journal(self.path)
        self.assertEqual([(u.acts, u.sequences) for u in reread.pending_lines],
                         [((Act(S.NEEDS_YOU, {"prompt": "Merge it?", "waiting_for": "approval"}),), (21,))])
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        raw["pending_lines"] = [{"task_id": task.task_id, "line": "old text", "status": "live", "row": "reply",
                                 "thread_id": "t1", "sequences": [5], "delivery_failures": 0}]
        self.path.write_text(json.dumps(raw), encoding="utf-8")
        upgraded, = T3Journal(self.path).pending_lines
        self.assertEqual((upgraded.acts, upgraded.thread_id, upgraded.sequences, upgraded.row),
                         ((Act(S.LEGACY_UPDATE, {"text": "old text"}),), "t1", (5,), "reply"),
                         "an old line is news to word again, never dropped and never replayed as text")


if __name__ == "__main__":
    unittest.main()
