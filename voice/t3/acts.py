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
from collections import Counter
from dataclasses import dataclass, field, replace
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
    SAME_TITLE = "same_title"
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
    CORRECTION = "correction"
    UNANSWERED = "unanswered"
    NOTHING_DONE = "nothing_done"
    CONFIRM_RESPOND = "confirm_respond"
    RESPONDED = "responded"
    NOTHING_PENDING = "nothing_pending"
    NEEDS_THE_APP = "needs_the_app"
    ASK_QUESTION = "ask_question"
    CONFIRM_ANSWER = "confirm_answer"
    ANSWERED = "answered"
    ANSWER_IN_APP = "answer_in_app"


class Outcome(StrEnum):
    STARTED = "started"
    SENT = "sent"
    STOP_REQUESTED = "stop_requested"
    ANSWERED = "answered"
    APPROVED = "approved"
    DENIED = "denied"


OUTCOME_CLAIMS = {Outcome.STARTED: "started", Outcome.SENT: "sent", Outcome.ANSWERED: "answered",
                  Outcome.APPROVED: "answered", Outcome.DENIED: "answered"}
OUTCOME_WORDS = {
    Outcome.STARTED: ("started", "coding thread"),
    Outcome.SENT: ("sent", "follow-up message"),
    Outcome.STOP_REQUESTED: ("asked T3 to stop", "coding thread"),
    Outcome.ANSWERED: ("answered", "thread question"),
    Outcome.APPROVED: ("approved", "permission request"),
    Outcome.DENIED: ("denied", "permission request"),
}


def outcome_meaning(outcomes) -> str:
    phrases = []
    for outcome, count in Counter(outcomes).items():
        verb, subject = OUTCOME_WORDS[Outcome(outcome)]
        phrases.append(f"{verb} one {subject}" if count == 1 else f"{verb} {count} {subject}s")
    return " and ".join(phrases)


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
    Kind.NO_PROJECT: Rule(()),
    Kind.LAUNCH_PENDING: Rule(("project",)),
    Kind.LAUNCHED: Rule(("project", "request", "recap"), ("project",)),
    Kind.LAUNCH_UNCONFIRMED: Rule(("project",)),
    Kind.NO_THREAD: Rule(("recent", "other")),
    Kind.CONFIRM_THREAD: Rule(("thread",), ("thread",), True),
    Kind.CONFIRM_TELL: Rule(("thread", "message"), ("thread", "message"), True, ("sent",)),
    Kind.CONFIRM_STOP: Rule(("thread",), ("thread",), True, ("stopped",)),
    Kind.SAME_TITLE: Rule(("thread", "op"), ("thread",), forbids=("sent", "stopped")),
    Kind.CANCELLED: Rule(("op", "thread"), ("thread",)),
    # `pending` and `prompt` are the request's stored text. `Speech.fill` takes them out before a line is worded and
    # puts `request` in their place: a description of that request that passed the approval gate, or nothing.
    Kind.THREAD_STATUS: Rule(("thread", "status", "summary", "need", "pending", "request", "waiting_for", "asked",
                              "since_last_asked"), ("thread",),
                             plain=True),
    Kind.TELL_PENDING: Rule(("thread",), ("thread",), forbids=("sent",)),
    Kind.SENT_FOLLOWUP: Rule(("thread", "recap"), ("thread",)),
    Kind.TELL_UNCONFIRMED: Rule(("thread",), ("thread",)),
    Kind.HELD_FOLLOWUP: Rule(("thread",), ("thread",), forbids=("sent", "finished")),
    Kind.DROPPED_FOLLOWUP: Rule(("thread",), ("thread",), forbids=("sent",)),
    Kind.STOPPING: Rule(("thread", "recap"), ("thread",), forbids=("stopped", "finished")),
    Kind.NOTHING_RUNNING: Rule(("thread",), ("thread",), forbids=("stopped", "working")),
    Kind.STOP_UNCONFIRMED: Rule(("thread",), ("thread",), forbids=("stopped",)),
    Kind.RUN_UPDATE: Rule(("thread", "status"), ("thread",)),
    Kind.REPLIED: Rule(("thread", "status", "reply"), ("thread",), forbids=("finished", "stopped")),
    Kind.NEEDS_YOU: Rule(("thread", "need", "prompt", "request", "waiting_for", "after_end", "summary"), ("thread",),
                         plain=True),
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
    Kind.UNANSWERED: Rule(("outcomes",), forbids=("started", "sent", "stopped", "answered", "finished")),
    # A free reply claimed an action while no thread was in view: nothing has been started, sent, stopped or answered.
    Kind.NOTHING_DONE: Rule((), forbids=("started", "sent", "stopped", "answered")),
    Kind.CORRECTION: Rule(("thread", "status", "need", "pending", "request", "waiting_for"), ("thread",), plain=True),
    # `decision` is approve or deny. `need` and `pending` say what is being answered, as a thread's status does.
    # An approval is confirmed only with a description of this request in `summary`, from the thread's own records.
    Kind.CONFIRM_RESPOND: Rule(("thread", "decision", "need", "pending", "summary", "asked"), ("thread",), True,
                               ("answered",), plain=True),
    Kind.RESPONDED: Rule(("thread", "decision", "need", "recap"), ("thread",), forbids=("finished",)),
    Kind.NOTHING_PENDING: Rule(("thread",), (), forbids=("answered",)),
    # `prompt` is the request's stored text, which `Speech.fill` swaps for a gated `request` as it does for a status.
    # `again`: the person was told this on this call already, so the line only says it still waits for them there.
    Kind.NEEDS_THE_APP: Rule(("thread", "decision", "need", "prompt", "request", "again"), ("thread",),
                             forbids=("answered",), plain=True),
    # A question a thread is waiting on, told with every option so the caller can pick one. `number` of `count` says which
    # of a request's questions this is, and `restated` why it is asked again: "unheard" when the reply fit no option, "changed"
    # when the thread now asks something else.
    Kind.ASK_QUESTION: Rule(("thread", "question", "options", "own_words", "number", "count", "restated"),
                            ("thread", "options"), True),
    # The answers about to be given, one per question in `questions`' order. Nothing is sent until a yes to this.
    Kind.CONFIRM_ANSWER: Rule(("thread", "questions", "answers"), ("thread", "answers"), True, ("answered",)),
    Kind.ANSWERED: Rule(("thread", "answers", "recap"), ("thread", "answers")),
    Kind.ANSWER_IN_APP: Rule(("thread",), ("thread",), forbids=("answered",)),
}
# A thread this voice started whose title T3 has not replaced is titled with the person's own words, cut off, which is
# no name to say. Such an act carries `begun_in`, the project it was started in, and no `thread`. When another thread
# the voice started in that project is just as unnamed, it also carries `begun_for`, what this one was started for.
# When it is the thread the call was opened on, it carries `on_call` instead, and the line calls it this thread.
for _kind, _rule in list(RULES.items()):
    if "thread" in _rule.slots:
        RULES[_kind] = replace(_rule, slots=(*_rule.slots, "begun_in", "begun_for", "on_call"),
                               named=(*_rule.named, "begun_in") if "thread" in _rule.named else _rule.named)
