import unittest
import urllib.error
from datetime import date, timedelta

from voice.tools import lookups, open_meteo, registry
from voice.tools.lookups import FARTHER, pick_days, wanted_dates
from voice.tools.mcp import McpError
from voice.tools.registry import Outcome, ToolCall, ToolChoice
from voice.tools.tests.replay import RECORDED, RECORDED_TODAY, replay_registry

MONDAY = date(2026, 11, 23)


def recorded_days(place_name):
    place = open_meteo.parse_place(RECORDED["http"][open_meteo.geocode_url(place_name)])
    return open_meteo.parse_days(RECORDED["http"][open_meteo.forecast_url(place)])


def spoken(day):
    return f"{day.date:%A}, {day.date:%B} {day.date.day}: a high of {round(day.high_f)} degrees Fahrenheit"


def days_from(first, count=16):
    return [open_meteo.Day(first + timedelta(days=i), 50.0 + i, i) for i in range(count)]


def made_up_fetch(first, count=16):
    """A made-up place whose own forecast starts on `first`, whatever date the caller is on."""
    def fetch(url, timeout_s):
        if "geocoding" in url:
            return {"results": [{"name": "Nowhereville", "admin1": "Madeup", "country": "Land", "latitude": 1.0,
                                 "longitude": 2.0, "timezone": "UTC"}]}
        dates = [first + timedelta(days=i) for i in range(count)]
        return {"daily": {"time": [d.isoformat() for d in dates], "temperature_2m_max": [50.0 + i for i in range(count)],
                          "precipitation_probability_max": list(range(count))}}
    return fetch


def weather(place="Denver", when="today"):
    return ToolCall("weather", {"place": place, "when": when})


class WantedDatesTest(unittest.TestCase):
    """Every relative day is a calendar date counted from the caller's today, before any forecast is looked at."""

    def test_each_typed_day_is_the_dates_it_names(self):
        cases = [("today", MONDAY, [MONDAY]), ("tomorrow", MONDAY, [date(2026, 11, 24)]),
                 (2, MONDAY, [date(2026, 11, 25)]), (7, MONDAY, [date(2026, 11, 30)]),
                 ("friday", MONDAY, [date(2026, 11, 27)]), ("monday", MONDAY, [MONDAY]),
                 ("sunday", MONDAY, [date(2026, 11, 29)]),
                 ("weekend", MONDAY, [date(2026, 11, 28), date(2026, 11, 29)]),
                 ("weekend", date(2026, 11, 28), [date(2026, 11, 28), date(2026, 11, 29)]),
                 ("weekend", date(2026, 11, 29), [date(2026, 11, 29)]),
                 (FARTHER, MONDAY, None), ("someday", MONDAY, None), (-1, MONDAY, None), (10 ** 9, MONDAY, None)]
        for when, today, dates in cases:
            with self.subTest(when=when, today=today):
                self.assertEqual(wanted_dates(when, today), dates)


class PickDaysTest(unittest.TestCase):
    """The forecast's days are selected by date, so a place whose own date is not the caller's answers for the
    caller's day, and a date the forecast does not hold is out of range."""

    def test_a_place_a_day_ahead_of_the_caller_is_picked_by_date_not_by_index(self):
        days = days_from(date(2026, 11, 24))
        self.assertEqual([d.date for d in pick_days("tomorrow", days, MONDAY)], [date(2026, 11, 24)])
        self.assertEqual([d.date for d in pick_days(7, days, MONDAY)], [date(2026, 11, 30)],
                         "a week from the caller's Monday, not eight days after it")
        self.assertEqual(pick_days("today", days, MONDAY), [], "the caller's Monday is before this forecast starts")

    def test_a_place_a_day_behind_the_caller_is_picked_by_date_not_by_index(self):
        days = days_from(date(2026, 11, 22))
        self.assertEqual([d.date for d in pick_days("today", days, MONDAY)], [MONDAY])
        self.assertEqual([d.date for d in pick_days(7, days, MONDAY)], [date(2026, 11, 30)])
        self.assertEqual([d.date for d in pick_days("weekend", days, MONDAY)],
                         [date(2026, 11, 28), date(2026, 11, 29)])
        self.assertEqual(pick_days(15, days, MONDAY), [], "the forecast ends on its fifteenth day, the caller's 14th")

    def test_a_date_past_the_forecast_is_out_of_range(self):
        days = days_from(MONDAY)
        self.assertEqual(len(pick_days(15, days, MONDAY)), 1)
        for when in (16, 400, FARTHER):
            with self.subTest(when):
                self.assertEqual(pick_days(when, days, MONDAY), [])


