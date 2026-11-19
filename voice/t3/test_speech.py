"""The voice's text model: one request in flight, summaries cached per thread and read sequence, and lines that
pass the slot check or are not returned."""

from __future__ import annotations

import asyncio
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock, patch

from voice.prototype.realtime import Session
from voice.prototype.tests.test_run_shared_session import FakeSession
from voice.t3 import fakes as F
from voice.t3 import types as T
from voice.t3.acts import Kind, act
from voice.t3.realtime_writer import RealtimeWriter
from voice.t3.speech import RENDER_SYSTEM, SUMMARY_SYSTEM, Lane, LocalText, Speech, reader_messages, text_model

REPLY = T.LastReply("I added clamp next to sum. All 12 tests pass.", "m1", None, "r1", "2026-11-18T10:00:00Z")


class Text:
    """A free text model that answers summaries with a gist and renders with `line`, counting each kind."""

    model = "fake/text:free"

    def __init__(self, line="Add a clamp helper finished, and all 12 tests pass.", gate=None, echo=False):
        self.line, self.gate, self.echo = line, gate, echo
        self.summaries, self.renders, self.bodies = 0, 0, []
        self.started = threading.Event()

    def complete(self, messages):
        self.started.set()
        if self.gate is not None:
            self.gate.wait()
        self.bodies.append(json.loads(messages[1]["content"]))
        if messages[0]["content"] == SUMMARY_SYSTEM:
            self.summaries += 1
            question = self.bodies[-1].get("question")
            return {"text": f"For {question}: it added clamp." if self.echo else
                    "It added clamp next to sum, and all 12 tests pass."}
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


class LlamaServer(BaseHTTPRequestHandler):
    """llama-server's tokenize and chat routes, recording each chat body."""

    bodies: list = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path == "/tokenize":
            reply = {"tokens": [124901] if body["content"] == "<think>" else [1, 2]}
        else:
            self.bodies.append(body)
            reply = {"choices": [{"message": {"content": "Should I stop it?"}, "finish_reason": "stop"}],
                     "usage": {"prompt_tokens": 300, "completion_tokens": 6}}
        data = json.dumps(reply).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


class LocalTextTest(unittest.TestCase):
    def test_the_local_renderer_bans_thinking_and_passes_the_lane(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), LlamaServer)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        local = LocalText(f"http://127.0.0.1:{server.server_address[1]}")
        record = Lane(local).call([{"role": "user", "content": "{}"}], timeout=5)
        self.assertEqual(record["text"], "Should I stop it?")
        sent = LlamaServer.bodies[-1]
        self.assertEqual((sent["logit_bias"], sent["chat_template_kwargs"]),
                         ([[124901, False]], {"enable_thinking": False}))

    def test_the_local_renderer_refuses_to_run_without_its_think_ban(self):
        class OddTokenizer(LlamaServer):
            def do_POST(self):
                if self.path == "/tokenize":
                    self.rfile.read(int(self.headers["Content-Length"]))
                    data = json.dumps({"tokens": [1, 2]}).encode()
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                else:
                    super().do_POST()

        server = ThreadingHTTPServer(("127.0.0.1", 0), OddTokenizer)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        local = LocalText(f"http://127.0.0.1:{server.server_address[1]}")
        with self.assertRaises(RuntimeError):
            local.complete([{"role": "user", "content": "{}"}])

    def test_the_renderer_is_chosen_by_name(self):
        self.assertIsInstance(text_model("local"), LocalText)
        self.assertEqual(text_model("particle-1", cloud=lambda: "cloud"), "cloud")
        with self.assertRaises(ValueError):
            text_model("particle-2")


