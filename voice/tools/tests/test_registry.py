import unittest
import urllib.error
from datetime import date

from voice.tools import open_meteo, registry
from voice.tools.mcp import McpError
from voice.tools.registry import Outcome, ToolCall
from voice.tools.tests.replay import RECORDED, replay_registry


def recorded_days(place_name):
    place = open_meteo.parse_place(RECORDED["http"][open_meteo.geocode_url(place_name)])
    return open_meteo.parse_days(RECORDED["http"][open_meteo.forecast_url(place)])


def spoken(day):
    return f"{day.date:%A}, {day.date:%B} {day.date.day}: a high of {round(day.high_f)} degrees Fahrenheit"


class RouteTest(unittest.TestCase):
    def setUp(self):
        self.registry, _, _ = replay_registry()

    def test_the_clip_requests_route_to_their_tool_and_arguments(self):
        cases = {
            "What's the weather in Denver tomorrow?": ToolCall("weather", {"place": "Denver", "when": "tomorrow"}),
            "What's the weather going to be in Denver this weekend,": ToolCall("weather", {"place": "Denver",
                                                                                          "when": "weekend"}),
            "I wonder if it's going to rain on Saturday.": ToolCall("weather", {"place": "Denver", "when": "saturday"}),
            "and what time is it in Tokyo right now?": ToolCall("time", {"place": "Tokyo"}),
            "What's the forecast for New York?": ToolCall("weather", {"place": "New York", "when": "today"}),
        }
        for instruction, expected in cases.items():
            with self.subTest(instruction):
                self.assertEqual(self.registry.route(instruction), expected)

    def test_requests_no_tool_covers_stay_unrouted(self):
        for instruction in ("Set a timer for 10 minutes.", "Find last week's sales report",
                            "and how long is the drive to Boulder?", "Check my calendar for Friday",
                            "Look up flight prices to Chicago"):
            with self.subTest(instruction):
                self.assertIsNone(self.registry.route(instruction))


class FollowUpRouteTest(unittest.TestCase):
    """A follow-up continues the earlier lookup only when no tool claims it and it states a value of its own."""

    EARLIER = ToolCall("weather", {"place": "Denver", "when": "tomorrow"})

    def setUp(self):
        self.registry, _, _ = replay_registry(None)

    def route(self, instruction, earlier=EARLIER):
        return self.registry.route(instruction, earlier)

    def test_a_new_day_keeps_the_place(self):
        self.assertEqual(self.route("And what about Saturday?"),
                         ToolCall("weather", {"place": "Denver", "when": "saturday"}))

    def test_a_new_place_keeps_the_day(self):
        self.assertEqual(self.route("What about in Boulder?"),
                         ToolCall("weather", {"place": "Boulder", "when": "tomorrow"}))

    def test_a_named_subject_wins(self):
        self.assertEqual(self.route("What time is it in Tokyo?"), ToolCall("time", {"place": "Tokyo"}))
        self.assertEqual(self.route("What's the weather on Saturday?"),
                         ToolCall("weather", {"place": "Denver", "when": "saturday"}),
                         "a weather request with no place still takes the earlier place")

    def test_nothing_to_continue(self):
        self.assertIsNone(self.route("And set a timer for ten minutes."), "states no day and no place")
        self.assertIsNone(self.route("And what about Saturday?", earlier=None), "no earlier lookup, no place")


class RunTest(unittest.TestCase):
    def run_one(self, instruction, **kw):
        tools, fetch, mcp = replay_registry(**kw)
        return tools.run(tools.route(instruction)), fetch, mcp

    def test_a_weekend_forecast_states_saturday_and_sunday_from_the_recorded_response(self):
        result, fetch, _ = self.run_one("What's the weather going to be in Denver this weekend,")
        weekend = [d for d in recorded_days("Denver") if d.date.weekday() >= 5][:2]
        self.assertEqual(result.outcome, Outcome.OK)
        self.assertEqual([d.date.weekday() for d in weekend], [5, 6])
        for day in weekend:
            self.assertIn(spoken(day), result.summary)
            self.assertIn(f"a {day.precipitation_pct} percent chance of precipitation", result.summary)
        self.assertTrue(result.summary.startswith("Forecast for Denver, Colorado."))
        self.assertEqual(len(fetch.urls), 2)

    def test_tomorrow_is_the_day_after_the_places_today(self):
        result, _, _ = self.run_one("What's the weather in Denver tomorrow?")
        today, tomorrow = recorded_days("Denver")[:2]
        self.assertEqual((tomorrow.date - today.date).days, 1)
        self.assertIn(spoken(tomorrow), result.summary)
        self.assertNotIn(spoken(today), result.summary)

    def test_the_time_comes_from_the_mcp_server_for_the_geocoded_time_zone(self):
        result, _, mcp = self.run_one("and what time is it in Tokyo right now?")
        self.assertEqual(mcp.calls, [("get_current_time", {"timezone": "Asia/Tokyo"})])
        self.assertEqual(result.outcome, Outcome.OK)
        recorded = RECORDED["mcp"]['get_current_time {"timezone": "Asia/Tokyo"}']
        self.assertEqual(result.detail["mcp_result"], recorded)
        hour, minute = recorded["datetime"][11:13], recorded["datetime"][14:16]
        clock = f"{int(hour) % 12 or 12}:{minute} {'PM' if int(hour) >= 12 else 'AM'}"
        self.assertEqual(result.summary.split(" on ")[0], f"In Tokyo, Japan it is {clock}")

    def test_a_place_geocoding_cannot_find_fails_with_a_spoken_reason(self):
        result, _, _ = self.run_one("What's the weather in Atlantis Nowhere?")
        self.assertEqual((result.outcome, result.summary),
                         (Outcome.FAILED, "The weather lookup found no place named Atlantis Nowhere."))

    def test_no_place_and_no_default_fails_without_a_request(self):
        result, fetch, _ = self.run_one("Is it going to rain on Saturday?", default_place=None)
        self.assertEqual((result.outcome, result.summary),
                         (Outcome.FAILED, "The weather lookup needs a place, and none was given."))
        self.assertEqual(fetch.urls, [])