class RunTest(unittest.TestCase):
    def run_one(self, call, **kw):
        tools, fetch, mcp = replay_registry(**kw)
        return tools.run(call), fetch, mcp

    def test_a_weekend_forecast_states_saturday_and_sunday_from_the_recorded_response(self):
        result, _, _ = self.run_one(weather(when="weekend"))
        weekend = [d for d in recorded_days("Denver") if d.date.weekday() >= 5][:2]
        self.assertEqual(result.outcome, Outcome.OK)
        for day in weekend:
            self.assertIn(spoken(day), result.summary)
        self.assertTrue(result.summary.startswith("Forecast for Denver, Colorado."))

    def test_tomorrow_and_a_counted_day_are_the_dates_after_the_callers_today(self):
        by_date = {d.date: d for d in recorded_days("Denver")}
        for when, offset in (("today", 0), ("tomorrow", 1), (3, 3)):
            with self.subTest(when):
                result, _, _ = self.run_one(weather(when=when))
                self.assertIn(spoken(by_date[RECORDED_TODAY + timedelta(days=offset)]), result.summary)

    def test_a_forecast_for_a_place_a_day_ahead_answers_for_the_callers_date(self):
        tools = registry.build(made_up_fetch(date(2026, 11, 24)), None, None, today=lambda: MONDAY)
        result = tools.run(weather("Nowhereville", 7))
        self.assertEqual([d["date"] for d in result.detail["days"]], ["2026-11-30"])

    def test_a_forecast_for_a_place_a_day_behind_answers_for_the_callers_date(self):
        tools = registry.build(made_up_fetch(date(2026, 11, 22)), None, None, today=lambda: MONDAY)
        self.assertEqual([d["date"] for d in tools.run(weather("Nowhereville", "today")).detail["days"]],
                         ["2026-11-23"])
        self.assertEqual([d["date"] for d in tools.run(weather("Nowhereville", 7)).detail["days"]], ["2026-11-30"])

    def test_a_day_past_the_forecast_fails_as_out_of_range_with_the_range_it_has(self):
        for when in (10, 400, FARTHER):
            with self.subTest(when):
                result, _, _ = self.run_one(weather(when=when))
                self.assertEqual((result.outcome, result.summary),
                                 (Outcome.FAILED, "The weather lookup can look ahead 6 days, so it has no forecast "
                                                  "that far ahead."))
                self.assertEqual(result.detail["out_of_range"], {"last_date": "2026-11-22", "days_ahead": 6})

    def test_the_forecast_asks_for_the_range_the_lookup_counts_in(self):
        url = open_meteo.forecast_url(open_meteo.parse_place(RECORDED["http"][open_meteo.geocode_url("Denver")]))
        self.assertIn(f"forecast_days={open_meteo.FORECAST_DAYS}", url)
        self.assertIn(f"past_days={open_meteo.PAST_DAYS}", url)

    def test_the_time_comes_from_the_mcp_server_for_the_geocoded_time_zone(self):
        result, _, mcp = self.run_one(ToolCall("time", {"place": "Tokyo"}))
        self.assertEqual(mcp.calls, [("get_current_time", {"timezone": "Asia/Tokyo"})])
        self.assertEqual(result.outcome, Outcome.OK)
        recorded = RECORDED["mcp"]['get_current_time {"timezone": "Asia/Tokyo"}']
        self.assertEqual(result.detail["mcp_result"], recorded)
        hour, minute = recorded["datetime"][11:13], recorded["datetime"][14:16]
        clock = f"{int(hour) % 12 or 12}:{minute} {'PM' if int(hour) >= 12 else 'AM'}"
        self.assertEqual(result.summary.split(" on ")[0], f"In Tokyo, Japan it is {clock}")

    def test_a_place_geocoding_cannot_find_fails_with_a_spoken_reason(self):
        result, _, _ = self.run_one(weather("Atlantis Nowhere"))
        self.assertEqual((result.outcome, result.summary),
                         (Outcome.FAILED, "The weather lookup found no place named Atlantis Nowhere."))

    def test_no_place_and_no_default_fails_without_a_request(self):
        result, fetch, _ = self.run_one(weather(None, "saturday"), default_place=None)
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
                result = tools.run(weather())
                self.assertEqual((result.outcome, result.summary), (outcome, summary))
                self.assertEqual(result.detail, {"error": repr(exc)})

    def test_an_mcp_tool_error_fails_the_time_lookup(self):
        tools, _, _ = replay_registry()
        tools.tools["time"] = registry.Tool("time", "time lookup", "the current time in a city",
                                            failing_registry(McpError("Invalid timezone")).tools["time"].call)
        result = tools.run(ToolCall("time", {"place": "Tokyo"}))
        self.assertEqual((result.outcome, result.summary), (Outcome.FAILED, "The time lookup failed, so there is no result."))

    def test_the_record_is_plain_json(self):
        tools, _, _ = replay_registry()
        record = tools.run(ToolCall("time", {"place": "Tokyo"})).record()
        self.assertEqual((record["tool"], record["outcome"], record["arguments"]), ("time", "ok", {"place": "Tokyo"}))
        self.assertIsInstance(record["latency_s"], float)