class SummaryTest(unittest.TestCase):
    def test_the_summary_is_cached_per_thread_and_sequence(self):
        text, reads = Text(), Reads(finished(140), finished(140), finished(141))
        speech = Speech(Lane(text), reads, F.Agree())
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
        speech = Speech(Lane(text), reads, F.Agree())
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
        speech = Speech(Lane(text), reads, F.Agree())
        speech.prefetch("t5", "How's the clamp thread going?")
        self.assertTrue(text.started.wait(2), "the summary started before anyone asked for it")
        gate.set()
        asked = (act(Kind.THREAD_STATUS, thread="Add a clamp helper", status="finished",
                     asked="how's the clamp thread going"),)
        filled, _ = speech.fill(asked, "t5", (140,))
        self.assertEqual((text.summaries, reads.count), (1, 1), "the final question normalises to the partial one")
        self.assertIn("summary", filled[0].slots)

    def test_two_questions_at_one_sequence_get_two_answers(self):
        text = Text(echo=True)
        speech = Speech(Lane(text), Reads(), F.Agree())
        speech.remember(finished(140))
        gists = []
        for question in ("Did the tests pass?", "What did it change?", "did the tests pass"):
            filled, _ = speech.fill((act(Kind.THREAD_STATUS, thread="Add a clamp helper", status="finished",
                                         asked=question),), "t5", (140,))
            gists.append(filled[0].slots["summary"])
        self.assertEqual(gists, ["For Did the tests pass?: it added clamp.", "For What did it change?: it added clamp.",
                                 "For Did the tests pass?: it added clamp."])
        self.assertEqual(text.summaries, 2, "the third question normalises to the first")

    def test_a_partial_question_never_serves_a_different_final_one(self):
        text, reads = Text(echo=True), Reads(finished(140))
        speech = Speech(Lane(text), reads, F.Agree())
        speech.prefetch("t5", "How's the clamp")
        asked = (act(Kind.THREAD_STATUS, thread="Add a clamp helper", status="finished",
                     asked="How's the clamp thread going?"),)
        filled, _ = speech.fill(asked, "t5", (140,))
        self.assertEqual(filled[0].slots["summary"], "For How's the clamp thread going?: it added clamp.")
        self.assertEqual(text.summaries, 2)

    def test_the_title_is_a_name_and_never_evidence(self):
        text = Text()
        speech = Speech(Lane(text), Reads(), F.Agree())
        unrelated = T.LastReply("I renamed the logger and moved its config.", "m1", None, "r1", "2026-11-18T10:00:00Z")
        speech.remember(F.state(thread="t5", title="Fix failing tests", as_of=3, activity=T.ThreadActivity.FINISHED,
                                last_reply=unrelated))
        speech.fill((act(Kind.THREAD_STATUS, thread="Fix failing tests", status="finished",
                         asked="Did it fix the tests?"),), "t5", (3,))
        records = text.bodies[0]["records"]
        self.assertNotIn("thread", records, "the title is not offered as a record of the work")
        self.assertEqual(records["name (only a name, not evidence of what was done)"], "Fix failing tests")
        self.assertIn("The thread's name is only a name, not evidence of what was done", SUMMARY_SYSTEM)
        self.assertIn("Attribute claims from the last reply to the thread", SUMMARY_SYSTEM)
        self.assertIn("never say the thread did what its name says unless the last reply says it", SUMMARY_SYSTEM)

    def test_a_thread_with_no_reply_is_summarised_from_its_status_and_need(self):
        text = Text()
        waiting = F.state(thread="t5", title="Add a clamp helper", as_of=9, activity=T.ThreadActivity.WAITING_ON_YOU,
                          pending=F.need(prompt="Run pytest?"))
        speech = Speech(Lane(text), Reads(waiting), F.Agree())
        filled, _ = speech.fill((act(Kind.NEEDS_YOU, prompt="Run pytest?"),), "t5")
        self.assertEqual(text.summaries, 1, "no reply is no reason to skip the answer")
        self.assertEqual(text.bodies[0]["records"]["last_reply"], None)
        self.assertEqual((text.bodies[0]["records"]["status"], text.bodies[0]["records"]["pending"]),
                         ("waiting_on_you", "Run pytest?"))
        self.assertIn("summary", filled[0].slots)

    def test_no_read_means_no_summary(self):
        text = Text()
        unread = Speech(Lane(text), lambda thread_id: None)
        self.assertEqual(unread.fill((act(Kind.DONE, status="failed"),), "t5"),
                         ((act(Kind.DONE, status="failed"),), ()))


