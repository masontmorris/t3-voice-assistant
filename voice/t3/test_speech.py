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
from voice.t3 import local_summary as LS
from voice.t3 import types as T
from voice.t3.acts import Kind, act
from voice.t3.pick.model import ModelFailed
from voice.t3.realtime_writer import RealtimeWriter
from voice.t3.verify import Decider
from voice.t3.speech import (LINE_SCHEMA, READER_VARIANTS, RENDER_SYSTEM, SUMMARY_SYSTEM, SUMMARY_TIMEOUT_S, Lane,
                              LocalText, Speech, local_messages, read_line, reader_messages, render_messages, text_model)

REPLY = T.LastReply("I added clamp next to sum. All 12 tests pass.", "m1", None, "r1", "2026-11-18T10:00:00Z")


class SpokenOrder(unittest.TestCase):
    """"The second one" is read against the order the options were stored in, so a line may only tell them in that
    order: a line that tells them in another is never minted, however well it names each."""

    ASK = act(Kind.ASK_QUESTION, thread="Tidy the Save Slots", question="Which storage should it use?",
              options=["Postgres", "SQLite"])

    def test_a_line_that_tells_the_options_in_another_order_is_refused_and_one_in_order_is_minted(self):
        speech = Speech(None, lambda thread_id: None, F.Agree())
        reordered = speech.accept((self.ASK,), "Tidy the Save Slots asks which storage to use: SQLite or Postgres?",
                                  "test")
        self.assertIsNone(reordered.line)
        self.assertIn("ask_question options out of order", reordered.problems[0])
        said = speech.accept((self.ASK,), "Tidy the Save Slots asks which storage to use: Postgres or SQLite?", "test")
        self.assertIsNotNone(said.line)

    def test_the_decider_is_asked_for_each_option_by_its_place_in_the_order_stored(self):
        from voice.t3.verify import asks

        names = {a.name: a.question for a in asks([self.ASK])}
        self.assertIn("choice number 2 of 2", names["a1_options_2"])
        self.assertIn("Postgres, SQLite", names["a1_options_2"])


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


class Cooling:
    """The paid summarizer while `LiveClient` cools down after a failed call."""

    model = "fake/text:free"

    def complete(self, messages):
        raise ModelFailed("the picker is cooling down after a failed pick")


class Writes:
    """The local writer: it words whatever it is given as `line`."""

    model = "fake/text:free"

    def __init__(self, line):
        self.line = line

    def complete(self, messages):
        return {"text": self.line}