class FilledTest(unittest.TestCase):
    """The arguments of a call are the decider's typed reading, the earlier lookup's when the reading says the segment
    continues it, and the default place. No instruction is read."""

    DENVER = weather("Denver", "tomorrow")

    def setUp(self):
        self.tools, _, _ = replay_registry(None)

    def dispatch(self, reading, earlier=None, instruction="Words nobody reads."):
        return self.tools.dispatch(instruction, earlier, choice=ToolChoice("weather", 0.9, reading)).call

    def test_the_readings_place_and_day_are_the_call(self):
        self.assertEqual(self.dispatch({"place": "Boston", "when": "saturday", "continues": False}),
                         weather("Boston", "saturday"))

    def test_a_follow_up_that_changes_only_the_place_keeps_the_earlier_day(self):
        self.assertEqual(self.dispatch({"place": "Boston", "when": None, "continues": True}, self.DENVER),
                         weather("Boston", "tomorrow"))
        self.assertEqual(self.dispatch({"place": "Boston", "when": None, "continues": True}, weather("Denver", 7)),
                         weather("Boston", 7), "a counted day carries over as it was")

    def test_a_follow_up_that_changes_only_the_day_keeps_the_earlier_place(self):
        self.assertEqual(self.dispatch({"place": None, "when": 7, "continues": True}, self.DENVER), weather("Denver", 7))

    def test_a_fresh_request_takes_nothing_from_the_earlier_lookup(self):
        self.assertEqual(self.dispatch({"place": "Boston", "when": None, "continues": False}, self.DENVER),
                         weather("Boston", "today"), "no day stated means today, not the earlier tomorrow")

    def test_a_fresh_request_that_names_no_place_has_none_to_run_with(self):
        self.assertEqual(self.dispatch({"place": None, "when": "tomorrow", "continues": False}, self.DENVER),
                         weather(None, "tomorrow"), "so the voice asks, as `waiting` says")
        waiting = self.tools.waiting("weather", self.DENVER, {"place": None, "when": "tomorrow", "continues": False})
        self.assertEqual(waiting["missing"], ("place",))

    def test_a_request_after_an_unrelated_lookup_continues_nothing(self):
        time = ToolCall("time", {"place": "Tokyo"})
        self.assertEqual(self.dispatch({"place": None, "when": 3, "continues": True}, time), weather(None, 3),
                         "only the immediately preceding lookup of the same tool can be continued")

    def test_a_new_call_has_no_earlier_lookup_to_continue(self):
        self.assertEqual(self.dispatch({"place": "Boston", "when": None, "continues": True}, None),
                         weather("Boston", "today"))

    def test_the_decider_does_not_get_overruled_by_the_words(self):
        for words in ("What about the day after tomorrow?", "What about a week from today?", "Saturday"):
            with self.subTest(words):
                self.assertEqual(self.dispatch({"place": None, "when": 2, "continues": True}, self.DENVER, words),
                                 weather("Denver", 2))

    def test_a_place_with_a_day_word_in_it_is_the_deciders_whole_span(self):
        self.assertEqual(self.dispatch({"place": "Friday Harbor", "when": None, "continues": False}),
                         weather("Friday Harbor", "today"))

    def test_the_default_place_stands_in_for_no_place(self):
        with_default, _, _ = replay_registry("Portland")
        call = with_default.dispatch("x", choice=ToolChoice("weather", 0.9, {"place": None, "when": None,
                                                                              "continues": False})).call
        self.assertEqual(call, weather("Portland", "today"))
        self.assertIsNone(with_default.waiting("weather", None, {"place": None, "when": None, "continues": False}))