class RenderTest(unittest.TestCase):
    def test_a_line_that_fails_the_check_is_not_returned(self):
        acts = (act(Kind.CONFIRM_STOP, thread="Add a clamp helper"),)
        good = Speech(Lane(Text("Should I stop Add a clamp helper?")), Reads(), F.Agree()).render(acts)
        self.assertEqual((good.text, good.line.acts, good.line.worded_by, good.problems),
                         ("Should I stop Add a clamp helper?", acts, "text_model", []))
        bad = Speech(Lane(Text("I've stopped Add a clamp helper.")), Reads(), F.Agree()).render(acts)
        self.assertIsNone(bad.line)
        self.assertEqual(bad.problems, ["not a question"])
        self.assertIsNone(Speech(None, Reads(), F.Agree()).render(acts).line, "no model, no line")
        unchecked = Speech(Lane(Text("Should I stop Add a clamp helper?")), Reads()).render(acts)
        self.assertEqual((unchecked.line, unchecked.error), (None, "no decider to check the line"))

    def test_the_prompt_carries_the_facts_the_turns_and_the_last_refusal(self):
        text = Text("Should I stop Add a clamp helper?")
        acts = (act(Kind.CONFIRM_STOP, thread="Add a clamp helper"),)
        Speech(Lane(text), Reads(), F.Agree()).render(acts, [{"user": "Stop the clamp thread."}], ["not a question"])
        body = text.bodies[0]
        self.assertEqual((body["keep_exact"], body["ask"], body["turns"]),
                         (["Add a clamp helper"], True, [{"person": "Stop the clamp thread."}]))
        self.assertIn("that the thread stopped or was cancelled", body["never_say"])
        self.assertEqual(body["an earlier wording was refused because"], ["not a question"])

    def test_the_realtime_reader_gets_the_existing_thread_direction_and_example(self):
        for title in ("Rotate Signing Keys", "Refresh Onboarding Checklist"):
            with self.subTest(title=title):
                system, user = reader_messages((act(Kind.CONFIRM_THREAD, thread=title),))
                self.assertIn("existing thread named in thread", system["content"])
                self.assertNotIn(title, system["content"])
                self.assertEqual(json.loads(user["content"]), {"kind": "confirm_thread", "thread": title})


    def test_known_and_example_titles_are_rejected_in_the_draft_and_the_call_may_say_only_the_draft(self):
        acts = (act(Kind.CONFIRM_THREAD, thread="Current Thread Notes"),)
        titles = frozenset({"Current Thread", "Archived Project"})
        legitimate = "Is Current Thread Notes the thread you mean?"
        speech = Speech(Lane(Text(legitimate)), Reads(), F.Agree())
        draft = speech.render(acts, known_names=titles).line
        self.assertIsNotNone(draft)
        for title in ("Current Thread", "Archived Project", "Example Polish Checkout Layout"):
            with self.subTest(title=title):
                text = f"Is Current Thread Notes or {title} the thread you mean?"
                wrong = Speech(Lane(Text(text)), Reads(), F.Agree()).render(acts, known_names=titles)
                self.assertEqual(wrong.problems, [f"unlisted title: {title}"])
                call = speech.recite(draft, text)
                self.assertIsNone(call.line, "the call may say only the checked draft")


class WriterSession(FakeSession):
    def __init__(self, name, on_event):
        super().__init__(name, on_event)
        self.closing, self.allow_close = asyncio.Event(), asyncio.Event()
        self.ws = Mock()
        self.ws.close = self._close_socket
        self._reader = Mock()
        self.close_error = None

    async def send(self, **event):
        if event["type"] == "response.create":
            self.sent.append(event)
            await self.push({"type": "response.output_text.delta", "delta": "Is Rotate Signing Keys the thread?"})
            await self.push({"type": "response.done", "response": {"status": "completed"}})
        else:
            await super().send(**event)

    async def _close_socket(self):
        self.closing.set()
        if self.close_error is not None:
            raise self.close_error
        await self.allow_close.wait()
        self.closed = True

    close = Session.close


