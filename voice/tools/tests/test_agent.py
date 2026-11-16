import json
import tempfile
import unittest
from concurrent.futures import wait
from datetime import date
from pathlib import Path
from unittest import mock

from voice.prototype.tasks import HUNG_LOOKUP_GRACE_S, MockTaskService
from voice.prototype.tests.test_tasks import ScriptedSegmenter
from voice.segmentation import client
from voice.tools import agent
from voice.tools.registry import Outcome, ToolCall
from voice.tools.tests.replay import replay_registry
from voice.tools.tests.test_registry import recorded_days

SAN_DIEGO = ToolCall("weather", {"place": "San Diego", "when": "today"})
RALEIGH = ToolCall("weather", {"place": "Raleigh", "when": "today"})
CONTEXT = [{"user": "What's the weather in San Diego today?",
            "results": ["Forecast for San Diego, California. Monday, November 16: a high of 68 degrees Fahrenheit."],
            "assistant": ["San Diego has a high of 68 today."]}]


class FakeChat:
    """Replies in order. A reply that is an exception is raised instead."""

    model = "fake/model:free"

    def __init__(self, *replies):
        self.replies, self.calls = list(replies), []

    def __call__(self, messages, timeout_s):
        self.calls.append((messages, timeout_s))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


class RouteTest(unittest.TestCase):
    """The six instructions from the recorded call, each after the lookup that preceded it there."""

    CASES = [
        ("check the weather in Raleigh today", SAN_DIEGO, ToolCall("weather", {"place": "Raleigh", "when": "today"})),
        ("What about in Durham?", RALEIGH, ToolCall("weather", {"place": "Durham", "when": "today"})),
        ("how is the weather in Seattle right now?", RALEIGH, ToolCall("weather", {"place": "Seattle", "when": "today"})),
        ("What time is it in Tokyo?", RALEIGH, ToolCall("time", {"place": "Tokyo"})),
    ]
    TO_AGENT = [
        ("tell me what, uhm, day George Washington was born on?", RALEIGH, None),
        ("Do I need to wear a jacket?", SAN_DIEGO, None),
        ("Pick a city on the east coast to check the weather for.", SAN_DIEGO, SAN_DIEGO),
        ("Are you checking the weather?", RALEIGH, RALEIGH),
    ]

    def test_requests_that_state_their_own_place_keep_the_fast_path_with_or_without_the_worker(self):
        for chat in (None, FakeChat()):
            tools, _, _ = replay_registry(chat=chat)
            for instruction, earlier, expected in self.CASES:
                with self.subTest(instruction, agent=chat is not None):
                    self.assertEqual(tools.route(instruction, earlier, CONTEXT), expected)

    def test_requests_the_parsers_would_guess_at_go_to_the_worker_with_the_context(self):
        tools, _, _ = replay_registry(chat=FakeChat())
        for instruction, earlier, _ in self.TO_AGENT:
            with self.subTest(instruction):
                self.assertEqual(tools.route(instruction, earlier, CONTEXT),
                                 ToolCall("agent", {"instruction": instruction, "context": CONTEXT}))

    def test_without_the_worker_routing_is_unchanged(self):
        tools, _, _ = replay_registry()
        for instruction, earlier, before in self.TO_AGENT:
            with self.subTest(instruction):
                self.assertEqual(tools.route(instruction, earlier, CONTEXT), before)

    def test_a_follow_up_that_states_a_day_or_names_a_subject_with_nothing_earlier_stays_fast(self):
        tools, _, _ = replay_registry(chat=FakeChat())
        self.assertEqual(tools.route("What's the weather on Saturday?", RALEIGH),
                         ToolCall("weather", {"place": "Raleigh", "when": "saturday"}))
        self.assertEqual(tools.route("And what about Saturday?", RALEIGH),
                         ToolCall("weather", {"place": "Raleigh", "when": "saturday"}))
        self.assertEqual(tools.route("What's the weather like?"), ToolCall("weather", {"place": "Denver", "when": "today"}),
                         "no earlier lookup, so the default place applies")

    def test_a_follow_up_to_a_worker_answer_goes_back_to_the_worker(self):
        tools, _, _ = replay_registry(chat=FakeChat())
        earlier = ToolCall("agent", {"instruction": "Pick a city on the east coast", "context": []})
        self.assertEqual(tools.route("And tomorrow?", earlier).tool, "agent")

    def test_ask_reaches_the_worker_only_when_one_is_configured(self):
        self.assertIsNone(replay_registry()[0].ask("When was George Washington born?"))
        self.assertEqual(replay_registry(chat=FakeChat())[0].ask("When was George Washington born?"),
                         ToolCall("agent", {"instruction": "When was George Washington born?", "context": []}))


