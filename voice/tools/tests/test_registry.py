import unittest
import urllib.error
from datetime import date, timedelta

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

    def test_a_relative_day_is_the_day_after_the_earlier_lookups(self):
        self.assertEqual(self.route("And what about the day after?"),
                         ToolCall("weather", {"place": "Denver", "when": "day after tomorrow"}),
                         "the day is relative to the earlier lookup's tomorrow")
        self.assertEqual(self.route("And the next day?", ToolCall("weather", {"place": "Denver", "when": "saturday"})),
                         ToolCall("weather", {"place": "Denver", "when": "sunday"}))

    def test_a_day_the_asr_split_in_two_still_states_the_day(self):
        self.assertEqual(self.route("And s aturday?"),
                         ToolCall("weather", {"place": "Denver", "when": "saturday"}),
                         "the split day carries the earlier place, which no parser could read before")

    def test_a_follow_up_that_names_nothing_continues_nothing(self):
        self.assertIsNone(self.route("Wait, do it again."))


class DayTableTest(unittest.TestCase):
    """The day table reads a weekday, today, tomorrow, a day named relative to an earlier lookup's, and one word the
    ASR split in two."""

    def test_the_table_reads_a_relative_day_and_a_split_one(self):
        from voice.tools.lookups import DAY_AFTER, resolved, stated_when
        cases = {"the day after tomorrow": "day after tomorrow", "And s aturday?": "saturday",
                 "the next day": DAY_AFTER, "what about sat urday": "saturday", "tomorrow morning": "tomorrow",
                 "and to morrow?": "tomorrow", "nothing here names a day": None}
        for words, when in cases.items():
            with self.subTest(words):
                self.assertEqual(stated_when(words), when)
        self.assertEqual(resolved(DAY_AFTER, "tomorrow"), "day after tomorrow")
        self.assertEqual(resolved(DAY_AFTER, "saturday"), "sunday")
        self.assertEqual(resolved(DAY_AFTER), "tomorrow", "with no earlier lookup it counts from today")
        self.assertEqual(resolved("saturday", "monday"), "saturday", "a day of its own stands as it is")

    def test_the_day_after_tomorrow_is_the_places_third_day(self):
        from voice.tools.lookups import pick_days
        days = [open_meteo.Day(date(2026, 11, 16 + i), 50.0, 0) for i in range(4)]
        self.assertEqual([d.date.day for d in pick_days("day after tomorrow", days)], [18])

    def test_the_follow_up_runs_the_forecast_for_that_day(self):
        tools, _, _ = replay_registry(None)
        earlier = ToolCall("weather", {"place": "Denver", "when": "tomorrow"})
        result = tools.run(tools.route("And what about the day after?", earlier))
        third = recorded_days("Denver")[2]
        self.assertEqual(result.outcome, Outcome.OK)
        self.assertIn(spoken(third), result.summary)


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

    def test_a_read_place_and_day_the_words_say_replace_the_parsers_reading(self):
        routed = self.rules.dispatch("And what about Boston on Saturday?", self.SAN_DIEGO,
                                     choice=registry.ToolChoice("weather", 0.5,
                                                                {"place": "Boston", "when": "saturday"}))
        self.assertEqual(routed.call, ToolCall("weather", {"place": "Boston", "when": "saturday"}),
                         "'what about Boston?' has no preposition, so only the reading finds the place")

    def test_a_reading_that_names_no_place_keeps_the_earlier_one(self):
        routed = self.rules.dispatch("And Sunday?", self.SAN_DIEGO,
                                     choice=registry.ToolChoice("weather", 0.5, {"place": None, "when": "sunday"}))
        self.assertEqual(routed.call, ToolCall("weather", {"place": "San Diego", "when": "sunday"}))

    def test_a_place_the_words_do_not_say_is_not_taken_from_the_reading(self):
        routed = self.rules.dispatch("And Saturday?", self.SAN_DIEGO,
                                     choice=registry.ToolChoice("weather", 0.5,
                                                                {"place": "Boston", "when": "saturday"}))
        self.assertEqual(routed.call, ToolCall("weather", {"place": "San Diego", "when": "saturday"}),
                         "the words name no place, so the follow-up keeps the earlier lookup's")

    def test_a_choice_does_not_carry_the_place_into_a_request_that_names_nothing(self):
        routed = self.worker.dispatch("Wait, do it again.", self.SAN_DIEGO,
                                      choice=registry.ToolChoice("weather", 0.59, {"place": None, "when": None}))
        self.assertEqual(routed.call, ToolCall("weather", {"place": "Boston", "when": "today"}),
                         "nothing there continues the earlier forecast, so the worker's default place stands")