class RealtimeWriterTest(unittest.IsolatedAsyncioTestCase):
    messages = reader_messages((act(Kind.CONFIRM_THREAD, thread="Rotate Signing Keys"),))

    async def test_a_third_draft_waits_for_a_closing_session_instead_of_failing(self):
        writer = RealtimeWriter("ws://unused")
        sessions = []

        async def capture_session(url, name, clock):
            session = await WriterSession.open(url, name, clock)
            sessions.append(session)
            return session

        with patch.object(Session, "open", new=capture_session):
            async with writer:
                for _ in range(2):
                    await asyncio.to_thread(writer.complete, self.messages)
                third = asyncio.create_task(asyncio.to_thread(writer.complete, self.messages))
                for session in sessions:
                    await session.closing.wait()
                await asyncio.sleep(0)
                self.assertFalse(third.done(), "two sessions are still closing, so the third waits")
                self.assertEqual(len(sessions), 2)
                sessions[0].allow_close.set()
                self.assertEqual((await third)["text"], "Is Rotate Signing Keys the thread?")
                for session in sessions:
                    session.allow_close.set()
        self.assertEqual(len(sessions), 3)

    async def test_drafts_return_on_fresh_sessions_before_close_and_shutdown_drains(self):
        drafts_ready = asyncio.Event()
        writer = RealtimeWriter("ws://unused")
        sessions, results = [], []

        async def capture_session(url, name, clock):
            session = await WriterSession.open(url, name, clock)
            sessions.append(session)
            return session

        async def write():
            async with writer:
                for _ in range(2):
                    results.append(await asyncio.to_thread(writer.complete, self.messages))
                drafts_ready.set()

        with patch.object(Session, "open", new=capture_session):
            async with asyncio.TaskGroup() as group:
                work = group.create_task(write())
                await drafts_ready.wait()
                try:
                    self.assertEqual(len(sessions), 2)
                    for session in sessions:
                        await session.closing.wait()
                        self.assertFalse(session.closed)
                    self.assertFalse(work.done())
                finally:
                    for session in sessions:
                        session.allow_close.set()

        self.assertEqual([r["text"] for r in results], ["Is Rotate Signing Keys the thread?"] * 2)
        self.assertTrue(all(isinstance(r["ms"], float) for r in results))
        first, second = sessions
        self.assertIsNot(first, second)
        for session in sessions:
            self.assertEqual(session.configs[0]["output_modalities"], ["text"])
            received, = [e["item"] for e in session.sent if e["type"] == "conversation.item.create"]
            self.assertEqual(received["role"], "user")
            self.assertEqual(received["content"], [{"type": "input_text", "text": self.messages[1]["content"]}])
            self.assertEqual(session.audio, [])
            self.assertTrue(session.closed)

    async def test_close_timeout_or_error_logs_and_aborts_without_leaking_capacity(self):
        for error in (None, OSError("close failed")):
            with self.subTest(error=error):
                sessions = []

                async def capture_session(url, name, clock):
                    session = await WriterSession.open(url, name, clock)
                    session.close_error = error
                    sessions.append(session)
                    return session

                writer = RealtimeWriter("ws://unused")
                with patch.object(Session, "open", new=capture_session), \
                        patch("voice.t3.realtime_writer.CLOSE_TIMEOUT_S", 0), \
                        self.assertLogs("voice.t3.realtime_writer", "WARNING") as logs:
                    async with writer:
                        for _ in range(3):
                            result = await asyncio.to_thread(writer.complete, self.messages)
                            self.assertEqual(result["text"], "Is Rotate Signing Keys the thread?")

                self.assertEqual(len(logs.output), 3)
                expected = "TimeoutError" if error is None else "close failed"
                self.assertTrue(all(expected in line for line in logs.output))
                for session in sessions:
                    session.ws.transport.abort.assert_called_once_with()
                    session._reader.cancel.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
