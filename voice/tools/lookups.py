"""The read-only lookups: weather from Open-Meteo and the current time from the `mcp-server-time` MCP server.

Each lookup has a parser, from a task's instruction to its arguments or None, and a call, from the arguments to a
spoken summary and a detail record. The parsers are plain word rules, written for the instructions the segmenter
copies from the transcript. The place is the capitalized name after "in", "for" or "at". A parser can also take
the arguments of the earlier task a follow-up continues: then "And what about Saturday?" needs no subject word, as
long as it states a day or a place of its own, and what it leaves out comes from the earlier task. With no place
from either, a lookup uses the default place, and with no default it fails. The time lookup geocodes the place
with Open-Meteo to get its IANA time zone, then asks the MCP server for the time there.
"""

import re
import time
from datetime import datetime

from voice.tools import open_meteo

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
NOT_PLACES = set(WEEKDAYS) | {"today", "tonight", "tomorrow", "this", "next", "the", "weekend", "now", "right"}
PLACE = re.compile(r"\b(?:in|for|at)\s+([A-Z][\w'.-]*(?:\s+[A-Z][\w'.-]*)*)")
WEATHER = re.compile(r"\b(?:weather|forecast|temperature|umbrella|rain(?:s|y|ing)?|snow(?:s|y|ing)?)\b", re.I)
TIME = re.compile(r"\bwhat time\b|\btime is it\b|\bcurrent time\b|\blocal time\b", re.I)


class LookupFailed(Exception):
    """A lookup that ran but has no answer. The message is spoken as the result."""


def remaining(deadline):
    left = deadline - time.monotonic()
    if left <= 0:
        raise TimeoutError("lookup deadline passed")
    return left


def find_place(text):
    for match in PLACE.finditer(text):
        words = []
        for word in match.group(1).split():
            if word.lower() in NOT_PLACES:
                break
            words.append(word.rstrip(".'-"))
        if words:
            return " ".join(words)
    return None


def stated_when(text):
    """today, tomorrow, weekend, or a weekday name, or None when the text names no day."""
    words = re.findall(r"[a-z]+", text.lower())
    for when in ("weekend", "tomorrow", *WEEKDAYS, "today", "tonight"):
        if when in words:
            return "today" if when == "tonight" else when
    return None


def find_when(text):
    """The day a request asks about. A request that names no day asks about today."""
    return stated_when(text) or "today"


def pick_days(when, days):
    """The forecast days a `when` names. `days` starts with the place's today."""
    if when == "today":
        return days[:1]
    if when == "tomorrow":
        return days[1:2]
    if when == "weekend":
        first = next((d for d in days if d.date.weekday() >= 5), None)
        if first is None:
            return []
        following = [d for d in days if (d.date - first.date).days == 1 and d.date.weekday() == 6]
        return [first, *following]
    return [d for d in days if d.date.weekday() == WEEKDAYS.index(when)][:1]


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


def weather_tool(fetch, default_place):
    def parse(instruction, earlier=None):
        place, when, subject = find_place(instruction), stated_when(instruction), WEATHER.search(instruction)
        if not subject and (earlier is None or (place is None and when is None)):
            return None
        earlier = earlier or {}
        return {"place": place or earlier.get("place") or default_place,
                "when": when or earlier.get("when") or "today"}

    def call(arguments, deadline):
        if not arguments["place"]:
            raise LookupFailed("needs a place, and none was given")
        place = geocode(fetch, arguments["place"], deadline)
        days = open_meteo.parse_days(fetch(open_meteo.forecast_url(place), remaining(deadline)))
        picked = pick_days(arguments["when"], days)
        if not picked:
            raise LookupFailed(f"has no forecast for {arguments['when']}")
        summary = f"Forecast for {place.spoken()}. " + ". ".join(day_sentence(d) for d in picked) + "."
        return summary, {"place": vars(place), "days": [{**vars(d), "date": d.date.isoformat()} for d in picked]}

    return parse, call


def time_tool(fetch, mcp, default_place):
    def parse(instruction, earlier=None):
        place = find_place(instruction)
        if not TIME.search(instruction) and (earlier is None or place is None):
            return None
        return {"place": place or (earlier or {}).get("place") or default_place}

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

    return parse, call