def failing_registry(exc):
    def fetch(url, timeout_s):
        raise exc

    class Mcp:
        def call_tool(self, name, arguments, timeout_s):
            raise exc

    return registry.build(fetch, Mcp(), "Denver", timeout_s=2.0)


class FailureTest(unittest.TestCase):
    def test_timeouts_report_timeout_and_other_errors_report_failed(self):
        cases = [
            (TimeoutError("read timed out"), Outcome.TIMEOUT, "The weather lookup timed out after 2 seconds."),
            (urllib.error.URLError(TimeoutError("connect")), Outcome.TIMEOUT,
             "The weather lookup timed out after 2 seconds."),
            (urllib.error.URLError("no route to host"), Outcome.FAILED,
             "The weather lookup failed, so there is no result."),
            (ValueError("bad json"), Outcome.FAILED, "The weather lookup failed, so there is no result."),
        ]
        for exc, outcome, summary in cases:
            with self.subTest(repr(exc)):
                tools = failing_registry(exc)
                result = tools.run(tools.route("What's the weather in Denver?"))
                self.assertEqual((result.outcome, result.summary), (outcome, summary))
                self.assertEqual(result.detail, {"error": repr(exc)})

    def test_an_mcp_tool_error_fails_the_time_lookup(self):
        tools, _, _ = replay_registry()
        tools.tools["time"] = registry.Tool("time", "time lookup", "the current time in a city",
                                            tools.tools["time"].parse,
                                            failing_registry(McpError("Invalid timezone")).tools["time"].call)
        result = tools.run(tools.route("What time is it in Tokyo?"))
        self.assertEqual((result.outcome, result.summary), (Outcome.FAILED, "The time lookup failed, so there is no result."))

    def test_the_record_is_plain_json(self):
        tools, _, _ = replay_registry()
        record = tools.run(tools.route("What time is it in Tokyo?")).record()
        self.assertEqual((record["tool"], record["outcome"], record["arguments"]), ("time", "ok", {"place": "Tokyo"}))
        self.assertIsInstance(record["latency_s"], float)


class PickDaysTest(unittest.TestCase):
    def test_the_weekend_on_a_sunday_is_that_sunday_alone(self):
        from voice.tools.lookups import pick_days
        days = [open_meteo.Day(date(2026, 11, 22 + i), 50.0, 0) for i in range(7)]
        self.assertEqual([d.date for d in pick_days("weekend", days)], [date(2026, 11, 22)])


