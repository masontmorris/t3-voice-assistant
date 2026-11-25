"""The voice's text model: one request in flight, summaries cached per thread and read sequence, and lines that
pass the slot check or are not returned."""

from __future__ import annotations

import asyncio
import dataclasses
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
from voice.t3.verify import Decider, Verdict
from voice.t3.speech import (LINE_SCHEMA, READER_VARIANTS, RENDER_SYSTEM, SUMMARY_SYSTEM, SUMMARY_TIMEOUT_S, Lane,
                              LocalText, RequestGate, Speech, local_messages, read_line, reader_messages, render_messages,
                              text_model, wording_direction, Launch, named, reference)
from voice.t3.journal import launch_title

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
        self.assertEqual(json.loads(example_out["content"]), {"line": READER_VARIANTS[(Kind.ASK_WHICH, "thread_pair")][1]})
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
                    if "accurate" in body["questions"]:
                        name, p = "accurate", 1 - p_unsupported
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

    def test_the_local_model_describes_a_request_in_the_approval_prompt_and_the_gate_judges_it_apart(self):
        need = F.need("q1", "rm -rf dist && pnpm build")
        for p_unsupported, spoken in ((0.04, True), (0.4, False), (0.11, False)):
            local, server = self.summariser(p_unsupported)
            text = local.describe(need)["text"]
            sent = server.bodies[-1]
            self.assertEqual(sent["messages"], LS.request_messages(need))
            self.assertEqual(sent["response_format"]["json_schema"]["schema"], LS.REQUEST_SCHEMA)
            self.assertEqual(text, "It says all 12 tests pass.")
            gate = RequestGate(local.gate_url)
            doubt = gate.doubt(need, text)
            self.assertEqual(server.gated[-1]["state"], LS.request_gate(need, text)["state"])
            self.assertEqual(doubt is not None and doubt < LS.REQUEST_THRESHOLD, spoken, p_unsupported)
        self.assertIsNone(RequestGate("http://127.0.0.1:9").doubt(need, "It builds."), "a gate that is down is no answer")

    def test_a_line_about_a_pending_request_gets_the_gated_description_or_only_its_kind(self):
        class Describes:
            model = "fake/text:free"

            def complete(self, messages):
                return {"text": json.dumps({"gist": "It wants to write a note into a file."})}

        class Gate:
            def __init__(self, answer):
                self.answer, self.asked = answer, []

            def doubt(self, need, description):
                self.asked.append(need.prompt)
                return self.answer

        waiting = F.state(thread="t5", title="Add a clamp helper", as_of=140, activity=T.ThreadActivity.WAITING_ON_YOU,
                          pending=F.need("q1", "printf 'ready' > note.txt"))
        for kind, stored in ((Kind.NEEDS_YOU, "prompt"), (Kind.THREAD_STATUS, "pending"), (Kind.CORRECTION, "pending")):
            said = (act(kind, thread="Add a clamp helper", need="command_approval", **{stored: "printf 'ready' > note.txt"}),)
            for doubt, described in ((0.01, True), (0.4, False), (None, False)):
                gate = Gate(doubt)
                speech = Speech(Lane(Describes()), Reads(waiting), F.Agree(), gate=gate)
                filled, _ = speech.fill(said, "t5")
                with self.subTest(kind=kind.value, doubt=doubt):
                    self.assertNotIn(stored, filled[0].slots, "the stored text never reaches the writer")
                    self.assertEqual(filled[0].slots.get("request"),
                                     "It wants to write a note into a file." if described else None)
                    self.assertEqual(gate.asked, ["printf 'ready' > note.txt"])

    def test_a_request_that_changed_is_not_described_from_the_new_one(self):
        class Gate:
            asked = []

            def doubt(self, need, description):
                self.asked.append(need.prompt)
                return 0.0

        waiting = F.state(thread="t5", title="Add a clamp helper", as_of=140, activity=T.ThreadActivity.WAITING_ON_YOU,
                          pending=F.need("q2", "rm -rf build"))
        gate = Gate()
        speech = Speech(Lane(Text()), Reads(waiting), F.Agree(), gate=gate)
        filled, _ = speech.fill((act(Kind.NEEDS_YOU, need="command_approval", prompt="printf 'ready' > note.txt"),), "t5")
        self.assertEqual((gate.asked, "request" in filled[0].slots), ([], False))

    def test_a_file_read_is_never_described(self):
        waiting = F.state(thread="t5", title="Add a clamp helper", as_of=140, activity=T.ThreadActivity.WAITING_ON_YOU,
                          pending=F.need("q3", "notes.md", source=T.NeedSource.FILE_READ))
        speech = Speech(Lane(Text()), Reads(waiting), F.Agree(), gate=type("G", (), {"doubt": lambda *a: 0.0})())
        filled, _ = speech.fill((act(Kind.NEEDS_YOU, need="file_read", prompt="notes.md"),), "t5")
        self.assertEqual(filled[0].slots, {"need": "file_read", "thread": "Add a clamp helper"})

    def test_a_tool_permission_confirmation_carries_the_tool_only_while_no_description_passed(self):
        class Gate:
            def __init__(self, doubt):
                self.doubt_of = doubt

            def doubt(self, need, description):
                return self.doubt_of

        class Describes(Text):
            def complete(self, messages):
                if messages[0]["content"] == LS.REQUEST_SYSTEM:
                    return {"text": json.dumps({"gist": "It wants to use a tool that lists notes."})}
                return super().complete(messages)

        need = F.need("q3", "list_notes", source=T.NeedSource.PERMISSION)
        confirm = (act(Kind.CONFIRM_RESPOND, thread="Add a clamp helper", decision="approve", need="permission",
                       tool="list_notes"),)
        for doubt, slots in ((0.01, {"summary": "It wants to use a tool that lists notes."}), (0.4, {"tool": "list_notes"})):
            speech = Speech(Lane(Describes()), Reads(finished(140)), F.Agree(), gate=Gate(doubt))
            filled, _ = speech.fill(confirm, "t5", (140,), need)
            self.assertEqual({k: v for k, v in filled[0].slots.items() if k in ("summary", "tool")}, slots)

    def test_a_launched_thread_is_named_by_its_id_and_by_the_title_it_has_now(self):
        request = "Um, start a thread to sort the pantry list, a long hesitant request that runs well past the limit."
        other = "Uh, I want a thread to tidy the lantern config files, and keep the formatting the same as before ok."
        launched = (Launch("t7", request, "Voice sandbox"), Launch("t8", other, "Voice sandbox"))
        title, other_title = launch_title(request), launch_title(other)
        said = (act(Kind.NEEDS_YOU, thread=title, need="command_approval"),
                act(Kind.CONFIRM_STOP, thread="Sort the pantry list"), act(Kind.NO_THREAD, other=True))
        alone = reference("t7", launched, {"t7": title, "t8": "Tidy the lantern config"})
        self.assertEqual((alone.unnamed, alone.shared), (True, False))
        got = named(said, alone, None)
        self.assertEqual(got[0].slots, {"begun_in": "Voice sandbox", "need": "command_approval"})
        self.assertEqual(got[1:], said[1:], "an act with another title, or with no thread, is not about this one")
        self.assertIn("in place of a thread name", wording_direction(got[0]))
        untitled = act(Kind.REPLIED, status="running", reply="Setting it up now.")
        self.assertEqual(named((untitled,), alone, "sorting the pantry list")[0].slots,
                         {"status": "running", "reply": "Setting it up now.", "begun_in": "Voice sandbox",
                          "begun_for": "sorting the pantry list"},
                         "news made before the thread had a title is named as every other line about it is")
        both = reference("t7", launched, {"t7": title, "t8": other_title})
        self.assertEqual(named(said, both, "sorting the pantry list")[0].slots,
                         {"begun_in": "Voice sandbox", "begun_for": "sorting the pantry list", "need": "command_approval"})
        self.assertEqual(named(said, both, None)[0].slots, {"thread": title, "need": "command_approval"},
                         "no gated account, so the thread is not guessed at")
        self.assertIsNone(reference("t9", launched, {"t9": title}), "a thread the voice did not start")
        self.assertIsNone(reference("t7", launched, {}), "a thread T3 does not list")
        retitled = reference("t7", launched, {"t7": "Sort the pantry list", "t8": other_title})
        self.assertEqual(named(said, retitled, None)[0].slots, {"thread": "Sort the pantry list", "need": "command_approval"})
        elsewhere = reference("t7", (Launch("t7", request, "Voice sandbox"), Launch("t8", other, "Pebble Garden")),
                              {"t7": title, "t8": other_title})
        self.assertFalse(elsewhere.shared, "another project's thread does not make the project ambiguous")

    def test_a_summary_prefetched_from_a_state_with_a_request_never_carries_it(self):
        text = Text()
        waiting = F.state(thread="t5", title="Add a clamp helper", as_of=9, activity=T.ThreadActivity.WAITING_ON_YOU,
                          pending=F.need(prompt="rm -rf build"), last_reply=REPLY)
        speech = Speech(Lane(text), Reads(waiting), F.Agree(), gate=None)
        speech.prefetch("t5", "How is it going?")
        filled, _ = speech.fill((act(Kind.THREAD_STATUS, status="waiting_on_you", need="command_approval",
                                     pending="rm -rf build", asked="How is it going?"),), "t5")
        self.assertEqual(text.summaries, 1, "the prefetched summary is the one reused")
        self.assertNotIn("rm -rf build", json.dumps(text.bodies), "no model call was shown the request")
        self.assertIn("summary", filled[0].slots)
        self.assertNotIn("pending", filled[0].slots)

    def test_a_thread_with_only_a_request_is_not_prefetched(self):
        text = Text()
        waiting = F.state(thread="t5", title="Add a clamp helper", as_of=9, activity=T.ThreadActivity.WAITING_ON_YOU,
                          pending=F.need(prompt="rm -rf build"))
        speech = Speech(Lane(text), Reads(waiting), F.Agree())
        speech.prefetch("t5", None)
        self.assertEqual(text.summaries, 0)

    def test_an_ordinary_summary_is_never_the_description_of_a_request_and_the_reverse(self):
        class Both:
            model = "fake/text:free"

            def __init__(self):
                self.summaries = self.descriptions = 0

            def complete(self, messages):
                if messages[0]["content"] == LS.REQUEST_SYSTEM:
                    self.descriptions += 1
                    return {"text": json.dumps({"gist": "It wants to delete the dist folder and rebuild."})}
                self.summaries += 1
                return {"text": "The build passed."}

        class Gate:
            asked = []

            def doubt(self, need, description):
                self.asked.append((need.prompt, description))
                return 0.01

        waiting = F.state(thread="t5", title="Add a clamp helper", as_of=140, activity=T.ThreadActivity.WAITING_ON_YOU,
                          pending=F.need("q1", "rm -rf dist && pnpm build"), last_reply=REPLY)
        model, gate = Both(), Gate()
        speech = Speech(Lane(model), Reads(waiting), F.Agree(), gate=gate)
        status = (act(Kind.THREAD_STATUS, thread="Add a clamp helper", status="waiting_on_you"),)
        filled, _ = speech.fill(status, "t5")
        self.assertEqual((filled[0].slots["summary"], model.summaries, gate.asked), ("The build passed.", 1, []))
        confirm = (act(Kind.CONFIRM_RESPOND, thread="Add a clamp helper", decision="approve", need="command_approval"),)
        said, _ = speech.fill(confirm, "t5", (140,), waiting.pending)
        self.assertEqual(said[0].slots["summary"], "It wants to delete the dist folder and rebuild.",
                         "the status summary cached at the same sequence did not stand in for the description")
        self.assertEqual((model.summaries, model.descriptions), (1, 1))
        self.assertEqual(gate.asked, [("rm -rf dist && pnpm build", "It wants to delete the dist folder and rebuild.")])
        speech.fill(confirm, "t5", (140,), waiting.pending)
        self.assertEqual(model.descriptions, 1, "the same request, with the same content, is described once")
        other = F.need("q1", "rm -rf dist build && pnpm build")
        speech.fill(confirm, "t5", (140,), other)
        self.assertEqual(model.descriptions, 2, "the same id with other content is described again")
        again, _ = speech.fill((act(Kind.THREAD_STATUS, thread="Add a clamp helper", status="waiting_on_you",
                                    asked="Is it done?"),), "t5")
        self.assertEqual((again[0].slots["summary"], model.summaries), ("The build passed.", 2),
                         "and a description is never an ordinary summary")

    def test_an_approval_confirmation_never_keeps_a_summary_it_was_given_and_a_denial_has_none(self):
        text = Text()
        speech = Speech(Lane(text), Reads(finished(140)), F.Agree(), gate=None)
        given = act(Kind.CONFIRM_RESPOND, thread="T", decision="approve", need="command_approval", summary="It is safe.")
        filled, _ = speech.fill((given,), "t5", (), F.need("q1", "pnpm test"))
        self.assertNotIn("summary", filled[0].slots, "no gate, so no description, and not the one it came with")
        denial = act(Kind.CONFIRM_RESPOND, thread="T", decision="deny", need="command_approval")
        self.assertEqual(speech.fill((denial,), "t5", (), F.need("q1", "pnpm test"))[0][0].slots, denial.slots)
        self.assertEqual(text.summaries, 0)

    def test_a_request_the_voice_cannot_describe_is_not_described_by_any_model(self):
        for source in (T.NeedSource.FILE_READ, T.NeedSource.MCP_ELICITATION):
            text = Text()

            class Gate:
                def doubt(self, need, description):
                    raise AssertionError("not asked")

            speech = Speech(Lane(text), Reads(finished(140)), F.Agree(), gate=Gate())
            self.assertIsNone(speech.describe("t5", F.need("q1", "mcp__github__create_issue", source=source)))
            self.assertEqual(text.summaries, 0)

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

    def test_a_thread_with_no_reply_and_a_request_is_not_summarised(self):
        text = Text()
        waiting = F.state(thread="t5", title="Add a clamp helper", as_of=9, activity=T.ThreadActivity.WAITING_ON_YOU,
                          pending=F.need(prompt="Run pytest?"))
        speech = Speech(Lane(text), Reads(waiting), F.Agree())
        filled, _ = speech.fill((act(Kind.NEEDS_YOU, need="command_approval", prompt="Run pytest?"),), "t5")
        self.assertEqual(text.summaries, 0, "what the request would do is the description's job, not a summary's")
        self.assertEqual(filled[0].slots, {"need": "command_approval", "thread": "Add a clamp helper"},
                         "no gate, so no description, and the stored text never reaches the writer")

    def test_a_summary_beside_a_request_is_written_without_the_request(self):
        text = Text()
        waiting = F.state(thread="t5", title="Add a clamp helper", as_of=9, activity=T.ThreadActivity.WAITING_ON_YOU,
                          pending=F.need(prompt="rm -rf build"), last_reply=REPLY)
        speech = Speech(Lane(text), Reads(waiting), F.Agree())
        speech.fill((act(Kind.THREAD_STATUS, status="waiting_on_you", need="command_approval", pending="rm -rf build"),),
                    "t5")
        self.assertEqual(text.summaries, 1)
        self.assertIsNone(text.bodies[0]["records"]["pending"])

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