QUESTIONS = frozenset(kind for kind, rule in RULES.items() if rule.question)
# A status asked again on a call with nothing new in T3's record since the last one was said: the line says so rather
# than offering the same status as news.
NOTHING_NEW = "nothing new since they last heard its status"
# The acts whose words come from a read of the thread: the voice summarises it before it speaks them.
SUMMARISED = frozenset({Kind.THREAD_STATUS, Kind.DONE, Kind.NEEDS_YOU, Kind.CONFIRM_RESPOND})
MAX_NAMED = 3


def act(kind: Kind, **slots) -> Act:
    """An act with its present slots. A slot the rule does not name is a bug, so it raises."""
    rule = RULES[kind]
    unknown = set(slots) - set(rule.slots)
    if unknown:
        raise ValueError(f"{kind.value} has no slot {sorted(unknown)}")
    kept = {name: value for name, value in slots.items() if value is not None and value != ()}
    if "outcomes" in kept:
        kept["outcomes"] = [Outcome(value).value for value in kept["outcomes"]]
    for name in ("candidates", "available"):
        if name in kept:
            kept[name] = list(kept[name])[:MAX_NAMED]
    return Act(kind, kept)


_MARKDOWN = re.compile(r"[*_`#>|\[\]]+")
_CONTRACTIONS = (("n't", " not"), ("'re", " are"), ("'ve", " have"), ("'ll", " will"), ("'d", " would"),
                 ("'m", " am"), ("'s", " s"))
_SENTENCE = re.compile(r"[.!?]+(?=\s|$)")
_NOT_SPEECH = re.compile(r"^[\[{]|```|`|\*\*|https?://|\||\.\.\.|\N{HORIZONTAL ELLIPSIS}|</?\s*think\b|think\s*>",
                         re.IGNORECASE)
# Shell syntax, a flag, a path or a backslash: what a raw command reads like, which no spoken line holds.
_CODE = re.compile(r"&&|\|\||\$[({]|\d?>&\d|(?:^|\s)--?[A-Za-z][\w-]*|(?:^|\s)~?\.{0,2}/\w|\w/[\w.-]+\.\w+|\\\w")
_FIELD_TOKEN = re.compile(r"[\w-]+")
MAX_SENTENCES_PER_ACT = 2
# A thread or project name in quote marks. The call's voice answers a question that quotes its subject ("Yes, it
# is.") in place of saying it, in 9 of 9 rehearsed relays, and says the same question without the marks.
_NAMES = ("thread", "project", "candidates", "available", "begun_in")
_QUOTE = "[\"\N{LEFT DOUBLE QUOTATION MARK}\N{RIGHT DOUBLE QUOTATION MARK}]"


