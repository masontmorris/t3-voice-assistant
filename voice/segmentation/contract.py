"""The decision contract: transcript input, candidate segments, the choice questions, and parsing the answers.

The decider can only pick among options the caller offers, so every decision is a choice, noul, or extraction
question built from the current segments. Answers are untrusted and parsed here; the rest of the package trusts the types.
"""

from dataclasses import dataclass, field
from enum import StrEnum

from voice.segmentation.client import MODEL


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


def _quote(segment):
    return f"{segment.sid} (\"{segment.text}\")"


def segment_questions(segment, earlier):
    """The choice questions for one segment. `earlier` is the list of segments before it."""
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


def decision_request(update, segments, tasks, delegates, only=None):
    """Build one decisions body. `only` limits the questions to one segment id, for the bundling comparison."""
    questions = {}
    for i, segment in enumerate(segments):
        if only is None or only == segment.sid:
            questions.update(segment_questions(segment, segments[:i]))
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


def parse_decisions(response, body):
    """Parse a decisions response against the request that produced it. Never raises on bad model output."""
    problems = []
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
        if len(problems) > before:
            continue
        correction = None
        if corrects != "none":
            mode, _, target = corrects.partition("_")
            correction = Correction(target, CorrectionMode(mode))
        segments[sid] = SegmentDecision(
            sid=sid, kind=Kind(kind), readiness=Readiness(ready),
            depends_on=None if depends == "none" else depends, corrects=correction, side_effect=side >= 0.5,
            confidence={name.removeprefix(f"{sid}_"): a.get("confidence", a.get("noul"))
                        for name, a in answers.items() if name.startswith(f"{sid}_") and isinstance(a, dict)})
    delegates = {}
    for name, question in questions.items():
        if name.endswith("_same_as"):
            did = name.removesuffix("_same_as")
            pick = _choice(answers, name, set(question["criteria"]), problems)
            if pick is not None:
                delegates[did] = None if pick == "new" else pick
    return Decisions(segments, delegates, problems)