class DispatchTest(unittest.TestCase):
    EAST_COAST = "Pick a city on the east coast to check the weather for."
    SAN_DIEGO = weather("San Diego", "today")

    def setUp(self):
        self.plain, _, _ = replay_registry()
        self.worker, _, _ = replay_registry("Boston", chat=lambda messages, timeout_s: "{}")

    def test_a_confident_choice_of_the_general_worker_asks_it(self):
        routed = self.worker.dispatch(self.EAST_COAST, self.SAN_DIEGO, choice=ToolChoice("agent", 0.62))
        self.assertEqual(routed.call, ToolCall("agent", {"instruction": self.EAST_COAST, "context": []}))
        self.assertEqual(routed.record()["via"], "decider")
        self.assertFalse(routed.record()["disagreement"])

    def test_a_choice_under_the_bar_goes_to_the_worker_and_logs_the_disagreement(self):
        routed = self.worker.dispatch("x", self.SAN_DIEGO, choice=ToolChoice("weather", 0.2))
        self.assertEqual(routed.call, ToolCall("agent", {"instruction": "x", "context": []}))
        self.assertEqual(routed.record()["via"], "worker")
        self.assertTrue(routed.record()["disagreement"])

    def test_with_no_worker_a_choice_under_the_bar_runs_nothing(self):
        self.assertIsNone(self.plain.dispatch("x", self.SAN_DIEGO, choice=ToolChoice("weather", 0.2)).call)
        self.assertIsNone(self.plain.dispatch("x", self.SAN_DIEGO).call)

    def test_a_confident_none_runs_nothing(self):
        routed = self.worker.dispatch("Nice weather today, huh?", choice=ToolChoice("none", 0.9))
        self.assertIsNone(routed.call)
        self.assertEqual(routed.record()["via"], "decider")

    def test_a_confident_choice_of_a_tool_takes_that_tool(self):
        routed = self.worker.dispatch("x", choice=ToolChoice("time", 0.5, {"place": "Denver", "when": None,
                                                                            "continues": False}))
        self.assertEqual(routed.call, ToolCall("time", {"place": "Denver"}))

    def test_ask_reaches_the_worker_only_when_one_is_configured(self):
        self.assertIsNone(self.plain.ask("When was George Washington born?"))
        self.assertEqual(self.worker.ask("When was George Washington born?"),
                         ToolCall("agent", {"instruction": "When was George Washington born?", "context": []}))