class Remembers:
    """A decider that, as the hosted one does, serves the first reading it gave for a line again until it is asked
    for a fresh one, which it does not keep. `readings` are what it reads, in order, one for each time it really reads;
    `asked` records each ask and how many fresh readings it was for. Several fresh readings pass only if all do."""

    def __init__(self, *readings):
        self.readings, self.kept, self.asked = list(readings), {}, []

    def verify(self, acts, line, *, fresh=0):
        self.asked.append((line, fresh))
        if fresh:
            passed = all([self.readings.pop(0) for _ in range(fresh)])
        elif line not in self.kept:
            passed = self.kept[line] = self.readings.pop(0)
        else:
            passed = self.kept[line]
        return Verdict(passed, problems=() if passed else ("the line says something the facts do not support.",))


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

    def test_a_wording_that_was_refused_and_comes_back_is_read_again_and_not_served_its_refusal(self):
        acts = (act(Kind.CONFIRM_STOP, thread="Add a clamp helper"),)
        line = "Should I stop Add a clamp helper?"
        decider = Remembers(False, True, True)
        speech = Speech(None, Reads(), decider, Lane(Writes(line)))
        refused = speech.render(acts)
        self.assertEqual((refused.line, refused.raw, refused.problems),
                         (None, line, ["the line says something the facts do not support."]))
        again = speech.render(acts, (), refused.problems, [refused.raw], 1)
        self.assertEqual(again.text, line, "the same words, read again, pass")
        self.assertEqual(decider.asked, [(line, 0), (line, 2)])
        stale = Remembers(False, False, False)
        self.assertIsNone(Speech(None, Reads(), stale, Lane(Writes(line))).render(acts).line)
        self.assertIsNone(Speech(None, Reads(), stale, Lane(Writes(line))).render(acts, (), (), [line], 1).line,
                          "the fresh readings are the only ones that can change the refusal, and here they refuse")
        self.assertEqual(stale.asked, [(line, 0), (line, 2)])

    def test_overturning_a_refusal_takes_two_new_readings_that_both_pass(self):
        acts = (act(Kind.CONFIRM_STOP, thread="Add a clamp helper"),)
        line = "Should I stop Add a clamp helper?"
        for second, third, said in ((True, True, True), (True, False, False), (False, True, False),
                                    (False, False, False)):
            with self.subTest(readings=(second, third)):
                speech = Speech(None, Reads(), Remembers(False, second, third), Lane(Writes(line)))
                self.assertIsNone(speech.render(acts).line)
                self.assertEqual(speech.render(acts, (), ["no"], [line], 1).line is not None, said)

    def test_a_refused_wording_is_read_again_once_whichever_route_says_it_and_however_many_drafts(self):
        acts = (act(Kind.CONFIRM_STOP, thread="Add a clamp helper"),)
        line = "Should I stop Add a clamp helper?"
        decider = Remembers(False, False, False)
        speech = Speech(Lane(Writes(line)), Reads(), decider, Lane(Writes(line)))
        first = speech.render(acts)
        self.assertEqual(decider.asked, [(line, 0), (line, 2)],
                         "the text model's identical wording is the writer's again: one fresh round, not two")
        second = speech.render(acts, (), first.problems, first.tried, 1, reread=first.read)
        self.assertIsNone(second.line)
        self.assertEqual(decider.asked, [(line, 0), (line, 2), (line, 0), (line, 0)],
                         "the second draft, from either route, is not read again: the refusal stands")
        self.assertEqual(decider.readings, [], "three real readings were all that were made")

    def test_a_new_wording_is_read_as_any_first_wording_is(self):
        acts = (act(Kind.CONFIRM_STOP, thread="Add a clamp helper"),)
        decider = Remembers(True)
        speech = Speech(None, Reads(), decider, Lane(Writes("Want me to stop Add a clamp helper?")))
        self.assertTrue(speech.render(acts, (), ["no"], ["Should I stop Add a clamp helper?"], 1).line)
        self.assertEqual(decider.asked, [("Want me to stop Add a clamp helper?", False)])

    def test_a_repeat_the_code_check_refuses_is_not_read_by_the_decider_at_all(self):
        acts = (act(Kind.CONFIRM_STOP, thread="Add a clamp helper"),)
        decider = Remembers()
        speech = Speech(None, Reads(), decider, Lane(Writes("I've stopped Add a clamp helper.")))
        rendered = speech.render(acts, (), ["not a question"], ["I've stopped Add a clamp helper."], 1)
        self.assertEqual((rendered.line, rendered.problems), (None, ["not a question"]))
        self.assertEqual(decider.asked, [])

    def test_the_wording_of_every_route_is_kept_as_refused(self):
        acts = (act(Kind.CONFIRM_STOP, thread="Add a clamp helper"),)
        speech = Speech(Lane(Writes("I stopped Add a clamp helper.")), Reads(), Remembers(), Lane(Writes("Stopped.")))
        rendered = speech.render(acts)
        self.assertEqual(rendered.tried, ("Stopped.", "I stopped Add a clamp helper."))
        self.assertEqual(rendered.raw, "Stopped.")

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

    def test_a_request_held_behind_another_is_shown_the_example_that_tells_it_apart_and_is_told_so(self):
        plain = reader_messages((act(Kind.CONFIRM_LAUNCH, project="Alpha"),))[0]["content"]
        other = reader_messages((act(Kind.CONFIRM_LAUNCH, project="Alpha", another=True,
                                     about="speeding up the loader"),))[0]["content"]
        bare = reader_messages((act(Kind.CONFIRM_LAUNCH, project="Alpha", another=True),))[0]["content"]
        self.assertNotIn("`another`", plain)
        self.assertIn("When `another` is given", other)
        self.assertIn("Should I also start a second new coding thread in Example Fleet Notes, for sorting the fleet "
                      "log by date?", other)
        self.assertIn("Should I also start a second new coding thread in Example Fleet Notes?", bare)
        self.assertNotIn("sorting the fleet log by date", bare, "an example's request is never offered where there is none")
        for kind, slots in ((Kind.CONFIRM_TELL, {"thread": "Alpha", "message": "add a test"}),
                            (Kind.CONFIRM_STOP, {"thread": "Alpha"}), (Kind.CONFIRM_THREAD, {"thread": "Alpha"})):
            with self.subTest(kind.value):
                shown = reader_messages((act(kind, **slots, another=True),))[0]["content"]
                self.assertTrue("also" in shown or "other thing you asked" in shown)

    def test_the_writer_is_shown_a_thread_example_for_a_thread_question_and_a_project_one_otherwise(self):
        which = lambda of: reader_messages((act(Kind.ASK_WHICH, of=of, candidates=["Alpha", "Beta"]),))[0]["content"]
        self.assertIn("Which thread do you mean", which("thread"))
        self.assertNotIn("Which project do you mean", which("thread"))
        self.assertIn("Which project do you mean", which("project"))

    def test_an_approval_asked_by_its_kind_is_shown_the_example_that_names_the_thread_as_the_act_does(self):
        shown = lambda **slots: reader_messages((act(Kind.CONFIRM_RESPOND, decision="approve", need="command_approval",
                                                      **slots),))[0]["content"]
        self.assertIn("Notes for sorting the fleet log by date, which is waiting for your approval to run a command?",
                      shown(begun_in="Example Fleet Notes", begun_for="an account"))
        self.assertIn("Example Fleet Notes, which is waiting for your approval to run a command?",
                      shown(begun_in="Example Fleet Notes"))
        self.assertIn("this thread's request to run a command", shown(on_call=True))
        self.assertIn("Example Archive Old Invoices running a command?", shown(thread="Add a clamp helper"))
        for decision, said in (("approve", "I approved the command for the thread you started in Example Fleet Notes for sorting"),
                               ("deny", "I said no to the command for the thread you started in Example Fleet Notes for sorting")):
            answered = reader_messages((act(Kind.RESPONDED, decision=decision, need="command_approval",
                                            begun_in="Example Fleet Notes", begun_for="an account"),))[0]["content"]
            self.assertIn(said, answered)
        tool = reader_messages((act(Kind.CONFIRM_RESPOND, thread="T", decision="approve", need="permission",
                                    tool="x"),))[0]["content"]
        self.assertIn("use the ListPages tool", tool)

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
