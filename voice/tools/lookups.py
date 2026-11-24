"""The read-only lookups: weather from Open-Meteo and the current time from the `mcp-server-time` MCP server.

Each lookup has a parser, from a task's instruction to its arguments or None, and a call, from the arguments to a
spoken summary and a detail record. The parsers are plain word rules, written for the instructions the segmenter
copies from the transcript. The place is the capitalized name after "in", "for" or "at". A parser can also take
the arguments of the earlier task a follow-up continues: then "And what about Saturday?" needs no subject word, as
long as it states a day or a place of its own, and what it leaves out comes from the earlier task. With no place
from either, a lookup uses the default place, and with no default it fails. The time lookup geocodes the place
with Open-Meteo to get its IANA time zone, then asks the MCP server for the time there.

The day table reads a weekday, `today`, `tomorrow`, `day after tomorrow`, and `the day after`, which is the day after
the earlier task's own day (`resolved`). It also rejoins one word the ASR split in two: "And s aturday?" is Saturday.
A day the words count from today ("in three days", "a week from today") is not in the table: the decider chooses it by
name, `_reading` turns the name into a whole number of days, `pick_days` takes that many days after the forecast's
first, and a day past the forecast's end is a failed lookup that says how far ahead it can look.
`answer_place` reads the place a person gives in answer to a question about it, and `names` says whether a text says
a value at all, which is what lets the registry keep a reading of the segment's own words and drop one it invented.
"""

import re
import time
from datetime import datetime

from voice.tools import open_meteo

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
DAY_AFTER_TOMORROW = "day after tomorrow"
# A day named relative to the earlier lookup's: "And what about the day after?" after a forecast for tomorrow.
DAY_AFTER = "the day after"
# A day counted from today is a whole number of days in a `when`, beside the named days. The decider chooses one of
# `COUNTED_DAYS` by name, one for each day the forecast reaches past tomorrow, or `FARTHER` for a day beyond it; the
# general worker may write any number up to `MAX_AHEAD`. `pick_days` takes that many days after the forecast's first.
COUNTED_DAYS = {f"in_{days}_days": days for days in range(2, open_meteo.FORECAST_DAYS)}
FARTHER = "farther"
MAX_AHEAD = 366
LATER = {"today": "tomorrow", "tomorrow": DAY_AFTER_TOMORROW}
# Longest phrase first, because "the day after tomorrow" holds "the day after".
DAY_PHRASES = ([("the day after tomorrow", DAY_AFTER_TOMORROW), ("day after tomorrow", DAY_AFTER_TOMORROW),
                ("the next day", DAY_AFTER), ("the day after", DAY_AFTER), ("next day", DAY_AFTER),
                ("day after", DAY_AFTER), ("weekend", "weekend"), ("tomorrow", "tomorrow"), ("tonight", "today"),
                ("today", "today")] + [(day, day) for day in WEEKDAYS])
# The days the ASR can split in two, which `said_words` rejoins.
DAY_WORDS = frozenset([*WEEKDAYS, "today", "tomorrow", "tonight", "weekend"])
NOT_PLACES = set(WEEKDAYS) | {"today", "tonight", "tomorrow", "this", "next", "the", "weekend", "now", "right"}
# The words a person puts around a place when they answer a question about it: "In Boston", "Seattle.", "the city
# is Boston". A word that is not one of these makes the words a request of their own, not an answer.
ANSWER_FILLER = frozenset("in at for to the a an city town place it's its is that's uh um er oh well sure okay ok yes "
                          "yeah yep please just".split())
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


def squash(text):
    """The text lowercased with everything that is not a letter or a digit removed."""
    return re.sub(r"[^a-z0-9]", "", text.lower())


def said_words(text):
    """The text's words, lowercased, with an ASR split inside a day rejoined: "And s aturday?" says saturday. Only a
    pair whose join is a day the table knows is rejoined, so ordinary words are left alone."""
    words, out, i = re.findall(r"[a-z]+", text.lower()), [], 0
    while i < len(words):
        pair = words[i] + (words[i + 1] if i + 1 < len(words) else "")
        if pair in DAY_WORDS:
            out.append(pair)
            i += 2
        else:
            out.append(words[i])
            i += 1
    return out


