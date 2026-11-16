"""Fact check for the voice layer: which day, date, time, number or place a reply names that the call never gave it.

The assistant invented "Sunday" after the person said "That's not what I meant." Nothing in the call said Sunday: the
task note carried no day, because the parroting fix drops any string that repeats the person's words, and the day the
person had said was in an earlier turn. The persona already said to use only the values in the note, and the model
broke it, so the check is here in code.

`sources` is what the assistant is allowed to say: the person's own words from this call, and the newest machine
note. The assistant's own earlier replies are not a source, or a wrong value it said once would become allowed.
`ungrounded(reply, sources)` returns the facts in `reply` that no source states, in the order they appear.

What it cannot see: a place named without a preposition ("Boulder looks clear"), a number spelled out in a reply, a
relative day ("tomorrow") with no anchor, and a value the person said in a way the patterns below do not read.
"""

import re
from typing import NamedTuple

WEEKDAYS = {"monday": "monday", "tuesday": "tuesday", "wednesday": "wednesday", "thursday": "thursday",
            "friday": "friday", "saturday": "saturday", "sunday": "sunday",
            "tues": "tuesday", "thurs": "thursday", "weds": "wednesday"}
# May is left out on purpose: it reads as a month and as a verb, so a bare May is not a date.
MONTHS = {"january": 1, "february": 2, "march": 3, "april": 4, "june": 6, "july": 7, "august": 8,
          "september": 9, "october": 10, "november": 11, "december": 12,
          "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "sept": 9,
          "oct": 10, "nov": 11, "dec": 12}
ORDINALS = r"(?:st|nd|rd|th)?"
NUMBER_WORDS = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
                "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
                "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30,
                "forty": 40, "fifty": 50, "sixty": 60, "hundred": 100}
LOCATIVES = r"(?:in|at|near|from|to|around|toward|towards|outside|across)"
NOT_A_PLACE = {"i", "you", "we", "he", "she", "they", "it", "me", "us", "them", "the", "a", "an", "my", "your",
               "his", "her", "their", "our", "this", "that", "these", "those", "sure", "okay", "ok", "yes", "no",
               "let", "here", "there", "now", "then", "and", "but", "so", "well", "hope", "good", "great", "thank",
               "thanks", "please", "alright", "right", "got", "once", "both", "each", "least", "last", "first",
               "next", "moment", "minute", "second", "degree", "degrees", "percent", "chance", "rain", "snow",
               "work", "all", "every", "more", "less", "today", "tonight", "tomorrow", "yesterday", "morning",
               "afternoon", "evening", "night"} | set(NUMBER_WORDS)
WEEKDAY_RE = re.compile(r"\b(" + "|".join(sorted(WEEKDAYS, key=len, reverse=True)) + r")s?\b", re.IGNORECASE)
MONTH_RE = re.compile(r"\b(" + "|".join(sorted(MONTHS, key=len, reverse=True)) + r")\.?(?=\s|\d|$)", re.IGNORECASE)
DATE_AFTER = re.compile(r"\s+(\d{1,2})" + ORDINALS + r"\b")
DATE_BEFORE = re.compile(r"(\d{1,2})" + ORDINALS + r"\s+$")
NUMBER_RE = re.compile(r"\b\d+(?:\.\d+)?\b")
TIME_RE = re.compile(r"\b(\d{1,2})(?::(\d{2}))?(?: ?(a\.?m\.?|p\.?m\.?|o'?clock))\b", re.IGNORECASE)
CLOCK_RE = re.compile(r"\b(\d{1,2}):(\d{2})\b")
HOUR_WORD_RE = re.compile(r"\b(?:at|around|by)\s+(\w+)", re.IGNORECASE)
PLACE_RUN = re.compile(r"\b" + LOCATIVES + r"\s+([A-Za-z][a-z]*(?:,\s*[A-Za-z][a-z]*)?)")
WORD_RE = re.compile(r"[A-Za-z][a-z]*")


# Words that belong to the machinery, not to the assistant's voice. A reply that says one of them is telling the
# person about the note, the check or the task list instead of answering them. Measured cost of the bare "note" in
# this list: 1 of 157 spoken replies on disk ("Got it, I've noted that ...") would be refused and asked again.
# "lookup" and "no answer yet" are a lookup note's own words. Measured on the callfix3 replay fix2, turns 21 and 23: "It
# looks like the lookup is still running and has no answer yet."
MACHINERY = ("fact check", "snapshot", "machine note", "machine-generated", "task description", "task update", "note",
             "lookup", "no answer yet")


class Fact(NamedTuple):
    kind: str
    surface: str
    key: tuple


