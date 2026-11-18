"""The voice's text model: one request in flight, summaries cached per thread and read sequence, and lines that
pass the slot check or are not returned."""

from __future__ import annotations

import json
import threading
import unittest

from voice.t3 import fakes as F
from voice.t3 import types as T
from voice.t3.acts import Kind, act
from voice.t3.speech import RENDER_SYSTEM, SUMMARY_SYSTEM, Lane, Speech, note

REPLY = T.LastReply("I added clamp next to sum. All 12 tests pass.", "m1", None, "r1", "2026-11-18T10:00:00Z")


class Text:
    """A free text model that answers summaries with a gist and renders with `line`, counting each kind."""

    model = "fake/text:free"

    def __init__(self, line="Add a clamp helper finished, and all 12 tests pass.", gate=None):
        self.line, self.gate = line, gate
        self.summaries, self.renders, self.bodies = 0, 0, []
        self.started = threading.Event()

    def complete(self, messages):
        self.started.set()
        if self.gate is not None:
            self.gate.wait()
        self.bodies.append(json.loads(messages[1]["content"]))
        if messages[0]["content"] == SUMMARY_SYSTEM:
            self.summaries += 1
            return {"text": "It added clamp next to sum, and all 12 tests pass."}
        assert messages[0]["content"] == RENDER_SYSTEM
        self.renders += 1
        return {"text": self.line}


class Reads:
    def __init__(self, *states):
        self.states, self.count = list(states), 0

    def __call__(self, thread_id):
        self.count += 1
        return self.states[min(self.count, len(self.states)) - 1]


def finished(as_of):
    return F.state(thread="t5", title="Add a clamp helper", as_of=as_of, activity=T.ThreadActivity.FINISHED,
                   last_reply=REPLY)


class LaneTest(unittest.TestCase):
    def test_only_free_models_and_one_request_at_a_time(self):
        with self.assertRaises(ValueError):
            Lane(type("Paid", (), {"model": "particle-ai/particle-1"})())
        gate = threading.Event()
        text = Text(gate=gate)
        lane = Lane(text)
        first = lane.submit([{"content": SUMMARY_SYSTEM}, {"content": "{}"}])
        second = lane.submit([{"content": SUMMARY_SYSTEM}, {"content": "{}"}])
        self.assertTrue(text.started.wait(2))
        self.assertFalse(second.running(), "the second request waits for the slot")
        gate.set()
        for future in (first, second):
            future.result(timeout=2)
        self.assertEqual((text.summaries, lane.calls), (2, 2))

    def test_a_request_past_its_timeout_fails_and_a_queued_one_is_dropped(self):
        gate = threading.Event()
        text = Text(gate=gate)
        lane = Lane(text)
        running = lane.submit([{"content": SUMMARY_SYSTEM}, {"content": "{}"}])
        self.assertTrue(text.started.wait(2))
        with self.assertRaises(TimeoutError):
            lane.call([{"content": SUMMARY_SYSTEM}, {"content": "{}"}], timeout=0.01)
        gate.set()
        running.result(timeout=2)
        lane.pool.shutdown(wait=True)
        self.assertEqual(text.summaries, 1, "the timed-out request never reached the model")


