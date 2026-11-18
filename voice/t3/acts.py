"""Typed dialog acts: what the voice may say about T3 work, as exact facts, never as sentences.

Code emits an `Act` for each thing the voice says. A text model words the acts (voice/t3/speech.py), and `check`
is the code's own say over that wording. It is narrow on purpose. A confirm act must come out as a question, each
named slot (a project, a thread, a message) must appear as given up to case, punctuation and first-to-second person,
and the line must be plain speech. Whether the line says what its act means is the decider's question
(voice/t3/verify.py), never a word list here. RULES holds one entry per act; its `forbids` are the status claims
the text model is told not to make and the decider is asked about.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Iterable, Mapping


class Kind(StrEnum):
    CONFIRM_LAUNCH = "confirm_launch"
    ASK_WHICH = "ask_which"
    NO_PROJECT = "no_project"
    LAUNCH_PENDING = "launch_pending"
    LAUNCHED = "launched"
    LAUNCH_UNCONFIRMED = "launch_unconfirmed"
    NO_THREAD = "no_thread"
    CONFIRM_THREAD = "confirm_thread"
    CONFIRM_TELL = "confirm_tell"
    CONFIRM_STOP = "confirm_stop"
    CANCELLED = "cancelled"
    THREAD_STATUS = "thread_status"
    TELL_PENDING = "tell_pending"
    SENT_FOLLOWUP = "sent_followup"
    TELL_UNCONFIRMED = "tell_unconfirmed"
    HELD_FOLLOWUP = "held_followup"
    DROPPED_FOLLOWUP = "dropped_followup"
    STOPPING = "stopping"
    NOTHING_RUNNING = "nothing_running"
    STOP_UNCONFIRMED = "stop_unconfirmed"
    RUN_UPDATE = "run_update"
    REPLIED = "replied"
    NEEDS_YOU = "needs_you"
    NEED_RESOLVED = "need_resolved"
    DONE = "done"
    THREAD_DELETED = "thread_deleted"
    LOST_TRACK = "lost_track"
    THREAD_GONE = "thread_gone"
    WATCH_QUEUED = "watch_queued"
    UNREACHABLE = "unreachable"
    SEAM_DOWN = "seam_down"
    REFUSED = "refused"
    LEGACY_UPDATE = "legacy_update"


@dataclass(frozen=True)
class Act:
    kind: Kind
    slots: dict[str, Any] = field(default_factory=dict, hash=False)

    def to_json(self) -> dict:
        return {"kind": self.kind.value, "slots": dict(self.slots)}

    @classmethod
    def from_json(cls, raw: Mapping) -> Act:
        return cls(Kind(raw["kind"]), dict(raw.get("slots") or {}))

    def with_slots(self, **slots) -> Act:
        return act(self.kind, **{**self.slots, **slots})


RUNNING = ("working", "waiting_on_you", "waiting_on_background", "queued", "idle", "running")
STATUS_FORBIDS: dict[str, tuple[str, ...]] = {
    **{status: ("finished", "stopped") for status in RUNNING},
    "finished": ("working",),
    "completed": ("working",),
    "failed": ("working",),
    "interrupted": ("working", "finished"),
    "cancelled": ("working", "finished"),
    "rolled_back": ("working",),
}


@dataclass(frozen=True)
class Rule:
    """`slots` an act may carry, `named` the ones its line must say as given, and `forbids` the status claims the
    text model is told, and the decider asked, that its line must not make. `question` acts ask the person
    something, so their line ends in a question mark. A `status` slot adds STATUS_FORBIDS for its value. A `plain`
    act carries a raw prompt as context, so its line must describe the need in words and never read out a command,
    a path or code."""

    slots: tuple[str, ...]
    named: tuple[str, ...] = ()
    question: bool = False
    forbids: tuple[str, ...] = ()
    plain: bool = False


RULES: dict[Kind, Rule] = {
    Kind.CONFIRM_LAUNCH: Rule(("project", "request"), ("project",), True, ("started",)),
    Kind.ASK_WHICH: Rule(("of", "candidates"), ("candidates",), True, ("started", "sent", "stopped")),
    Kind.NO_PROJECT: Rule(("available",), ("available",)),
    Kind.LAUNCH_PENDING: Rule(("project",)),
    Kind.LAUNCHED: Rule(("project", "request"), ("project",)),
    Kind.LAUNCH_UNCONFIRMED: Rule(("project",)),
    Kind.NO_THREAD: Rule(("recent", "other")),
    Kind.CONFIRM_THREAD: Rule(("thread",), ("thread",), True),
    Kind.CONFIRM_TELL: Rule(("thread", "message"), ("thread", "message"), True, ("sent",)),
    Kind.CONFIRM_STOP: Rule(("thread",), ("thread",), True, ("stopped",)),
    Kind.CANCELLED: Rule(("op", "thread"), ("thread",)),
    Kind.THREAD_STATUS: Rule(("thread", "status", "summary", "need", "pending", "waiting_for", "asked"), ("thread",),
                             plain=True),
    Kind.TELL_PENDING: Rule(("thread",), ("thread",), forbids=("sent",)),
    Kind.SENT_FOLLOWUP: Rule(("thread",), ("thread",)),
    Kind.TELL_UNCONFIRMED: Rule(("thread",), ("thread",)),
    Kind.HELD_FOLLOWUP: Rule(("thread",), ("thread",), forbids=("sent", "finished")),
    Kind.DROPPED_FOLLOWUP: Rule(("thread",), ("thread",), forbids=("sent",)),
    Kind.STOPPING: Rule(("thread",), ("thread",), forbids=("stopped", "finished")),
    Kind.NOTHING_RUNNING: Rule(("thread",), ("thread",), forbids=("stopped", "working")),
    Kind.STOP_UNCONFIRMED: Rule(("thread",), ("thread",), forbids=("stopped",)),
    Kind.RUN_UPDATE: Rule(("thread", "status"), ("thread",)),
    Kind.REPLIED: Rule(("thread", "status", "reply"), ("thread",), forbids=("finished", "stopped")),
    Kind.NEEDS_YOU: Rule(("thread", "need", "prompt", "waiting_for", "after_end", "summary"), ("thread",), plain=True),
    Kind.NEED_RESOLVED: Rule(("thread", "status"), ("thread",)),
    Kind.DONE: Rule(("thread", "status", "summary"), ("thread",)),
    Kind.THREAD_DELETED: Rule(("thread",), ("thread",)),
    Kind.LOST_TRACK: Rule(("thread",), ("thread",)),
    Kind.THREAD_GONE: Rule(("thread",), ("thread",)),
    Kind.WATCH_QUEUED: Rule(("thread", "following"), ("thread",)),
    Kind.UNREACHABLE: Rule(()),
    Kind.SEAM_DOWN: Rule(("reason",)),
    Kind.REFUSED: Rule(("op", "code", "reason"), forbids=("started", "sent")),
    Kind.LEGACY_UPDATE: Rule(("thread", "text")),
}
QUESTIONS = frozenset(kind for kind, rule in RULES.items() if rule.question)
# The acts whose words come from a read of the thread: the voice summarises it before it speaks them.
SUMMARISED = frozenset({Kind.THREAD_STATUS, Kind.DONE, Kind.NEEDS_YOU})
MAX_NAMED = 3


def act(kind: Kind, **slots) -> Act:
    """An act with its present slots. A slot the rule does not name is a bug, so it raises."""
    rule = RULES[kind]
    unknown = set(slots) - set(rule.slots)
    if unknown:
        raise ValueError(f"{kind.value} has no slot {sorted(unknown)}")
    kept = {name: value for name, value in slots.items() if value is not None and value != ()}
    for name in ("candidates", "available"):
        if name in kept:
            kept[name] = list(kept[name])[:MAX_NAMED]
    return Act(kind, kept)


_MARKDOWN = re.compile(r"[*_`#>|\[\]]+")
_NOT_WORD = re.compile(r"[^\w']+")
_PERSON = {"i": "you", "me": "you", "my": "your", "mine": "yours", "myself": "yourself", "i'm": "you're",
           "i've": "you've", "i'll": "you'll", "i'd": "you'd", "am": "are"}
_CONTRACTIONS = (("n't", " not"), ("'re", " are"), ("'ve", " have"), ("'ll", " will"), ("'d", " would"),
                 ("'m", " am"), ("'s", " s"))
_SENTENCE = re.compile(r"[.!?]+(?=\s|$)")
_NOT_SPEECH = re.compile(r"```|`|\*\*|https?://|\||\.\.\.|\N{HORIZONTAL ELLIPSIS}|</?\s*think\b|think\s*>", re.IGNORECASE)
# Shell syntax, a flag, a path or a backslash: what a raw command reads like, which no spoken line holds.
_CODE = re.compile(r"&&|\|\||\$[({]|\d?>&\d|(?:^|\s)--?[A-Za-z][\w-]*|(?:^|\s)~?\.{0,2}/\w|\w/[\w.-]+\.\w+|\\\w")
MAX_SENTENCES_PER_ACT = 2


def normalise(text: str) -> str:
    """Lower case, straight quotes, no markdown or punctuation, first person as second, contractions spelled out."""
    text = (text or "").lower().replace("\N{RIGHT SINGLE QUOTATION MARK}", "'").replace(
        "\N{LEFT SINGLE QUOTATION MARK}", "'")
    text = _MARKDOWN.sub(" ", text)
    words = [_PERSON.get(word.strip("'"), word.strip("'")) for word in _NOT_WORD.split(text) if word.strip("'")]
    text = " ".join(words)
    for short, full in _CONTRACTIONS:
        text = text.replace(short, full)
    return " ".join(text.split())


def says(line: str, value: str) -> bool:
    """`value` appears in `line` as whole words, after `normalise`."""
    want = normalise(value)
    return bool(want) and f" {want} " in f" {normalise(line)} "


def _named_values(value) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    return [str(value)]


def check(line: str, acts: Iterable[Act]) -> list[str]:
    """Why `line` may not be spoken for `acts`. Empty when it may."""
    acts = list(acts)
    problems = []
    text = (line or "").strip()
    if not text:
        return ["empty"]
    # A named value is said as given, so its own punctuation ("incl.", "...", a command's "|") is not the line's.
    form = text
    for value in sorted((v for a in acts for name in RULES[a.kind].named if name in a.slots
                         for v in _named_values(a.slots[name])), key=len, reverse=True):
        form = form.replace(value, "x")
    if _NOT_SPEECH.search(form):
        problems.append("not plain speech")
    if any(RULES[a.kind].plain for a in acts) and _CODE.search(form):
        problems.append("reads code")
    if len(_SENTENCE.findall(form if form[-1] in ".!?" else form + ".")) > MAX_SENTENCES_PER_ACT * len(acts):
        problems.append("too long")
    if any(a.kind in QUESTIONS for a in acts) and not text.endswith("?"):
        problems.append("not a question")
    for a in acts:
        for name in RULES[a.kind].named:
            if name in a.slots:
                for value in _named_values(a.slots[name]):
                    if not says(text, value):
                        problems.append(f"{a.kind.value} {name} missing: {value}")
    return problems


def forbidden_claims(acts: Iterable[Act]) -> set[str]:
    """The status claims a line for `acts` may not make: each act's own, less any another act in it states."""
    claimed, forbidden = set(), set()
    for a in acts:
        status = str(a.slots.get("status"))
        forbidden |= set(RULES[a.kind].forbids) | set(STATUS_FORBIDS.get(status, ()))
        if a.kind in (Kind.CANCELLED, Kind.REFUSED):
            forbidden |= set(OP_FORBIDS.get(str(a.slots.get("op")), ()))
        claimed |= set(CLAIMED.get(a.kind, ())) | (set(STATUS_CLAIMS.get(status, ())) if "status" in a.slots else set())
    return forbidden - claimed


OP_FORBIDS = {"start": ("started",), "tell": ("sent",), "stop": ("stopped",), "ask": ()}
# What an act states as true, so another act in the same line may not forbid it.
CLAIMED = {Kind.LAUNCHED: ("started",), Kind.SENT_FOLLOWUP: ("sent",)}
STATUS_CLAIMS = {"finished": ("finished",), "completed": ("finished",), "interrupted": ("stopped",),
                 "cancelled": ("stopped",), "working": ("working",), "running": ("working",)}
