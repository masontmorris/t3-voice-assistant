"""The read-only lookups: weather from Open-Meteo and the current time from the `mcp-server-time` MCP server.

A lookup is a call, from typed arguments to a spoken summary and a detail record. Nothing here reads the caller's
words: the decider reads the place and the day (`voice/segmentation/contract.py`) and the registry fills what it leaves
out (`voice/tools/registry.py`). With no place from either, a lookup uses the default place, and with no default it
fails. The time lookup geocodes the place with Open-Meteo to get its IANA time zone, then asks the MCP server for the
time there.

A weather `when` is a typed day: `today`, `tomorrow`, `weekend`, a weekday name, a whole number of days after today, or
`farther`, a day the decider says is beyond the forecast. `wanted_dates` turns it into calendar dates counted from the
caller's today, which the caller of `weather_call` injects, and the forecast's days are selected by those dates, so a
place whose own date differs from the caller's still answers for the caller's day. A date the forecast does not hold
fails the lookup as out of range, with the range it has.
"""

import time
from datetime import date, datetime, timedelta

from voice.tools import open_meteo

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
# The decider chooses a counted day by name, one for each day the forecast reaches past tomorrow, or FARTHER for a
# day beyond it. The general worker may write any whole number of days.
COUNTED_DAYS = {f"in_{days}_days": days for days in range(2, open_meteo.FORECAST_DAYS)}
FARTHER = "farther"


class LookupFailed(Exception):
    """A lookup that ran but has no answer. The message is the result; `detail` is the typed reason."""

    def __init__(self, message, detail=None):
        super().__init__(message)
        self.detail = detail or {}


def remaining(deadline):
    left = deadline - time.monotonic()
    if left <= 0:
        raise TimeoutError("lookup deadline passed")
    return left


def counted(when):
    return isinstance(when, int) and not isinstance(when, bool)


def wanted_dates(when, today):
    """The calendar dates a `when` names, counted from `today`, or None when it names none a forecast could hold."""
    try:
        if when == "today":
            return [today]
        if when == "tomorrow":
            return [today + timedelta(days=1)]
        if counted(when) and when >= 0:
            return [today + timedelta(days=when)]
    except OverflowError:
        return None
    if when == "weekend":
        first = next(day for day in (today + timedelta(days=i) for i in range(7)) if day.weekday() >= 5)
        return [first, first + timedelta(days=1)] if first.weekday() == 5 else [first]
    if when in WEEKDAYS:
        return [today + timedelta(days=(WEEKDAYS.index(when) - today.weekday()) % 7)]
    return None


def pick_days(when, days, today):
    """The forecast days whose dates a `when` names, counted from the caller's `today`. None of them is out of range."""
    wanted = wanted_dates(when, today) or ()
    return [d for d in days if d.date in wanted]


def day_sentence(day):
    name = f"{day.date:%A}, {day.date:%B} {day.date.day}"
    high = "no high temperature" if day.high_f is None else f"a high of {round(day.high_f)} degrees Fahrenheit"
    rain = ("no precipitation estimate" if day.precipitation_pct is None
            else f"a {day.precipitation_pct} percent chance of precipitation")
    return f"{name}: {high} and {rain}"


def geocode(fetch, name, deadline):
    place = open_meteo.parse_place(fetch(open_meteo.geocode_url(name), remaining(deadline)))
    if place is None:
        raise LookupFailed(f"found no place named {name}")
    return place


def weather_call(fetch, today=date.today):
    """The weather lookup. `today` is the caller's date; the server's own date stands for it, since a call carries no
    time zone of its own."""

    def call(arguments, deadline):
        if not arguments["place"]:
            raise LookupFailed("needs a place, and none was given")
        place = geocode(fetch, arguments["place"], deadline)
        days = open_meteo.parse_days(fetch(open_meteo.forecast_url(place), remaining(deadline)))
        caller_today = today()
        picked = pick_days(arguments["when"], days, caller_today)
        if not picked:
            last = max((d.date for d in days), default=caller_today)
            ahead = (last - caller_today).days
            raise LookupFailed(f"can look ahead {ahead} days, so it has no forecast that far ahead",
                               {"out_of_range": {"last_date": last.isoformat(), "days_ahead": ahead}})
        summary = f"Forecast for {place.spoken()}. " + ". ".join(day_sentence(d) for d in picked) + "."
        return summary, {"place": vars(place), "days": [{**vars(d), "date": d.date.isoformat()} for d in picked]}

    return call


def time_call(fetch, mcp):
    def call(arguments, deadline):
        if not arguments["place"]:
            raise LookupFailed("needs a place, and none was given")
        place = geocode(fetch, arguments["place"], deadline)
        started = time.perf_counter()
        result = mcp.call_tool("get_current_time", {"timezone": place.timezone}, remaining(deadline))
        mcp_s = time.perf_counter() - started
        now = datetime.fromisoformat(result["datetime"])
        clock = f"{now:%I:%M %p}".lstrip("0")
        summary = f"In {place.spoken()} it is {clock} on {result['day_of_week']}, {now:%B} {now.day}."
        return summary, {"place": vars(place), "mcp_tool": "get_current_time", "mcp_result": result,
                         "mcp_latency_s": mcp_s}

    return call