# The person, or the assistant itself, named in the third person. A reply that says one is narrating the call
# instead of talking to the person. Measured on demo-corrections turn 3: "I need to know what information or details
# the user wants ...", and on the callfix3 probes: "The assistant responds with a friendly greeting ...".
THIRD_PERSON = re.compile(
    r"\bthe (?:user|person|caller|speaker|customer|assistant)\b"
    r"|\b(?:they|he|she) (?:want|wants|wanted|mean|meant|asked|said|need|needs|would like|are asking|were asking)\b",
    re.IGNORECASE)


def names_machinery(text):
    """The machinery words `text` says, in the order they appear. Empty when it names none."""
    low = (text or "").lower()
    return [word for word in MACHINERY if word in low]


def speaks_of_person(text):
    """The words in `text` that speak of the person in the third person, lower case, in order. Empty when none."""
    return [match.group(0).lower() for match in THIRD_PERSON.finditer(text or "")]


def _strings(payload):
    """Every string inside a machine note, so a status, a task id and a result summary are all readable."""
    if isinstance(payload, str):
        return [payload]
    if isinstance(payload, dict):
        return [text for value in payload.values() for text in _strings(value)]
    if isinstance(payload, (list, tuple)):
        return [text for value in payload for text in _strings(value)]
    if isinstance(payload, (int, float)) and not isinstance(payload, bool):
        return [str(payload)]
    return []


def _clock(hour, minute):
    return f"{int(hour) % 12 or 12}:{minute or '00'}"


def facts_in(text, worded_numbers=False, any_case_places=False):
    """The facts a piece of text states, with the words as written. Sources also read spelled-out numbers and
    lower-case place names."""
    hits = []
    for match in WEEKDAY_RE.finditer(text):
        hits.append((match.start(), match.end(), Fact("weekday", match.group(0),
                                                     ("weekday", WEEKDAYS[match.group(1).lower()]))))
    for match in MONTH_RE.finditer(text):
        month = MONTHS[match.group(1).lower()]
        after = DATE_AFTER.match(text, match.end())
        before = DATE_BEFORE.search(text[:match.start()])
        day = int(after.group(1)) if after else (int(before.group(1)) if before else None)
        if day is not None:
            end = after.end() if after else match.end()
            hits.append((match.start(), end, Fact("date", text[match.start():end].strip(),
                                                  ("date", f"{month}/{day}"))))
    times = []
    for match in TIME_RE.finditer(text):
        times.append(match.span())
        hits.append((match.start(), match.end(), Fact("time", match.group(0),
                                                      ("time", _clock(match.group(1), match.group(2))))))
    for match in CLOCK_RE.finditer(text):
        if any(start <= match.start() and match.end() <= end for start, end in times):
            continue
        times.append(match.span())
        hits.append((match.start(), match.end(), Fact("time", match.group(0),
                                                      ("time", _clock(match.group(1), match.group(2))))))
    if worded_numbers:
        for match in HOUR_WORD_RE.finditer(text):
            word = match.group(1).lower()
            if word in NUMBER_WORDS:
                hits.append((match.start(), match.end(), Fact(
                    "time", match.group(0), ("time", f"{NUMBER_WORDS[word] % 12 or 12}:00"))))
    for match in NUMBER_RE.finditer(text):
        if any(start <= match.start() and match.end() <= end for start, end in times):
            continue
        number = match.group(0)
        if re.fullmatch(r"\d+\.0+", number):
            number = str(int(float(number)))
        hits.append((match.start(), match.end(), Fact("number", match.group(0), ("number", number))))
    if worded_numbers:
        for match in re.finditer(r"\b(" + "|".join(NUMBER_WORDS) + r")\b", text, re.IGNORECASE):
            hits.append((match.start(), match.end(), Fact(
                "number", match.group(0), ("number", str(NUMBER_WORDS[match.group(1).lower()])))))
    for match in PLACE_RUN.finditer(text):
        for word in WORD_RE.finditer(match.group(1)):
            if not any_case_places and not word.group(0)[0].isupper():
                continue
            low = word.group(0).lower()
            if low in NOT_A_PLACE or low in WEEKDAYS or low in MONTHS:
                continue
            hits.append((match.start(1) + word.start(), match.start(1) + word.end(),
                         Fact("place", word.group(0), ("place", low))))
    hits.sort(key=lambda hit: hit[0])
    return [fact for _, _, fact in hits]


def ungrounded(text, sources):
    """The facts in `text` that no source states, in the order they appear, with duplicates dropped."""
    allowed, said = set(), set()
    for source in sources:
        for piece in _strings(source):
            allowed.update(fact.key for fact in facts_in(piece, worded_numbers=True, any_case_places=True))
            said.update(word.lower() for word in WORD_RE.findall(piece))
    seen, missing = set(), []
    for fact in facts_in(text or ""):
        # A place the person named with no preposition ("what about San Diego?") is still a place they named.
        if fact.key in allowed or fact.key in seen or (fact.kind == "place" and fact.key[1] in said):
            continue
        seen.add(fact.key)
        missing.append(fact)
    return missing