class CountedDayTest(unittest.TestCase):
    """A day counted from today is a whole number of days in `when`, resolved against the forecast's own first day."""

    DENVER = ToolCall("weather", {"place": "Denver", "when": "tomorrow"})

    def setUp(self):
        self.tools, _, _ = replay_registry(None)

    def test_a_number_of_days_picks_that_many_days_after_the_first(self):
        from voice.tools.lookups import pick_days
        days = [open_meteo.Day(date(2026, 11, 23) + timedelta(days=i), 50.0, 0) for i in range(16)]
        self.assertEqual([d.date for d in pick_days(7, days)], [date(2026, 11, 30)])
        self.assertEqual(pick_days(16, days), [], "a day past the forecast's last is no day")

    def test_the_reading_of_a_counted_day_outranks_the_word_table_and_keeps_the_earlier_place(self):
        routed = self.tools.dispatch("What about a week from today?", self.DENVER,
                                     choice=registry.ToolChoice("weather", 0.9, {"place": None, "when": 7}))
        self.assertEqual(routed.call, ToolCall("weather", {"place": "Denver", "when": 7}),
                         "the table reads today in 'a week from today'; the decider's count is the day")

    def test_a_follow_up_that_changes_only_the_place_keeps_the_earlier_counted_day(self):
        earlier = ToolCall("weather", {"place": "Denver", "when": 7})
        routed = self.tools.dispatch("What about Boston?", earlier,
                                     choice=registry.ToolChoice("weather", 0.9, {"place": "Boston", "when": None}))
        self.assertEqual(routed.call, ToolCall("weather", {"place": "Boston", "when": 7}),
                         "'what about Boston?' has no preposition, so only the reading finds the place")

    def test_the_day_after_a_counted_day_is_the_next_number(self):
        from voice.tools.lookups import DAY_AFTER, resolved
        self.assertEqual(resolved(DAY_AFTER, 7), 8)

    def test_a_forecast_for_a_counted_day_names_that_day(self):
        result = self.tools.run(ToolCall("weather", {"place": "Denver", "when": 3}))
        self.assertEqual(result.outcome, Outcome.OK)
        self.assertIn(spoken(recorded_days("Denver")[3]), result.summary)

    def test_a_day_past_the_forecast_says_how_far_ahead_it_can_look(self):
        result = self.tools.run(ToolCall("weather", {"place": "Denver", "when": 10}))
        self.assertEqual((result.outcome, result.summary),
                         (Outcome.FAILED, "The weather lookup can look ahead 6 days, so it has no forecast that far "
                                          "ahead."))
        far = self.tools.run(ToolCall("weather", {"place": "Denver", "when": "farther"}))
        self.assertEqual((far.outcome, far.summary), (result.outcome, result.summary))

    def test_the_forecast_asks_for_the_longest_range_the_source_gives(self):
        self.assertIn(f"forecast_days={open_meteo.FORECAST_DAYS}", open_meteo.forecast_url(
            open_meteo.parse_place(RECORDED["http"][open_meteo.geocode_url("Denver")])))