class DispatchTest(unittest.TestCase):
    """The decider's tool answer decides a dispatch when it clears the bar, and the rules only cover what it is unsure
    about. The measured case: after the San Diego forecast, "Pick a city on the east coast to check the weather for."
    has the weather word in it, so the rules use the earlier task's place and forecast San Diego again. The real call
    ran with no general worker configured, which is the default."""

    EAST_COAST = "Pick a city on the east coast to check the weather for."
    SAN_DIEGO = ToolCall("weather", {"place": "San Diego", "when": "today"})

    def setUp(self):
        self.rules, _, _ = replay_registry()
        self.worker, _, _ = replay_registry("Boston", chat=lambda messages, timeout_s: "{}")

    def test_the_word_rules_re_run_the_earlier_place_on_the_east_coast_request(self):
        self.assertEqual(self.rules.dispatch(self.EAST_COAST, self.SAN_DIEGO).record()["tool"], "weather")

    def test_a_confident_choice_overrides_the_rules_and_says_so(self):
        routed = self.worker.dispatch(self.EAST_COAST, self.SAN_DIEGO, choice=registry.ToolChoice("agent", 0.62))
        self.assertEqual(routed.call, ToolCall("agent", {"instruction": self.EAST_COAST, "context": []}))
        self.assertEqual(routed.record()["via"], "decider")
        self.assertFalse(routed.record()["disagreement"])

    def test_a_choice_under_the_bar_falls_back_to_the_rules_and_logs_the_disagreement(self):
        routed = self.worker.dispatch(self.EAST_COAST, self.SAN_DIEGO, choice=registry.ToolChoice("agent", 0.39))
        self.assertEqual(routed.call, ToolCall("agent", {"instruction": self.EAST_COAST, "context": []}),
                         "with a worker configured the rules also ask it, and the choice names the same tool")
        unsure = self.rules.dispatch(self.EAST_COAST, self.SAN_DIEGO, choice=registry.ToolChoice("agent", 0.20))
        self.assertEqual(unsure.call, ToolCall("weather", {"place": "San Diego", "when": "today"}))
        self.assertEqual(unsure.record()["via"], "rules")
        self.assertTrue(unsure.record()["disagreement"])
        self.assertEqual(unsure.record()["choice"], {"tool": "agent", "confidence": 0.2, "arguments": None})

    def test_a_confident_none_runs_nothing(self):
        routed = self.rules.dispatch("Nice weather today, huh?", choice=registry.ToolChoice("none", 0.9))
        self.assertIsNone(routed.call)
        self.assertEqual(routed.record()["via"], "decider")
        self.assertEqual(self.rules.route("Nice weather today, huh?").tool, "weather",
                         "the rules would have run a forecast for small talk")

    def test_a_confident_choice_of_a_tool_takes_that_tool_not_the_one_the_rules_pick(self):
        routed = self.worker.dispatch("What's the weather in Denver tomorrow?", choice=registry.ToolChoice("time", 0.5))
        self.assertEqual(routed.call, ToolCall("time", {"place": "Denver"}),
                         "the worker's default place is Boston, so Denver is read from the instruction")

    def test_a_read_place_and_day_replace_the_parsers_reading(self):
        routed = self.rules.dispatch("And Saturday?", self.SAN_DIEGO,
                                     choice=registry.ToolChoice("weather", 0.5,
                                                                {"place": "Boston", "when": "saturday"}))
        self.assertEqual(routed.call, ToolCall("weather", {"place": "Boston", "when": "saturday"}))

    def test_a_reading_that_names_no_place_keeps_the_earlier_one(self):
        routed = self.rules.dispatch("And Sunday?", self.SAN_DIEGO,
                                     choice=registry.ToolChoice("weather", 0.5, {"place": None, "when": "sunday"}))
        self.assertEqual(routed.call, ToolCall("weather", {"place": "San Diego", "when": "sunday"}))

    def test_a_choice_does_not_carry_the_place_into_a_request_that_names_nothing(self):
        routed = self.worker.dispatch("Wait, do it again.", self.SAN_DIEGO,
                                      choice=registry.ToolChoice("weather", 0.59, {"place": None, "when": None}))
        self.assertEqual(routed.call, ToolCall("weather", {"place": "Boston", "when": "today"}),
                         "nothing there continues the earlier forecast, so the worker's default place stands")


class CorrectedTest(unittest.TestCase):
    """A correction of an earlier lookup runs that lookup's own tool on the corrected words. The tool is the one the
    corrected task ran, not the one the amended words happen to route to, and the argument the correction changes
    comes from the amended words while the rest stays."""

    WEATHER = ToolCall("weather", {"place": "Boulder", "when": "tomorrow"})
    CALENDAR = ToolCall("calendar", {"when": "Thursday"})

    def setUp(self):
        self.rules, _, _ = replay_registry()

    def test_the_corrected_place_replaces_the_rejected_one(self):
        self.assertEqual(self.rules.corrected(self.WEATHER, "What's the weather in Portland tomorrow?"),
                         ToolCall("weather", {"place": "Portland", "when": "tomorrow"}))

    def test_the_arguments_the_correction_leaves_alone_are_the_earlier_calls(self):
        self.assertEqual(self.rules.corrected(self.WEATHER, "And what about Sunday?"),
                         ToolCall("weather", {"place": "Boulder", "when": "sunday"}))

    def test_a_tool_the_registry_does_not_hold_goes_to_the_general_worker(self):
        self.assertEqual(self.rules.corrected(self.CALENDAR, "Look up my calendar for Friday afternoon."),
                         ToolCall("agent", {"instruction": "Look up my calendar for Friday afternoon.", "context": []}))

    def test_a_general_worker_that_is_not_configured_fails_rather_than_raises(self):
        call = self.rules.corrected(self.CALENDAR, "Look up my calendar for Friday afternoon.")
        self.assertEqual(self.rules.run(call).outcome, Outcome.FAILED)


class ChoicesTest(unittest.TestCase):
    def test_the_options_are_every_registered_tool_the_worker_and_none(self):
        tools, _, _ = replay_registry(chat=lambda messages, timeout_s: "{}")
        self.assertEqual(list(tools.choices()), ["weather", "time", "agent", "none"])
        for name, line in tools.choices().items():
            with self.subTest(name):
                self.assertTrue(line and line[0].isupper(), f"{name} needs one sentence, what it does and when")


if __name__ == "__main__":
    unittest.main()