def run(chat, instruction, context=None, **kw):
    tools, fetch, mcp = replay_registry(chat=chat, **kw)
    tools.agent.today = lambda: date(2026, 11, 16)
    return tools.run(tools.ask(instruction, context)), fetch


class AgentTest(unittest.TestCase):
    def tomorrow(self):
        return recorded_days("Denver")[1]

    def test_a_tool_backed_answer_runs_the_lookup_and_words_its_result(self):
        high = round(self.tomorrow().high_f)
        worded = f"Tomorrow in Denver the high is {high} degrees, so bring a light jacket."
        chat = FakeChat('{"tool": "weather", "place": "Denver", "when": "tomorrow"}', worded)
        result, fetch = run(chat, "Do I need a jacket tomorrow?")
        self.assertEqual((result.tool, result.outcome, result.summary), ("agent", Outcome.OK, worded))
        self.assertEqual(len(fetch.urls), 2)
        tool_summary = result.detail["tool_result"]["summary"]
        self.assertTrue(tool_summary.startswith("Forecast for Denver, Colorado."))
        self.assertIn(tool_summary, chat.calls[1][0][-1]["content"], "the second call is given the tool result")
        self.assertIn("Monday, November 16, 2026", chat.calls[0][0][0]["content"])
        self.assertIn("default place is Denver", chat.calls[0][0][0]["content"])

    def test_a_worded_answer_with_a_number_the_tool_did_not_give_is_replaced_by_the_tool_summary(self):
        chat = FakeChat('{"tool": "weather", "place": "Denver", "when": "tomorrow"}',
                        "Tomorrow in Denver the high is 999 degrees.")
        result, _ = run(chat, "Do I need a jacket tomorrow?")
        self.assertEqual(result.summary, result.detail["tool_result"]["summary"])
        self.assertEqual(result.detail["unsupported"], ["999"])

    def test_a_failed_wording_call_falls_back_to_the_tool_summary(self):
        chat = FakeChat('{"tool": "time", "place": "Tokyo"}', RuntimeError("status 500"))
        result, _ = run(chat, "What time is it where my sister lives?")
        self.assertEqual(result.outcome, Outcome.OK)
        self.assertTrue(result.summary.startswith("In Tokyo, Japan it is"))

    def test_a_direct_answer_is_one_model_call_in_plain_words_and_at_most_two_sentences(self):
        chat = FakeChat('```json\n{"answer": "**George Washington** was born on February 22, 1732. He was the '
                        'first president. He led the army."}\n```')
        result, fetch = run(chat, "what day was George Washington born on?", CONTEXT)
        self.assertEqual(result.summary, "George Washington was born on February 22, 1732. He was the first president.")
        self.assertEqual((len(chat.calls), fetch.urls), (1, []))
        self.assertIn("Person: What's the weather in San Diego today?", chat.calls[0][0][1]["content"])

    def test_a_place_left_out_takes_the_default(self):
        chat = FakeChat('{"tool": "weather", "when": "tomorrow"}', "Denver is mild tomorrow.")
        result, _ = run(chat, "What about tomorrow?")
        self.assertEqual(result.detail["tool_result"]["arguments"], {"place": "Denver", "when": "tomorrow"})

    def test_a_failed_tool_call_reports_the_tools_own_reason(self):
        chat = FakeChat('{"tool": "weather", "place": "Atlantis Nowhere", "when": "today"}')
        result, _ = run(chat, "weather in that lost city?")
        self.assertEqual((result.outcome, result.summary),
                         (Outcome.FAILED, "The weather lookup found no place named Atlantis Nowhere."))
        self.assertEqual(len(chat.calls), 1)

    def test_malformed_replies_and_refusals_fail_plainly(self):
        cases = [("Sure, here is the weather!", "The lookup got no usable answer."),
                 ('{"tool": "weather", "place": ', "The lookup got no usable answer."),
                 ('{"answer": "  "}', "The lookup got no usable answer."),
                 ('{"cannot": "needs your calendar"}', "The lookup can't answer that with the tools it has.")]
        for reply, summary in cases:
            with self.subTest(reply):
                result, fetch = run(FakeChat(reply), "Check my calendar for Friday")
                self.assertEqual((result.outcome, result.summary, fetch.urls), (Outcome.FAILED, summary, []))

    def test_a_model_failure_and_a_timeout_are_reported_without_raising(self):
        result, _ = run(FakeChat(RuntimeError("status 400")), "what day was George Washington born on?")
        self.assertEqual((result.outcome, result.summary), (Outcome.FAILED, "The lookup failed, so there is no result."))
        result, _ = run(FakeChat(TimeoutError("read")), "what day was George Washington born on?")
        self.assertEqual((result.outcome, result.summary), (Outcome.TIMEOUT, "The lookup timed out after 12 seconds."))

    def test_each_model_call_gets_what_is_left_of_the_workers_own_deadline(self):
        chat = FakeChat('{"answer": "February 22, 1732."}')
        run(chat, "what day was George Washington born on?")
        self.assertTrue(11.0 < chat.calls[0][1] <= agent.TIMEOUT_S)

    def test_only_the_last_six_turns_reach_the_model(self):
        context = [{"user": f"turn {n}"} for n in range(1, 9)]
        chat = FakeChat('{"answer": "Yes."}')
        run(chat, "anything else?", context)
        prompt = chat.calls[0][0][1]["content"]
        self.assertNotIn("turn 2\n", prompt)
        self.assertIn("Person: turn 3", prompt)
        self.assertIn("Person: turn 8", prompt)