_TOKEN = re.compile(r"\d+(?:[.,:/]\d+)*|[%$€£]|\w+(?:'\w+)*")
# What can still be added to a token that ends the text so far: an apostrophe, or a number's separator or sign.
_MAY_CONTINUE = "'.,:/%$"


def _words(text: str, *, arriving: bool = False) -> list[str]:
    """The words of `text`, lower case, with markdown and punctuation gone and contractions spelled out. A number is one
    word with its own separators ("1,500" is not "1.500"), and "$" and "%" are words. Who does what is never changed:
    "I" stays "I", so a line must say a person's own words as they were said. With `arriving`, `text` is the start of a
    reply, and a last word that more characters could still change ("don'", "1,") is left out."""
    text = (text or "").lower().replace("’", "'").replace(
        "‘", "'")
    text = _MARKDOWN.sub(" ", text)
    found = list(_TOKEN.finditer(text))
    if arriving and found and (found[-1].end() == len(text) or text[found[-1].end()] in _MAY_CONTINUE):
        found.pop()
    words = []
    for match in found:
        word = match.group(0)
        for short, full in _CONTRACTIONS:
            word = word.replace(short, full)
        words.extend(word.split())
    return words


_DOTS = ".…"


def _word_spans(text: str) -> list[tuple[str, int, int]]:
    """`_words` with where each word sits in `text`."""
    flat = _MARKDOWN.sub(lambda match: " " * len(match.group(0)), text.lower().replace("’", "'").replace("‘", "'"))
    if len(flat) != len(text):
        return []
    spans = []
    for match in _TOKEN.finditer(flat):
        word = match.group(0)
        for short, full in _CONTRACTIONS:
            word = word.replace(short, full)
        spans.extend((part, match.start(), match.end()) for part in word.split())
    return spans


def masked(text: str, value: str) -> str:
    """`text` with each place `says` finds `value` replaced by "x". A name is said as given up to punctuation, so its
    own punctuation (a clipped title's ellipsis, the stops inside a spoken request) is not the line's. Only the
    value's own characters are masked: a character the value does not have, between or after its words, stays for
    the form checks to judge."""
    want = [word for word, _, _ in _word_spans(value)]
    have = _word_spans(text)
    if not want:
        return text
    own = {c for c in value if not c.isalnum() and not c.isspace()}
    if own & set(_DOTS):
        own |= set(_DOTS)
    trail = re.search(r"[^\w\s]+$", value.rstrip())
    out, at, i = [], 0, 0
    while i <= len(have) - len(want):
        if [word for word, _, _ in have[i:i + len(want)]] != want:
            i += 1
            continue
        start = have[i][1]
        for k in range(len(want)):
            end = have[i + k][2]
            if k + 1 < len(want):
                following = have[i + k + 1][1]
                if all(c.isspace() or c in own for c in text[end:following]):
                    continue
                out += [text[at:start], "x"]
                at, start = end, following
                continue
            if trail and set(trail.group()) <= set(_DOTS):
                while end < len(text) and text[end] in _DOTS:
                    end += 1
            elif trail and text.startswith(trail.group(), end):
                end += len(trail.group())
            out += [text[at:start], "x"]
            at = end
        i += len(want)
    return "".join(out) + text[at:]


def normalise(text: str) -> str:
    return " ".join(_words(text))


def recites(text: str, line: str, *, arriving: bool = False) -> bool:
    """`text` says `line`'s words and no others, up to case, punctuation, markdown and contractions. With `arriving`,
    `text` is the start of a reply: it only has to open `line`, and a last word that may be half said is not read."""
    said, want = _words(text, arriving=arriving), _words(line)
    return want[:len(said)] == said if arriving else said == want


def says(line: str, value: str) -> bool:
    """`value` appears in `line` as whole words, after `normalise`."""
    want = normalise(value)
    return bool(want) and f" {want} " in f" {normalise(line)} "


def _named_values(value) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    return [str(value)]