class SummaryTest(unittest.TestCase):
    def test_the_summary_is_cached_per_thread_and_sequence(self):
        text, reads = Text(), Reads(finished(140), finished(140), finished(141))
        speech = Speech(Lane(text), reads)
        done = (act(Kind.DONE, status="completed"),)
        first, evidence = speech.fill(done, "t5")
        self.assertEqual(first[0].slots, {"status": "completed", "thread": "Add a clamp helper",
                                          "summary": "It added clamp next to sum, and all 12 tests pass."})
        self.assertEqual(evidence, (140,))
        speech.fill(done, "t5")
        self.assertEqual(text.summaries, 1, "the same read sequence reuses its summary")
        speech.fill(done, "t5")
        self.assertEqual(text.summaries, 2, "a new sequence is summarised again")

    def test_a_read_answer_uses_the_read_it_came_from_and_its_question(self):
        text, reads = Text(), Reads()
        speech = Speech(Lane(text), reads)
        speech.remember(finished(140))
        asked = (act(Kind.THREAD_STATUS, thread="Add a clamp helper", status="finished",
                     asked="Did the tests pass?"),)
        filled, evidence = speech.fill(asked, "t5", (140,))
        self.assertEqual((reads.count, evidence), (0, ()), "no second read")
        self.assertEqual(text.bodies[0]["question"], "Did the tests pass?")
        self.assertEqual(text.bodies[0]["records"]["last_reply"], REPLY.text, "the full last reply, never a cut")
        self.assertIn("summary", filled[0].slots)

    def test_a_speculative_start_is_the_summary_the_answer_uses(self):
        gate = threading.Event()
        text, reads = Text(gate=gate), Reads(finished(140))
        speech = Speech(Lane(text), reads)
        speech.prefetch("t5", "How's the clamp thread going?")
        self.assertTrue(text.started.wait(2), "the summary started before anyone asked for it")
        gate.set()
        asked = (act(Kind.THREAD_STATUS, thread="Add a clamp helper", status="finished"),)
        filled, _ = speech.fill(asked, "t5", (140,))
        self.assertEqual((text.summaries, reads.count), (1, 1))
        self.assertIn("summary", filled[0].slots)

    def test_no_reply_or_no_read_means_no_summary(self):
        text = Text()
        empty = F.state(thread="t5", title="Add a clamp helper", as_of=9)
        speech = Speech(Lane(text), Reads(empty))
        filled, _ = speech.fill((act(Kind.NEEDS_YOU, prompt="Run it?"),), "t5")
        self.assertEqual((filled[0].slots, text.summaries), ({"prompt": "Run it?", "thread": "Add a clamp helper"}, 0))
        unread = Speech(Lane(text), lambda thread_id: None)
        self.assertEqual(unread.fill((act(Kind.DONE, status="failed"),), "t5"),
                         ((act(Kind.DONE, status="failed"),), ()))


class RenderTest(unittest.TestCase):
    def test_a_line_that_fails_the_check_is_not_returned(self):
        acts = (act(Kind.CONFIRM_STOP, thread="Add a clamp helper"),)
        good = Speech(Lane(Text("Should I stop Add a clamp helper?")), Reads()).render(acts)
        self.assertEqual((good.line, good.problems), ("Should I stop Add a clamp helper?", []))
        bad = Speech(Lane(Text("I've stopped Add a clamp helper.")), Reads()).render(acts)
        self.assertIsNone(bad.line)
        self.assertEqual(bad.problems, ["not a question", "claims stopped"])
        self.assertIsNone(Speech(None, Reads()).render(acts).line, "no model, no line")

    def test_the_prompt_carries_the_facts_the_turns_and_the_last_refusal(self):
        text = Text("Should I stop Add a clamp helper?")
        acts = (act(Kind.CONFIRM_STOP, thread="Add a clamp helper"),)
        Speech(Lane(text), Reads()).render(acts, [{"user": "Stop the clamp thread."}], ["not a question"])
        body = text.bodies[0]
        self.assertEqual((body["keep_exact"], body["ask"], body["turns"]),
                         (["Add a clamp helper"], True, [{"person": "Stop the clamp thread."}]))
        self.assertIn("that the thread stopped or was cancelled", body["never_say"])
        self.assertEqual(body["an earlier wording was refused because"], ["not a question"])

    def test_the_lfm3_note_names_the_exact_words_and_the_question(self):
        text = note((act(Kind.CONFIRM_TELL, thread="Add a clamp helper", message="also add a test"),))
        self.assertIn('"Add a clamp helper", "also add a test"', text)
        self.assertIn("End with a question.", text)
        self.assertIn("Do not say that the message was sent or passed on.", text)


if __name__ == "__main__":
    unittest.main()
