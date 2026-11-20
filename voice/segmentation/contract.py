"""The decision contract: transcript input, candidate segments, the choice questions, and parsing the answers.

The decider can only pick among options the caller offers, so every decision is a choice, noul, or extraction
question built from the current segments. Answers are untrusted and parsed here; the rest of the package trusts the types.
"""

import re
from dataclasses import dataclass, field
from enum import StrEnum

from voice.segmentation.client import MODEL
from voice.tools.registry import NONE, T3_ASK, T3_TOOLS, ToolChoice

# A place answer is a name: letters, spaces, and the punctuation a name can carry. Measured in the 37-turn replay:
# turn 020's answer was a copy of the request's own state, quotes and newlines included, and the forecast ran on it and
# failed where the words name Seattle.
PLACE_NAME = re.compile(r"^[\w\s.,'’-]+$")


class Kind(StrEnum):
    REQUEST = "request"
    DESCRIPTION = "description"
    CHITCHAT = "chitchat"
    CORRECTION = "correction"
    TOPIC_CHANGE = "topic_change"


class Readiness(StrEnum):
    DISPATCH_NOW = "dispatch_now"
    WAIT = "wait"
    CLARIFY = "clarify"


class CorrectionMode(StrEnum):
    REPLACE = "replace"
    CANCEL = "cancel"


class Intent(StrEnum):
    """What a whole utterance is. Only a request may start a new task."""
    REQUEST = "request"
    CORRECTION = "correction"
    CHITCHAT = "chitchat"


@dataclass(frozen=True)
class UtteranceIntent:
    """`replacement` is the words a correction states instead ("Sunday" in "no, Sunday"), or None when it states
    none ("that's not what I meant"), which leaves nothing to amend yet."""
    intent: Intent
    replacement: str | None = None


@dataclass(frozen=True)
class TranscriptUpdate:
    revision: int
    text: str
    is_final: bool


@dataclass(frozen=True)
class Segment:
    sid: str
    start: int
    end: int
    text: str
    open: bool


@dataclass(frozen=True)
class Correction:
    target: str
    mode: CorrectionMode


@dataclass(frozen=True)
class SegmentDecision:
    sid: str
    kind: Kind
    readiness: Readiness
    depends_on: str | None
    corrects: Correction | None
    side_effect: bool
    confidence: dict = field(default_factory=dict, compare=False)
    tool: ToolChoice | None = None


@dataclass(frozen=True)
class Decisions:
    """Parsed answers for one decision point. A segment with any malformed answer is left out of `segments`."""
    segments: dict[str, SegmentDecision]
    delegates: dict[str, str | None]
    problems: list[str]


KIND_CRITERIA = {
    Kind.REQUEST: "The speaker asks the assistant to do something or to find something out.",
    Kind.DESCRIPTION: "The speaker describes or mentions a task (their own, someone else's, past, or habitual) "
                      "without asking the assistant to do it.",
    Kind.CHITCHAT: "Small talk, filler, thanks, or a reaction, with no task in it.",
    Kind.CORRECTION: "The speaker changes, replaces, or withdraws something they asked for in an earlier segment.",
    Kind.TOPIC_CHANGE: "Only a transition to a new subject, such as \"anyway\" or \"different thing\", with no task "
                       "of its own.",
}

READINESS_CRITERIA = {
    Readiness.DISPATCH_NOW: "Complete and specific enough for a worker to start now.",
    Readiness.WAIT: "Still being spoken, or missing words a worker would need that more speech is likely to supply.",
    Readiness.CLARIFY: "Finished but ambiguous. The assistant must ask the speaker a question before anyone starts.",
}

SIDE_EFFECT_CRITERIA = {
    "true": "Carrying it out acts outside the assistant: it sends a message or email, contacts someone, buys, books, "
            "posts, or changes or deletes someone's data.",
    "false": "It only looks up, computes, or reports information back to the speaker.",
}
TOOL_INSTRUCTIONS = ("Which tool carries out segment {quote}? Pick the tool whose line matches what it asks for. Pick "
                     "none when it asks for nothing looked up and the assistant answers it from this call. Pick agent "
                     "when it needs information from outside this call that no other tool covers, or asks for one of "
                     "them while leaving the assistant to choose a detail first, such as the city.")