def in_order(text: str, values: Iterable[str]) -> bool:
    """`values` are all said in `text`, each after the one before, as whole words after `normalise`. Each is looked for
    from the end of the last, so a value that is also a word of the question ("strong") is found where it is listed."""
    words, at = normalise(text).split(), 0
    for value in values:
        want = normalise(value).split()
        found = next((i for i in range(at, len(words) - len(want) + 1) if words[i:i + len(want)] == want), None)
        if found is None:
            return False
        at = found + len(want)
    return True


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
        form = masked(form, value)
    if _NOT_SPEECH.search(form):
        problems.append("not plain speech")
    if any(RULES[a.kind].plain for a in acts) and _CODE.search(form):
        problems.append("reads code")
    if len(_SENTENCE.findall(form if form[-1] in ".!?" else form + ".")) > MAX_SENTENCES_PER_ACT * len(acts):
        problems.append("too long")
    names = [v for a in acts for name in _NAMES if name in a.slots for v in _named_values(a.slots[name])]
    if any(re.search(f"{_QUOTE}{re.escape(name)}{_QUOTE}", text, re.IGNORECASE) for name in names):
        problems.append("quotes around a name")
    expects_question = any(a.kind in QUESTIONS for a in acts)
    if expects_question and not text.endswith("?"):
        problems.append("not a question")
    if not expects_question and "?" in form:
        problems.append("unrequested question")
    for a in acts:
        for name in RULES[a.kind].named:
            if name in a.slots:
                for value in _named_values(a.slots[name]):
                    if not says(text, value):
                        problems.append(f"{a.kind.value} {name} missing: {value}")
    for a in acts:
        # "The second one" is read against the order the options were stored in, so they are told in that order.
        options = a.slots.get("options") if a.kind is Kind.ASK_QUESTION else None
        if options and all(says(text, value) for value in options) and not in_order(text, options):
            problems.append(f"{a.kind.value} options out of order: {', '.join(options)}")
    return problems


def field_names(line: str, acts: Iterable[Act], vocabulary: frozenset[str]) -> list[str]:
    """The names from the acts' own vocabulary (`vocabulary`: kind, field and enum names) that `line` says as words.
    A named value is the act's to say, so its own words are left out first; a name a thread reported ("load_config")
    is not in the vocabulary and is never refused."""
    acts = list(acts)
    form = line or ""
    for value in sorted((v for a in acts for name in RULES[a.kind].named if name in a.slots
                         for v in _named_values(a.slots[name])), key=len, reverse=True):
        form = masked(form, value)
    said = list(dict.fromkeys(t.casefold() for t in _FIELD_TOKEN.findall(form) if t.casefold() in vocabulary))
    return [f"says a field name: {name}" for name in said]


def check_names(line: str, acts: tuple[Act, ...], known_names: Iterable[str]) -> list[str]:
    """Match known titles on token boundaries, giving longer explicit titles precedence."""
    slots = [normalise(value) for act in acts for slot in act.slots.values() for value in _named_values(slot)]
    explicit = {normalise(value) for act in acts for key in ("thread", "project", "available", "candidates", "begun_in")
                if key in act.slots for value in _named_values(act.slots[key])}
    candidates = {normalise(name): name for name in known_names if name}
    for name in explicit:
        candidates.setdefault(name, name)
    text = f" {normalise(line)} "
    problems = []
    for key in sorted(candidates, key=lambda name: (-len(name), name)):
        if not key:
            continue
        pattern = f" {key} "
        if pattern not in text:
            continue
        if key not in explicit and not any(pattern in f" {slot} " for slot in slots if slot not in explicit):
            problems.append(f"unlisted title: {candidates[key]}")
        while pattern in text:
            text = text.replace(pattern, " " + "_" * len(key) + " ")
    return problems


def forbidden_claims(acts: Iterable[Act]) -> set[str]:
    """The status claims a line for `acts` may not make: each act's own, less any another act in it states."""
    claimed, forbidden = set(), set()
    for a in acts:
        status = str(a.slots.get("status"))
        forbidden |= set(RULES[a.kind].forbids) | set(STATUS_FORBIDS.get(status, ()))
        if a.kind in (Kind.CANCELLED, Kind.REFUSED):
            forbidden |= set(OP_FORBIDS.get(str(a.slots.get("op")), ()))
        if a.kind is Kind.UNANSWERED:
            claimed.update(OUTCOME_CLAIMS[value] for value in a.slots.get("outcomes", ()) if value in OUTCOME_CLAIMS)
        claimed |= set(CLAIMED.get(a.kind, ())) | (set(STATUS_CLAIMS.get(status, ())) if "status" in a.slots else set())
    return forbidden - claimed


OP_FORBIDS = {"start": ("started",), "tell": ("sent",), "stop": ("stopped",), "ask": ("stopped",),
              "answer": ("answered",)}
# What an act states as true, so another act in the same line may not forbid it.
CLAIMED = {Kind.LAUNCHED: ("started",), Kind.SENT_FOLLOWUP: ("sent",)}
STATUS_CLAIMS = {"finished": ("finished",), "completed": ("finished",), "interrupted": ("stopped",),
                 "cancelled": ("stopped",), "working": ("working",), "running": ("working",)}