class Answers:
    """A decider client that answers the reuse question with `p_yes`, or raises it when it is an exception."""

    def __init__(self, p_yes):
        self.p_yes, self.asked = p_yes, []

    def call(self, route, request, label=""):
        self.asked.append(request["state"])
        if isinstance(self.p_yes, Exception):
            raise self.p_yes
        return {"status": 200, "response": {"answers": {"answers": {"type": "noul", "noul": self.p_yes}}}}


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
            reply = {"choices": [{"message": {"content": json.dumps({"line": "Should I stop it?"})}, "finish_reason": "stop"}],
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
        self.assertEqual(sent["response_format"]["json_schema"]["schema"], LINE_SCHEMA)

    def test_output_that_is_not_a_line_is_no_line(self):
        for text in ('{"line": "Should I stop it?', "Should I stop it?", '{"words": "Should I stop it?"}', '{"line": 3}'):
            with self.subTest(text=text):
                self.assertEqual(read_line(text), "")

    def test_the_local_writer_gets_the_examples_as_chat_turns_and_the_answer_as_json(self):
        acts = (act(Kind.ASK_WHICH, of="thread", candidates=["Alpha", "Beta"]),)
        system, example_in, example_out, record = local_messages(acts)
        self.assertEqual((system["role"], example_in["role"], example_out["role"], record["role"]),
                         ("system", "user", "assistant", "user"))
        self.assertEqual(json.loads(example_out["content"]), {"line": READER_VARIANTS[(Kind.ASK_WHICH, "thread")][1]})
        self.assertNotIn("Respond with interleaved", system["content"])
        self.assertEqual(json.loads(record["content"].removeprefix("New record: ")),
                         {"kind": "ask_which", "of": "thread", "candidates": ["Alpha", "Beta"]})

    def test_a_local_lane_is_worded_with_the_chat_form_and_any_other_with_the_system_prompt(self):
        local = Speech(Lane(LocalText("http://127.0.0.1:1")), Reads())
        self.assertIs(local._lane_messages(), local_messages)
        self.assertIs(Speech(Lane(Text()), Reads())._lane_messages(), render_messages)

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

    def summariser(self, p_unsupported, held=None, p_reuse=None):
        """A local summariser whose model answers a gist with an extra key and whose gate answers `p_unsupported`,
        or has no server at all when it is None. With `held`, the gate answers only once it is set."""

        class Local(LlamaServer):
            def do_POST(self):
                if self.path == "/v1/systemone":
                    body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                    Local.gated.append(body)
                    if held is not None:
                        held.wait(30)
                    name, p = ("answers", p_reuse) if "answers" in body["questions"] else ("unsupported", p_unsupported)
                    reply = {"answers": {name: {"type": "noul", "noul": p}}}
                elif self.path == "/v1/chat/completions":
                    body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                    self.bodies.append(body)
                    reply = {"choices": [{"message": {"content": json.dumps(
                        {"gist": "It says all 12 tests pass.", "status": "finished"})}, "finish_reason": "stop"}]}
                else:
                    return super().do_POST()
                data = json.dumps(reply).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        Local.gated = []
        server = ThreadingHTTPServer(("127.0.0.1", 0), Local)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        if held is not None:
            self.addCleanup(held.set)
        url = f"http://127.0.0.1:{server.server_address[1]}"
        gate = url if p_unsupported is not None else "http://127.0.0.1:9"
        return LocalText(url, gate_url=gate), Local

    def test_the_local_summariser_sends_the_measured_request_and_speaks_a_gist_the_gate_passes(self):
        local, server = self.summariser(0.02)
        speech = Speech(Lane(local), Reads(finished(140)), F.Agree())
        asked = (act(Kind.THREAD_STATUS, thread="Add a clamp helper", status="finished", asked="Did the tests pass?"),)
        filled, _ = speech.fill(asked, "t5")
        self.assertEqual(filled[0].slots["summary"], "It says all 12 tests pass.", "the extra key is ignored")
        sent = server.bodies[-1]
        self.assertEqual(sent["messages"], LS.messages(finished(140), "Did the tests pass?"))
        self.assertEqual((sent["temperature"], sent["top_k"], sent["repeat_penalty"], sent["max_tokens"]),
                         (0.1, 50, 1.1, 60))
        self.assertEqual(sent["response_format"]["json_schema"]["schema"], LS.GIST_SCHEMA)
        self.assertEqual(server.gated[-1]["state"]["gist"], "It says all 12 tests pass.")

    def test_the_local_summariser_asks_its_own_gate_whether_an_earlier_gist_answers(self):
        local, server = self.summariser(0.02, p_reuse=0.99)
        speech = Speech(Lane(local), Reads(finished(140)), F.Agree())
        speech.prefetch("t5", "Did the")
        for gist in list(speech.summaries.values()):
            gist.future.result(timeout=5)
        written = len(server.bodies)
        asked = (act(Kind.THREAD_STATUS, thread="Add a clamp helper", status="finished", asked="Did the tests pass?"),)
        filled, _ = speech.fill(asked, "t5")
        self.assertEqual(filled[0].slots["summary"], "It says all 12 tests pass.")
        self.assertEqual(len(server.bodies), written, "the gist was not written again")
        self.assertEqual([b["state"].get("question") for b in server.gated], [None, "Did the tests pass?"])

    def test_a_gist_the_gate_does_not_pass_leaves_the_status_bare(self):
        for p_unsupported, why in ((0.4, "the gate finds it unsupported"), (None, "no gate answers")):
            local, _ = self.summariser(p_unsupported)
            speech = Speech(Lane(local), Reads(finished(140)), F.Agree())
            asked = (act(Kind.THREAD_STATUS, thread="Add a clamp helper", status="finished", asked="Is it done?"),)
            filled, _ = speech.fill(asked, "t5")
            self.assertNotIn("summary", filled[0].slots, why)
            self.assertEqual(filled[0].slots["thread"], "Add a clamp helper")

    def test_a_gate_that_does_not_answer_drops_the_gist_before_the_status_line_stops_waiting(self):
        held = threading.Event()
        local, server = self.summariser(0.02, held=held)
        lane = Lane(local)
        record = lane.summarise(finished(140), "Is it done?").result(timeout=SUMMARY_TIMEOUT_S)
        self.assertEqual((record["text"], record["gist"]), ("", "It says all 12 tests pass."))
        held.set()
        record = lane.summarise(finished(140), "Is it done?").result(timeout=SUMMARY_TIMEOUT_S)
        self.assertEqual(record["text"], "It says all 12 tests pass.", "the late answer is not read as the next one")
        self.assertEqual(len(server.gated), 2)

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

    def early(self, decider_answer):
        """A summary started from partial words, written, then the final question, with the reuse question answered
        by `decider_answer`."""
        text, answers = Text(echo=True), Answers(decider_answer)
        speech = Speech(Lane(text), Reads(finished(140)), F.Agree(), reuse=Decider(answers))
        speech.prefetch("t5", "How's the clamp")
        for gist in list(speech.summaries.values()):
            gist.future.result(timeout=2)
        asked = (act(Kind.THREAD_STATUS, thread="Add a clamp helper", status="finished",
                     asked="How's the clamp thread going?"),)
        filled, _ = speech.fill(asked, "t5", (140,))
        return text, answers, filled[0].slots.get("summary")

    def test_an_earlier_summary_the_decider_says_answers_the_final_question_is_reused(self):
        text, answers, summary = self.early(0.9)
        self.assertEqual((summary, text.summaries), ("For How's the clamp: it added clamp.", 1))
        self.assertEqual((answers.asked[0]["summary_was_for"], answers.asked[0]["question"]),
                         ("How's the clamp", "How's the clamp thread going?"))

    def test_without_a_confident_yes_the_summary_is_written_again(self):
        for answer in (0.2, True, None, ValueError("the decider is down")):
            text, _, summary = self.early(answer)
            self.assertEqual((summary, text.summaries), ("For How's the clamp thread going?: it added clamp.", 2),
                             answer)

    def test_a_summary_from_the_model_before_a_switch_is_not_reused(self):
        before, after = Text(), Text()
        lane = Lane(before)
        speech = Speech(lane, Reads(finished(140)), F.Agree())
        speech.remember(finished(140))
        asked = (act(Kind.THREAD_STATUS, thread="Add a clamp helper", status="finished", asked="Is it done?"),)
        speech.fill(asked, "t5", (140,))
        lane.use(after)
        filled, _ = speech.fill(asked, "t5", (140,))
        self.assertEqual((before.summaries, after.summaries), (1, 1), "the new model writes its own summary")
        self.assertIn("summary", filled[0].slots)

    def test_a_summary_whose_caller_gave_up_never_runs(self):
        gate = threading.Event()
        text = Text(gate=gate)
        lane = Lane(text)
        speech = Speech(lane, Reads(), F.Agree())
        running = speech._summary(finished(140), "Is it done?")
        self.assertTrue(text.started.wait(2))
        other = F.state(thread="t6", title="Trim image margins", as_of=7, activity=T.ThreadActivity.FINISHED,
                        last_reply=REPLY)
        with patch("voice.t3.speech.SUMMARY_TIMEOUT_S", 0.05):
            self.assertIsNone(speech.summary(other, "Did it finish?"))
        gate.set()
        running.result(timeout=2)
        lane.pool.shutdown(wait=True)
        self.assertEqual(text.summaries, 1, "the queued summary its caller gave up on never reached the model")

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

    def test_a_summarizer_that_is_cooling_down_still_lets_the_status_line_be_drafted(self):
        writer = Lane(Writes("Add a clamp helper is finished."))
        speech = Speech(Lane(Cooling()), Reads(finished(140)), F.Agree(), writer)
        filled, _ = speech.fill((act(Kind.THREAD_STATUS, status="finished", asked="How is it going?"),), "t5")
        self.assertEqual(filled[0].slots, {"status": "finished", "asked": "How is it going?",
                                           "thread": "Add a clamp helper"}, "no summary, and the title still")
        self.assertEqual(speech.render(filled).text, "Add a clamp helper is finished.")

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

    def test_a_refused_writer_draft_is_worded_again_by_the_text_model_told_why(self):
        acts = (act(Kind.CONFIRM_STOP, thread="Add a clamp helper"),)
        text = Text("Should I stop Add a clamp helper?")
        speech = Speech(Lane(text), Reads(), F.Agree(), Lane(Writes("I've stopped Add a clamp helper.")))
        rendered = speech.render(acts)
        self.assertEqual((rendered.text, rendered.line.worded_by), ("Should I stop Add a clamp helper?", "text_model"))
        body = text.bodies[0]
        self.assertEqual(body["an earlier wording was refused because"], ["not a question"])
        self.assertEqual(body["refused wordings"], ["I've stopped Add a clamp helper."])

    def test_a_passing_writer_draft_never_reaches_the_text_model(self):
        acts = (act(Kind.CONFIRM_STOP, thread="Add a clamp helper"),)
        text = Text()
        speech = Speech(Lane(text), Reads(), F.Agree(), Lane(Writes("Should I stop Add a clamp helper?")))
        self.assertEqual(speech.render(acts).line.worded_by, "lfm3")
        self.assertEqual(text.renders, 0)

    def test_when_the_writer_and_the_text_model_both_fail_the_writers_refusal_is_returned(self):
        acts = (act(Kind.CONFIRM_STOP, thread="Add a clamp helper"),)
        speech = Speech(Lane(Cooling()), Reads(), F.Agree(), Lane(Writes("I've stopped Add a clamp helper.")))
        rendered = speech.render(acts)
        self.assertEqual((rendered.line, rendered.problems, rendered.raw),
                         (None, ["not a question"], "I've stopped Add a clamp helper."))

    def test_the_writer_is_shown_a_thread_example_for_a_thread_question_and_a_project_one_otherwise(self):
        which = lambda of: reader_messages((act(Kind.ASK_WHICH, of=of, candidates=["Alpha", "Beta"]),))[0]["content"]
        self.assertIn("Which thread do you mean", which("thread"))
        self.assertNotIn("Which project do you mean", which("thread"))
        self.assertIn("Which project do you mean", which("project"))

    def test_the_realtime_reader_gets_the_existing_thread_direction_and_example(self):
        for title in ("Rotate Signing Keys", "Refresh Onboarding Checklist"):
            with self.subTest(title=title):
                system, user = reader_messages((act(Kind.CONFIRM_THREAD, thread=title),))
                self.assertIn("existing thread named in thread", system["content"])
                self.assertNotIn(title, system["content"])
                self.assertEqual(json.loads(user["content"].removeprefix("New record: ")), {"kind": "confirm_thread", "thread": title})


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
                self.assertEqual(len(sessions), 2, "two sessions are still closing, so the third has none to open")
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