def stated_when(text):
    """The day a text names, in `DAY_PHRASES`' words, or None when it names no day. `DAY_AFTER` is a day relative to
    an earlier lookup's, which `resolved` turns into a day of its own."""
    said = f" {' '.join(said_words(text))} "
    return next((when for phrase, when in DAY_PHRASES if f" {phrase} " in said), None)


def later(when):
    """The day after `when` names, or None when it names no single day to step from."""
    if isinstance(when, int):
        return when + 1
    if when in WEEKDAYS:
        return WEEKDAYS[(WEEKDAYS.index(when) + 1) % 7]
    return LATER.get(when)


def resolved(when, earlier_when=None):
    """`when` with a relative day made absolute: the day after a Saturday is Sunday, and the day after a tomorrow is
    the day after tomorrow. A `when` that names no such reference stands as it is."""
    return later("today" if earlier_when is None else earlier_when) if when == DAY_AFTER else when


def find_when(text):
    """The day a request asks about, with a relative day counted from today. A request that names no day asks about
    today."""
    return resolved(stated_when(text)) or "today"


def names(text, value):
    """Whether `text` says `value`, punctuation and word boundaries aside, so an ASR split inside the value still
    counts: "s aturday" says Saturday."""
    return bool(value) and squash(value) in squash(text or "")


def supported(reading, text):
    """The decider's reading of this segment's own words: its place and day, with a value the words do not say
    dropped.

    Measured on Mason's recording: the model answers `today` for words that name no day (turns 004 and 022), so a named
    day it read cannot be believed over the words; and turn 016's "Wait, do it again." names no place, so a place read
    there could only be a guess or a copy of the earlier forecast's. A place the words do say stays, which is what
    reads "what about San Diego?" where the preposition rule cannot. A number of days is the decider's own count and
    stays: the table of day words has no way to say it, and its `today` in "a week from today" is the fault.
    """
    reading = reading or {}
    place, when = reading.get("place"), reading.get("when")
    counted = isinstance(when, int) or when == FARTHER
    return {"place": place if names(text, place) else None,
            "when": when if counted or (when is not None and when == stated_when(text)) else None}


def answer_place(text):
    """The place a person names in answer to a question about it: "In Boston", "Seattle.", "the city is Boston". None
    when the words ask something themselves ("What's the time in Tokyo?"), or name no place."""
    if "?" in (text or ""):
        return None
    words = re.findall(r"[A-Za-z][\w'’-]*", text or "")
    while words and words[0].lower() in ANSWER_FILLER:
        words = words[1:]
    while words and words[-1].lower() in ANSWER_FILLER:
        words = words[:-1]
    if not words or len(words) > 3 or not all(word[0].isupper() for word in words):
        return None
    if any(word.lower() in NOT_PLACES for word in words):
        return None
    return " ".join(words)


ANSWERS = {"place": answer_place}


def answer(field, text):
    """The value a person's words give for the argument `field`, or None when they give none."""
    return ANSWERS[field](text) if field in ANSWERS else None


def pick_days(when, days):
    """The forecast days a `when` names. `days` starts with the place's today. A number is that many days after it."""
    if isinstance(when, int):
        return days[when:when + 1]
    if when == FARTHER:
        return []
    if when == "today":
        return days[:1]
    if when == "tomorrow":
        return days[1:2]
    if when == DAY_AFTER_TOMORROW:
        return days[2:3]
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
                "when": resolved(when, earlier.get("when")) or earlier.get("when") or "today"}

    def call(arguments, deadline):
        if not arguments["place"]:
            raise LookupFailed("needs a place, and none was given")
        place = geocode(fetch, arguments["place"], deadline)
        days = open_meteo.parse_days(fetch(open_meteo.forecast_url(place), remaining(deadline)))
        picked = pick_days(arguments["when"], days)
        if not picked and (isinstance(arguments["when"], int) or arguments["when"] == FARTHER):
            raise LookupFailed(f"can look ahead {len(days) - 1} days, so it has no forecast that far ahead")
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
