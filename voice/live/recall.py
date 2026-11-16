"""What a question about the call refers to, read in code from the question and the call's own turns.

Measured failure it exists for: asked "What was the first thing I asked you?", the live server answered with the
call's most recent request. The call's turns were already in the snapshot item as JSON, numbered oldest first. This
module turns the one turn the question names into one short sentence, which the live server puts in the voice session
immediately before the reply, so the model answers from a stated fact rather than from the audio it just heard.

The turn a question names is chosen here, in code, and never written here: the note is the person's own words and a
turn number, and the model still composes the reply.

What it cannot see: a question worded outside the patterns below ("remind me how we started"), a topic the person
names with a word the call did not use ("the capital of Japan" for Tokyo), and a question about the assistant's own
earlier replies, which this file reads only as far as the person's turn it followed.
"""

import re

from voice.segmentation.contract import Intent
from voice.segmentation.intent import utterance_intent

PREFIX = "[call record, machine-generated, not spoken by the user] "
HISTORICAL = re.compile(
    r"\b(?:asked|said|told|mentioned|requested|answered)\b"
    r"|\bdid (?:you|i) (?:ask|say|tell|mention|request)\b", re.IGNORECASE)
PERSON = re.compile(r"\b(?:i|me|my)\b", re.IGNORECASE)
FIRST = re.compile(r"\b(?:first|1st|earliest|original|beginning|so far|at the start)\b", re.IGNORECASE)
LAST = re.compile(r"\b(?:last|latest|most recent|previous|prior|before this|before that|just now|just said"
                  r"|just asked)\b", re.IGNORECASE)
ORDINALS = {"second": 2, "2nd": 2, "third": 3, "3rd": 3, "fourth": 4, "4th": 4, "fifth": 5, "5th": 5}
TOPIC = re.compile(r"\b(?:about|regarding)\s+(?P<topic>[^?.!,]+)", re.IGNORECASE)
STOP = frozenset("the a an my your our their his her this that these those to of in on at for with and or but i me "
                 "you it is was are were do did does".split())


def _tokens(text):
    return tuple(re.findall(r"[a-z']+", (text or "").lower()))


def request_turns(turns):
    """The turn numbers, 1-based, that ask for something, so a greeting, a reaction or a correction is not one."""
    return [number for number, text in enumerate(turns, 1) if utterance_intent(text).intent == Intent.REQUEST]


def question_kind(text):
    """(kind, ...) for a question about the call, or None when it asks about something else.

    The kinds are ("first",), ("last",), ("request_number", n) and ("topic", topic as said, content words)."""
    said = text or ""
    if not (HISTORICAL.search(said) and PERSON.search(said)):
        return None
    for word in _tokens(said):
        if word in ORDINALS:
            return ("request_number", ORDINALS[word])
    if FIRST.search(said):
        return ("first",)
    if LAST.search(said):
        return ("last",)
    match = TOPIC.search(said)
    if match:
        words = tuple(word for word in _tokens(match["topic"]) if word not in STOP)
        if words:
            return ("topic", " ".join(match["topic"].split()), words)
    return None


def _ordinal(n):
    return {2: "second", 3: "third", 4: "fourth", 5: "fifth"}.get(n, f"{n}th")


def recall_note(question, turns):
    """One sentence naming the turn `question` asks about, or None when it asks about nothing in this call.

    `turns` is the person's own earlier turns, oldest first, so turns[0] is the call's turn 1."""
    kind = question_kind(question)
    if kind is None or not turns:
        return None
    total = len(turns)
    if kind[0] == "topic":
        topic, words = kind[1], kind[2]
        found = [number for number, text in enumerate(turns, 1) if all(word in _tokens(text) for word in words)]
        if found:
            number = found[-1]
            return _frame(f'about {topic}, turn {number} of {total} said "{turns[number - 1]}"')
        listed = "; ".join(f'turn {number}: "{text}"' for number, text in enumerate(turns, 1))
        return _frame(f"no turn mentions {topic}. The earlier turns were {listed}")
    asked = request_turns(turns)
    if not asked:
        return None
    if kind[0] == "first":
        number, label = asked[0], "the first request was"
    elif kind[0] == "last":
        number, label = asked[-1], "the most recent request was"
    else:
        if kind[1] > len(asked):
            return None
        number, label = asked[kind[1] - 1], f"request {_ordinal(kind[1])} was"
    return _frame(f'{label} turn {number} of {total}, "{turns[number - 1]}"')


def _frame(body):
    """The one opening every note shares: the words are a record of an earlier turn, not a request to run now."""
    return PREFIX + f"earlier in this call, not a request now. {body}"


def call_record(turns):
    """Every earlier turn of the call, numbered oldest first, one line each, in plain words."""
    listed = "; ".join(f'turn {number}: "{text}"' for number, text in enumerate(turns, 1))
    return PREFIX + f"earlier turns of this call, already said, oldest first: {listed}"