class RecordedReadingTest(unittest.TestCase):
    """The decider's own answers for the two follow-ups the polish round lost and for turn 016's repeat.

    The readings are measured: t52's `weather` 0.87 with no place and `tomorrow` for the day, and t67's 0.86 with no
    place and `saturday` for the split day, are the live gate eval's polish sample (`calls-polish`, three readings
    each). Turn 016's 0.53 with no place and `today` is the toolpick-polish2 replay's."""

    DENVER = ToolCall("weather", {"place": "Denver", "when": "tomorrow"})

    def setUp(self):
        self.tools, _, _ = replay_registry(None)

    def test_t52_the_day_after_keeps_the_place_and_moves_the_day(self):
        routed = self.tools.dispatch("And what about the day after?", self.DENVER,
                                     choice=registry.ToolChoice("weather", 0.87, {"place": None, "when": "tomorrow"}))
        self.assertEqual(routed.call, ToolCall("weather", {"place": "Denver", "when": "day after tomorrow"}),
                         "the reading said tomorrow, which the words do not say; the words say the day after")

    def test_t67_the_split_day_keeps_the_place(self):
        routed = self.tools.dispatch("And s aturday?", self.DENVER,
                                     choice=registry.ToolChoice("weather", 0.86, {"place": None, "when": "saturday"}))
        self.assertEqual(routed.call, ToolCall("weather", {"place": "Denver", "when": "saturday"}))

    def test_turn_016_does_not_repeat_the_earlier_forecast(self):
        durham = ToolCall("weather", {"place": "Durham", "when": "today"})
        for reading in ({"place": None, "when": "today"}, {"place": "Durham", "when": "today"}):
            with self.subTest(reading):
                routed = self.tools.dispatch("Wait, do it again.", durham,
                                             choice=registry.ToolChoice("weather", 0.53, reading))
                self.assertEqual(routed.call, ToolCall("weather", {"place": None, "when": "today"}),
                                 "the words name no place: no earlier forecast is repeated and no place the model "
                                 "invented is taken")
                waiting = self.tools.waiting("weather", "Wait, do it again.", durham, reading)
                self.assertEqual(waiting["missing"], ("place",), "so the turn asks which city instead of failing")


class WaitingTest(unittest.TestCase):
    """A call that cannot answer asks the person for what it lacks, and their answer completes it."""

    DENVER = ToolCall("weather", {"place": "Denver", "when": "tomorrow"})

    def setUp(self):
        self.tools, self.fetch, _ = replay_registry(None)

    def test_a_forecast_with_no_place_waits_for_a_city(self):
        waiting = self.tools.waiting("weather", "What's the weather like?", None, {"place": None, "when": None})
        self.assertEqual((waiting["tool"], waiting["missing"]), ("weather", ("place",)))
        self.assertEqual(waiting["arguments"], {"place": None, "when": "today"},
                         "the call it would run, so the answer completes it rather than starting over")
        self.assertIsNone(self.tools.waiting("weather", "What's the weather in Denver?", None,
                                             {"place": None, "when": None}), "a place the words state is enough")

    def test_the_earlier_lookups_place_or_the_default_one_is_enough(self):
        self.assertIsNone(self.tools.waiting("weather", "And Saturday?", self.DENVER))
        with_default, _, _ = replay_registry("Portland")
        self.assertIsNone(with_default.waiting("weather", "What's the weather like?"))

    def test_an_answer_that_names_a_city_completes_the_call(self):
        waiting = self.tools.waiting("weather", "What's the weather like?", None, {"place": None, "when": None})
        for answer, place in (("Seattle.", "Seattle"), ("In Boston", "Boston"), ("the city is Boston", "Boston")):
            with self.subTest(answer):
                self.assertEqual(self.tools.answered(waiting, answer),
                                 ToolCall("weather", {"place": place, "when": "today"}))

    def test_the_completed_call_is_the_lookup_that_was_asked_for(self):
        waiting = self.tools.waiting("weather", "What's the weather like?", None, {"place": None, "when": None})
        result = self.tools.run(self.tools.answered(waiting, "In Denver"))
        self.assertEqual(result.outcome, Outcome.OK)
        self.assertTrue(result.summary.startswith("Forecast for Denver, Colorado."))

    def test_another_tools_subject_is_not_the_answer(self):
        waiting = self.tools.waiting("weather", "What's the weather like?", None, {"place": None, "when": None})
        self.assertIsNone(self.tools.answered(waiting, "What's the time in Tokyo?"),
                          "a time lookup is a new request, not an answer to the question about a forecast")

    def test_words_that_name_no_place_answer_nothing(self):
        waiting = self.tools.waiting("weather", "What's the weather like?", None, {"place": None, "when": None})
        for answer in ("Never mind.", "Saturday", "what did you say?"):
            with self.subTest(answer):
                self.assertIsNone(self.tools.answered(waiting, answer))


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
