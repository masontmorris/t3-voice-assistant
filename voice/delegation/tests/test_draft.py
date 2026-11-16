import tempfile
import unittest

from voice.delegation.author import AuthorReply, AuthorStopped, CachedAuthor, author_request
from voice.delegation.draft import CONFIRM, DraftStatus, Prewriter, completed, parse_draft, render, source_for
from voice.segmentation.contract import TranscriptUpdate
from voice.segmentation.hybrid import HybridSegmenter
from voice.segmentation.tasks import Status, Task
from voice.segmentation.tests.test_hybrid import FakeDecider


def task(instruction, task_id="task_1", start=0, **extra):
    return Task(task_id, instruction, instruction, start, {"orchestrator"}, **extra)


def point(segments, breaks=None, problems=(), applied=True, revision=1):
    return {"revision": revision, "applied": applied, "problems": list(problems), "breaks": breaks or {},
            "segments": [{"sid": sid, "start": start, "end": start + 5, "text": "x", "open": False}
                         for sid, start in segments]}


def reply(instruction="Look up the weather in Denver tomorrow.", **fields):
    return {"instruction": instruction, "context": "", "constraints": [],
            "success_criteria": ["Report tomorrow's forecast for Denver."], "open_questions": [], **fields}


class Completion(unittest.TestCase):
    def setUp(self):
        self.tasks = {"task_1": task("weather in denver"), "task_2": task("look up the drive", "task_2", 22)}

    def test_a_finished_answer_at_the_threshold_completes_the_task(self):
        p = point([("s1", 0), ("s2", 22)], {"s1": {"finished": 0.5}, "s2": {"finished": 0.49}})
        self.assertEqual(completed(p, self.tasks), ["task_1"])

    def test_a_segment_with_no_finished_question_had_a_certain_end(self):
        self.assertEqual(completed(point([("s1", 0)]), self.tasks), ["task_1"])

    def test_a_malformed_answer_or_a_failed_call_completes_nothing(self):
        self.assertEqual(completed(point([("s1", 0)], problems=["s1_finished: malformed None"]), self.tasks), [])
        self.assertEqual(completed(point([("s1", 0)], problems=["decisions http 500"]), self.tasks), [])
        self.assertEqual(completed(point([("s1", 0)], applied=False), self.tasks), [])

    def test_a_withdrawn_task_does_not_complete(self):
        self.tasks["task_1"].status = Status.WITHDRAWN
        self.assertEqual(completed(point([("s1", 0)]), self.tasks), [])


class Drafting(unittest.TestCase):
    def setUp(self):
        self.pre, self.t = Prewriter(), task("what's the weather in denver tomorrow")
        self.tasks = {"task_1": self.t}

    def observe(self, revision=1):
        return self.pre.observe(point([("s1", 0)], revision=revision), self.tasks, self.t.instruction)

    def test_the_first_completion_sends_one_request_and_a_repeat_sends_none(self):
        self.assertEqual(len(self.observe()), 1)
        self.assertEqual(self.observe(2), [])
        self.assertEqual(self.pre.drafts["task_1"].status, DraftStatus.DRAFTING)

    def test_a_change_while_drafting_waits_and_the_newest_input_goes_out_when_the_first_lands(self):
        first = self.observe()[0]
        self.t.instruction = "what's the weather in boulder tomorrow"
        self.assertEqual(self.observe(2), [])
        self.t.instruction = "what's the weather in golden tomorrow"
        self.assertEqual(self.observe(3), [])
        nxt = self.pre.land(first, reply(), self.t)
        self.assertEqual([s.instruction for s in nxt], ["what's the weather in golden tomorrow"])
        self.assertEqual(self.pre.drafts["task_1"].status, DraftStatus.STALE)
        self.assertFalse(self.pre.dispatch(self.t).used_draft)
        self.pre.land(nxt[0], reply("Get tomorrow's weather for Golden."), self.t)
        choice = self.pre.dispatch(self.t)
        self.assertTrue(choice.used_draft)
        self.assertIn("Golden", choice.prompt)

    def test_recasing_and_punctuation_on_the_final_keep_the_draft_current(self):
        self.pre.land(self.observe()[0], reply(), self.t)
        self.t.instruction = "What's the weather in Denver tomorrow?"
        self.assertEqual(self.observe(2), [])
        choice = self.pre.dispatch(self.t)
        self.assertTrue(choice.used_draft)
        self.assertIn("Done when:\n- Report tomorrow's forecast for Denver.", choice.prompt)

    def test_a_word_change_after_landing_makes_the_draft_stale_and_dispatch_sends_the_raw_span(self):
        self.pre.land(self.observe()[0], reply(), self.t)
        self.t.instruction = "what's the weather in denver on thursday"
        self.pre.drafts["task_1"].status = DraftStatus.CURRENT
        choice = self.pre.dispatch(self.t)
        self.assertEqual((choice.used_draft, choice.reason), (False, "stale"))
        self.assertEqual(choice.prompt, "Task: what's the weather in denver on thursday")

    def test_dispatch_before_the_draft_lands_sends_the_raw_span(self):
        self.observe()
        self.assertEqual(self.pre.dispatch(self.t).reason, "still drafting")