class Response:
    def __init__(self, status, body):
        self.status, self.body = status, body


class OpenRouterChatTest(unittest.TestCase):
    def setUp(self):
        folder = Path(tempfile.mkdtemp())
        self.stop = folder / "BILLING_STOP"
        self.ledger = folder / "calls.jsonl"
        patch = mock.patch.object(client, "STOP", self.stop)
        patch.start()
        self.addCleanup(patch.stop)
        sleep = mock.patch.object(agent.time, "sleep")
        self.sleeps = sleep.start()
        self.addCleanup(sleep.stop)

    def chat(self, *responses, model="fake/model:free"):
        chat = agent.OpenRouterChat("not-a-real-key", model=model, ledger=self.ledger)
        replies = list(responses)
        chat._send = mock.Mock(side_effect=lambda payload, timeout_s: replies.pop(0))
        return chat

    def test_a_model_that_is_not_free_is_refused_before_any_request(self):
        chat = self.chat(model="fake/paid-model")
        with self.assertRaises(AssertionError):
            chat([{"role": "user", "content": "hi"}], 5.0)
        chat._send.assert_not_called()

    def test_a_429_backs_off_and_retries(self):
        ok = {"status": 200, "ms": 5.0, "body": {"choices": [{"message": {"content": "hello"}}]}}
        chat = self.chat({"status": 429, "ms": 5.0, "body": {"error": {"message": "slow down"}}}, ok)
        self.assertEqual(chat([{"role": "user", "content": "hi"}], 5.0), "hello")
        self.assertEqual(self.sleeps.call_args.args[0], 2.0)
        entries = [json.loads(line) for line in self.ledger.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([e["status"] for e in entries], [429, 200])
        self.assertNotIn("not-a-real-key", self.ledger.read_text(encoding="utf-8"))

    def test_a_402_or_billing_wording_stops_every_later_call(self):
        for response in ({"status": 402, "ms": 1.0, "body": {}},
                         {"status": 403, "ms": 1.0, "body": {"error": {"message": "Insufficient credits"}}}):
            with self.subTest(response["status"]):
                self.stop.unlink(missing_ok=True)
                chat = self.chat(response)
                with self.assertRaises(client.BillingStop):
                    chat([{"role": "user", "content": "hi"}], 5.0)
                self.assertTrue(self.stop.exists())
                with self.assertRaises(client.BillingStop):
                    chat([{"role": "user", "content": "hi"}], 5.0)
                self.assertEqual(chat._send.call_count, 1)

    def test_a_billing_stop_inside_a_lookup_is_a_failed_result_not_a_crash(self):
        self.stop.write_text("{}", encoding="utf-8")
        chat = self.chat()
        result, _ = run(chat, "what day was George Washington born on?")
        self.assertEqual(result.outcome, Outcome.FAILED)
        self.assertIn("BillingStop", result.detail["error"])
        chat._send.assert_not_called()


class ServiceTest(unittest.TestCase):
    def test_a_task_no_parser_claims_runs_on_the_worker_with_the_workers_own_due_time(self):
        tools, _, _ = replay_registry(chat=FakeChat('{"answer": "Yes, it will be cool, so bring a jacket."}'))
        segmenter = ScriptedSegmenter()
        service = MockTaskService(segmenter, "conv_test", tools=tools, simulate=False, earlier=SAN_DIEGO,
                                  context=CONTEXT)
        self.addCleanup(service.close)
        segmenter.step(1, "Do I need to wear a jacket?", final=True, s1={})
        service.sync(now=0.0)
        execution = service.executions["task_1"]
        self.assertEqual(execution.tool["tool"], "agent")
        self.assertEqual(execution.due_at, agent.TIMEOUT_S + HUNG_LOOKUP_GRACE_S)
        wait(service.pending_calls(), timeout=5)
        service.poll(now=0.5)
        self.assertEqual((execution.state, execution.result_summary),
                         ("completed", "Yes, it will be cool, so bring a jacket."))


if __name__ == "__main__":
    unittest.main()