class WaitingTest(unittest.TestCase):
    """A call that cannot answer asks the person for what it lacks, and the decider's reading of their answer
    completes it."""

    def setUp(self):
        self.tools, _, _ = replay_registry(None)
        self.waiting = self.tools.waiting("weather", None, {"place": None, "when": None, "continues": False})

    def test_a_forecast_with_no_place_waits_for_a_city(self):
        self.assertEqual((self.waiting["tool"], self.waiting["missing"]), ("weather", ("place",)))
        self.assertEqual(self.waiting["arguments"], {"place": None, "when": "today"},
                         "the call it would run, so the answer completes it rather than starting over")

    def test_the_earlier_lookups_place_when_continued_or_the_default_one_is_enough(self):
        self.assertIsNone(self.tools.waiting("weather", weather("Denver", "tomorrow"), {"when": "saturday",
                                                                                         "continues": True}))

    def test_an_answer_the_decider_reads_as_one_completes_the_call(self):
        for place in ("Seattle", "Boston", "Friday Harbor"):
            with self.subTest(place):
                reading = {"place": place, "when": None, "answers": True}
                self.assertEqual(self.tools.answered(self.waiting, reading), weather(place, "today"))

    def test_the_completed_call_is_the_lookup_that_was_asked_for(self):
        result = self.tools.run(self.tools.answered(self.waiting, {"place": "Denver", "when": None, "answers": True}))
        self.assertEqual(result.outcome, Outcome.OK)
        self.assertTrue(result.summary.startswith("Forecast for Denver, Colorado."))

    def test_words_the_decider_does_not_read_as_the_answer_answer_nothing(self):
        for reading in ({"place": "Tokyo", "when": None, "answers": False}, {"place": None, "when": None, "answers": True},
                        {"place": "Boston", "when": None, "answers": None}, None):
            with self.subTest(reading):
                self.assertIsNone(self.tools.answered(self.waiting, reading))


class CorrectedTest(unittest.TestCase):
    """A correction of an earlier lookup runs that lookup's own tool on the corrected words. The argument the decider
    read in the amended words replaces the earlier one, and the rest stays."""

    WEATHER = weather("Boulder", "tomorrow")
    CALENDAR = ToolCall("calendar", {"when": "Thursday"})

    def setUp(self):
        self.tools, _, _ = replay_registry()

    def test_the_corrected_place_replaces_the_rejected_one(self):
        self.assertEqual(self.tools.corrected(self.WEATHER, "x", {"place": "Portland", "when": None}),
                         weather("Portland", "tomorrow"))

    def test_the_arguments_the_correction_leaves_alone_are_the_earlier_calls(self):
        self.assertEqual(self.tools.corrected(self.WEATHER, "x", {"place": None, "when": "sunday"}),
                         weather("Boulder", "sunday"))

    def test_a_tool_the_registry_does_not_hold_goes_to_the_general_worker(self):
        self.assertEqual(self.tools.corrected(self.CALENDAR, "Look up my calendar for Friday afternoon."),
                         ToolCall("agent", {"instruction": "Look up my calendar for Friday afternoon.", "context": []}))

    def test_a_general_worker_that_is_not_configured_fails_rather_than_raises(self):
        call = self.tools.corrected(self.CALENDAR, "Look up my calendar for Friday afternoon.")
        self.assertEqual(self.tools.run(call).outcome, Outcome.FAILED)


class ChoicesTest(unittest.TestCase):
    def test_the_options_are_every_registered_tool_the_worker_and_none(self):
        tools, _, _ = replay_registry(chat=lambda messages, timeout_s: "{}")
        self.assertEqual(list(tools.choices()), ["weather", "time", "agent", "none"])
        for name, line in tools.choices().items():
            with self.subTest(name):
                self.assertTrue(line and line[0].isupper(), f"{name} needs one sentence, what it does and when")


if __name__ == "__main__":
    unittest.main()