WHEN_CRITERIA = {
    "none": "It names no day and asks about no particular day.",
    "today": "It asks about today, or about the day in progress.",
    "tomorrow": "It asks about tomorrow.",
    "weekend": "It asks about the weekend.",
    **{day: f"It asks about {day.capitalize()}." for day in
       ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")},
}


def _quote(segment):
    return f"{segment.sid} (\"{segment.text}\")"


def segment_questions(segment, earlier, tools=None, arguments=False):
    """The choice questions for one segment. `earlier` is the list of segments before it.

    `tools` is the tool question's option set, as `Registry.choices` gives it, and `arguments` adds the two
    extraction questions a weather lookup needs when the decider reads them instead of the word rules.
    """
    q = {
        f"{segment.sid}_kind": {"type": "choice", "instructions": f"Classify segment {_quote(segment)}.",
                                "criteria": {k.value: v for k, v in KIND_CRITERIA.items()}},
        f"{segment.sid}_ready": {"type": "choice",
                                 "instructions": f"If segment {_quote(segment)} is a request or a correction, is "
                                                 "it ready to hand to a worker?",
                                 "criteria": {k.value: v for k, v in READINESS_CRITERIA.items()}},
        f"{segment.sid}_side_effect": {"type": "noul",
                                       "instructions": f"Would carrying out segment {_quote(segment)} act outside "
                                                       "the assistant?",
                                       "criteria": SIDE_EFFECT_CRITERIA},
    }
    if tools:
        q[f"{segment.sid}_tool"] = {"type": "choice",
                                    "instructions": TOOL_INSTRUCTIONS.format(quote=_quote(segment)),
                                    "criteria": dict(tools)}
    if arguments:
        q[f"{segment.sid}_place"] = {
            "type": "text",
            "instructions": f"In segment {_quote(segment)}, which city, town or place is named, exactly as it is "
                            "written? Read only a place the speaker says there, not one from an earlier segment."}
        q[f"{segment.sid}_when"] = {
            "type": "choice",
            "instructions": f"In segment {_quote(segment)}, which day does the request ask about?",
            "criteria": WHEN_CRITERIA}
    if earlier:
        q[f"{segment.sid}_depends"] = {
            "type": "choice",
            "instructions": f"Does segment {_quote(segment)} need the result of an earlier segment before it can "
                            "start?",
            "criteria": {"none": "It can start without any earlier segment's result.",
                         **{e.sid: f"It needs the result of {_quote(e)} first." for e in earlier}},
        }
        q[f"{segment.sid}_corrects"] = {
            "type": "choice",
            "instructions": f"Does segment {_quote(segment)} change or withdraw an earlier segment?",
            "criteria": {"none": "It does not change any earlier segment.",
                         **{f"replace_{e.sid}": f"It states a new version of {_quote(e)}." for e in earlier},
                         **{f"cancel_{e.sid}": f"It withdraws {_quote(e)} with no replacement." for e in earlier}},
        }
    return q


def delegate_question(did, instruction, segments):
    return {f"{did}_same_as": {
        "type": "choice",
        "instructions": f"The voice assistant submitted the task \"{instruction}\". Which transcript segment asks "
                        "for the same thing?",
        "criteria": {"new": "No segment asks for it.",
                     **{s.sid: f"{_quote(s)} asks for the same thing." for s in segments}},
    }}


def decision_state(update, segments, tasks, delegates):
    open_sid = next((s.sid for s in segments if s.open), None)
    return {
        "transcript": update.text,
        "transcript_is_final": update.is_final,
        "segments": {s.sid: s.text for s in segments},
        "still_being_spoken": open_sid,
        "tasks": tasks,
        "delegate_requests": delegates,
    }


def decision_request(update, segments, tasks, delegates, only=None, tools=None, arguments=False):
    """Build one decisions body. `only` limits the questions to one segment id, for the bundling comparison.

    `tools` adds the tool question to every segment, and `arguments` the place and day extraction questions, so every
    answer a dispatch reads arrives in this one request and the state is billed once."""
    questions = {}
    for i, segment in enumerate(segments):
        if only is None or only == segment.sid:
            questions.update(segment_questions(segment, segments[:i], tools, arguments))
    if only is None:
        for did, instruction in delegates.items():
            questions.update(delegate_question(did, instruction, segments))
    return {"model": MODEL, "state": decision_state(update, segments, tasks, delegates), "questions": questions}


def _choice(answers, name, options, problems):
    answer = answers.get(name)
    if not isinstance(answer, dict) or answer.get("type") != "choice" or answer.get("choice") not in options:
        problems.append(f"{name}: {_describe(answer)}")
        return None
    return answer["choice"]


def _noul(answers, name, problems):
    answer = answers.get(name)
    value = answer.get("noul") if isinstance(answer, dict) and answer.get("type") == "noul" else None
    if not isinstance(value, (int, float)) or not 0 <= value <= 1:
        problems.append(f"{name}: {_describe(answer)}")
        return None
    return value


def _describe(answer):
    if answer is None:
        return "missing"
    if isinstance(answer, dict):
        return f"type={answer.get('type')!r} choice={answer.get('choice')!r}"
    return f"unexpected {type(answer).__name__}"


def _text(answers, name):
    """A `text` answer's value, or None when none was found and when the answer is unusable."""
    answer = answers.get(name)
    value = answer.get("text") if isinstance(answer, dict) and answer.get("type") == "text" else None
    return value.strip() if isinstance(value, str) and value.strip() else None


def _reading(answers, sid, questions, problems):
    """The place and day the decider read for one segment, or None when the request did not ask for them. An
    unreadable answer leaves the argument to the tool's parser rather than failing the segment."""
    if f"{sid}_place" not in questions:
        return None
    answer = answers.get(f"{sid}_when")
    when = answer.get("choice") if isinstance(answer, dict) and answer.get("type") == "choice" else None
    if when not in WHEN_CRITERIA:
        return None
    place = _text(answers, f"{sid}_place")
    if place is not None and not PLACE_NAME.match(place):
        problems.append(f"{sid}_place: not a name, {place[:40]!r}")
        place = None
    return {"place": place, "when": None if when == "none" else when}


def _thread_pick(picked, margin, probabilities):
    """The T3 tools are four ways to act on one subject, so when "no tool" wins only because they split the weight,
    the turn is about a thread. Which of the four it meant is not known from a sum, so the pick is the lookup, which
    acts on nothing, at the sum's margin over the next option. A pick of another tool stands. Only on the final
    transcript: while it is spoken, a fragment of a longer start request reads this way (50 of 1,937 partial request
    reads in the decider cache, 3 of 916 final ones, measured 2026-11-19)."""
    if picked != NONE or not isinstance(probabilities, dict):
        return picked, margin
    weights = {name: p for name, p in probabilities.items() if isinstance(p, (int, float))}
    threads = sum(p for name, p in weights.items() if name in T3_TOOLS)
    other = max((p for name, p in weights.items() if name not in T3_TOOLS), default=0.0)
    if threads <= other:
        return picked, margin
    return T3_ASK, threads - other


def parse_decisions(response, body):
    """Parse a decisions response against the request that produced it. Never raises on bad model output.

    A malformed answer to a question that says whether the task may run drops its segment, as before. A malformed
    tool or argument answer only sends that task back to the word rules, and is logged as a problem."""
    problems, tool_problems = [], []
    answers = response.get("answers") if isinstance(response, dict) else None
    if not isinstance(answers, dict):
        return Decisions({}, {}, ["no answers object"])
    questions = body["questions"]
    segments = {}
    for sid in body["state"]["segments"]:
        if f"{sid}_kind" not in questions:
            continue
        before = len(problems)
        kind = _choice(answers, f"{sid}_kind", set(Kind), problems)
        ready = _choice(answers, f"{sid}_ready", set(Readiness), problems)
        side = _noul(answers, f"{sid}_side_effect", problems)
        depends = corrects = "none"
        if f"{sid}_depends" in questions:
            depends = _choice(answers, f"{sid}_depends", set(questions[f"{sid}_depends"]["criteria"]), problems)
            corrects = _choice(answers, f"{sid}_corrects", set(questions[f"{sid}_corrects"]["criteria"]), problems)
        tool = None
        if f"{sid}_tool" in questions:
            picked = _choice(answers, f"{sid}_tool", set(questions[f"{sid}_tool"]["criteria"]), tool_problems)
            margin = answers.get(f"{sid}_tool", {}).get("confidence")
            margin = margin if isinstance(margin, (int, float)) else 0.0
            if picked is not None and body["state"].get("transcript_is_final"):
                picked, margin = _thread_pick(picked, margin, answers[f"{sid}_tool"].get("probabilities"))
            tool = None if picked is None else ToolChoice(picked, margin,
                                                          _reading(answers, sid, questions, tool_problems))
        if len(problems) > before:
            continue
        correction = None
        if corrects != "none":
            mode, _, target = corrects.partition("_")
            correction = Correction(target, CorrectionMode(mode))
        segments[sid] = SegmentDecision(
            sid=sid, kind=Kind(kind), readiness=Readiness(ready),
            depends_on=None if depends == "none" else depends, corrects=correction, side_effect=side >= 0.5,
            tool=tool,
            confidence={name.removeprefix(f"{sid}_"): a.get("confidence", a.get("noul"))
                        for name, a in answers.items() if name.startswith(f"{sid}_") and isinstance(a, dict)})
    delegates = {}
    for name, question in questions.items():
        if name.endswith("_same_as"):
            did = name.removesuffix("_same_as")
            pick = _choice(answers, name, set(question["criteria"]), problems)
            if pick is not None:
                delegates[did] = None if pick == "new" else pick
    return Decisions(segments, delegates, problems + tool_problems)