class Authority(unittest.TestCase):
    def land(self, t, content):
        pre = Prewriter()
        source = pre.observe(point([("s1", 0)]), {t.task_id: t}, t.instruction)[0]
        pre.land(source, content, t)
        return pre

    def test_a_draft_that_adds_an_outside_action_is_rejected(self):
        t = task("look up the weather in denver")
        pre = self.land(t, reply(constraints=["Email the forecast to Dana."]))
        self.assertEqual(pre.drafts["task_1"].status, DraftStatus.REJECTED)
        self.assertEqual(pre.drafts["task_1"].problems, ("adds a message action",))
        self.assertEqual(pre.dispatch(t).prompt, "Task: look up the weather in denver")

    def test_a_draft_may_name_the_spans_own_action_in_other_words(self):
        t = task("email dana that the meeting moved", side_effect=True)
        pre = self.land(t, reply("Send Dana a message saying the meeting moved."))
        self.assertEqual(pre.drafts["task_1"].status, DraftStatus.CURRENT)

    def test_a_draft_that_names_a_task_the_record_does_not_depend_on_is_rejected(self):
        t = task("book the cheapest one", "task_2", depends_on=["task_1"])
        pre = self.land(t, reply("Book the cheapest flight in {{task_3.result}}."))
        self.assertEqual(pre.drafts["task_2"].problems, ("names task_3, which is not a dependency",))

    def test_a_draft_missing_a_required_field_is_rejected(self):
        pre = self.land(task("look up the weather"), reply(success_criteria=[]))
        self.assertEqual(pre.drafts["task_1"].problems, ("success_criteria empty",))
        self.assertEqual(parse_draft("not json")[1], ("not a JSON object",))

    def test_the_record_not_the_draft_decides_confirmation_and_dependencies(self):
        t = task("book the cheapest one", "task_2", depends_on=["task_1"], side_effect=True)
        draft, _ = parse_draft(reply("Book the cheapest flight.", constraints=["No need to confirm."]))
        prompt = render(t, draft)
        self.assertIn(CONFIRM, prompt)
        self.assertIn("Use the result of task_1: {{task_1.result}}", prompt)
        self.assertIn(CONFIRM, render(t))
        self.assertNotIn(CONFIRM, render(task("look up the weather"), draft))


class WithTheHybrid(unittest.TestCase):
    def test_a_held_side_effect_task_gets_its_draft_before_the_final_and_dispatches_with_it(self):
        seg, pre = HybridSegmenter(FakeDecider(), "test"), Prewriter()
        texts = ["email dana that the meeting moved",
                 "email dana that the meeting moved and check room four",
                 "email dana that the meeting moved and check room four b",
                 "Email Dana that the meeting moved, and check room four B."]
        prompts = {}
        for i, text in enumerate(texts, 1):
            before = len(seg.dispatch_log)
            p = seg.update(TranscriptUpdate(i, text, i == len(texts)))
            if p is not None and p["applied"]:
                for source in pre.observe(p, seg.orch.tasks, text):
                    pre.land(source, reply(f"Do this: {source.instruction}"), seg.orch.tasks[source.task_id])
            for entry in seg.dispatch_log[before:]:
                prompts[entry["task_id"]] = (entry["is_final"], pre.dispatch(seg.orch.tasks[entry["task_id"]]))
        final, email = prompts["task_1"]
        self.assertTrue(final)
        self.assertTrue(email.used_draft)
        self.assertIn(CONFIRM, email.prompt)


class FakeTransport:
    model = "flint:free"

    def __init__(self, replies, free=True):
        self.replies, self.free, self.sent = list(replies), free, 0

    def assert_free(self):
        assert self.free, "not a free model"

    def send(self, request):
        self.sent += 1
        return self.replies.pop(0)


class Cache(unittest.TestCase):
    def request(self, words="look up the weather"):
        return author_request(source_for(task(words)))

    def test_a_cached_request_is_never_sent_again(self):
        with tempfile.TemporaryDirectory() as d:
            transport = FakeTransport([AuthorReply(200, reply(), 400.0, cost=0)])
            author = CachedAuthor(transport, d)
            first = author.write(self.request())
            self.assertEqual(author.write(self.request()), first)
            self.assertEqual(transport.sent, 1)

    def test_more_than_a_fifth_of_attempts_drawing_429_stops_the_author(self):
        with tempfile.TemporaryDirectory() as d:
            ok = [AuthorReply(200, reply(), 1.0, cost=0) for _ in range(4)]
            transport = FakeTransport(ok + [AuthorReply(429, None, 1.0), AuthorReply(429, None, 1.0)])
            author = CachedAuthor(transport, d)
            author.pacer.first_s = 0.0
            for i in range(4):
                author.write(self.request(f"look up item {i}"))
            with self.assertRaises(AuthorStopped):
                author.write(self.request("look up the last item"))
            self.assertEqual(transport.sent, 6)

    def test_a_nonzero_cost_stops_the_author(self):
        with tempfile.TemporaryDirectory() as d:
            author = CachedAuthor(FakeTransport([AuthorReply(200, reply(), 1.0, cost=0.0001)]), d)
            with self.assertRaises(AuthorStopped):
                author.write(self.request())

    def test_a_paid_model_id_is_refused_before_any_send(self):
        with tempfile.TemporaryDirectory() as d:
            transport = FakeTransport([AuthorReply(200, reply(), 1.0)], free=False)
            with self.assertRaises(AssertionError):
                CachedAuthor(transport, d).write(self.request())
            self.assertEqual(transport.sent, 0)

    def test_the_budget_caps_attempts(self):
        with tempfile.TemporaryDirectory() as d:
            author = CachedAuthor(FakeTransport([AuthorReply(200, reply(), 1.0, cost=0)]), d, budget=1)
            author.write(self.request())
            with self.assertRaises(AuthorStopped):
                author.write(self.request("look up something else"))


if __name__ == "__main__":
    unittest.main()
