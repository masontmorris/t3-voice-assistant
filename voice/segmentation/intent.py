"""What one whole utterance is: a new request, a correction of an earlier turn, or chit-chat.

Read in code from the final transcript, with no decider call, so the end of a gated turn costs nothing more. The
measured failure it exists for: after "And what about Saturday?", the person said "That's not what I meant.", the
tools session delegated those words as a new request, and the assistant said it was running Saturday's weather again
and then that it was done. A correction is classified here only when the utterance as a whole is one; a request that
corrects itself mid-utterance ("Saturday, no, Sunday") is the decider's per-segment CORRECTION.

What it cannot see: a correction worded outside the patterns below ("hmm, the other one"), a correction that also
asks something new ("no, and set a timer"), and which earlier turn a correction means. "No, what's the time?" is a
request, because it asks its own question.
"""

import re

from voice.segmentation.contract import Intent, UtteranceIntent

LEADING_FILLER = re.compile(r"^(?:(?:um+|uh+|er|ah|oh|hmm+|well|sorry|wait)\b[\s,.!]*)+", re.IGNORECASE)
TRAILING = re.compile(r"(?:[\s,]+(?:instead|please|then))*[\s.!?,]*$", re.IGNORECASE)
NO = r"(?:no+|nope|nah)"
REJECTION = (
    rf"(?:{NO}[\s,.!]+)*(?:(?:that|it|this)(?:'s|s| is| was)\s+)?"
    r"(?:not what i (?:meant|mean|asked(?: for| about)?|wanted|said|was asking(?: for| about)?)|wrong|not right"
    r"|incorrect|not it|not that|not quite)"
    r"|(?:no+[\s,.!]*)*you (?:misunderstood|misheard|got (?:it|that|me) wrong)(?: me)?"
    rf"|(?:{NO}[\s,.!]*)+|wrong")
REJECTS = re.compile(REJECTION, re.IGNORECASE)
REJECTS_THEN = re.compile(rf"(?:{REJECTION}|actually)[\s,.!;]+(?P<rest>.+)", re.IGNORECASE)
RESTATED = re.compile(r"(?:i (?:meant|mean|said|asked for|asked about|wanted)|i was asking (?:for|about)"
                      r"|make (?:it|that)|change (?:it|that) to)[\s,]+(?P<rest>.+)", re.IGNORECASE)
NOT_THIS_BUT = re.compile(r"not\s+(?P<old>[^,]+?)(?:,|\s+but)\s+(?P<rest>.+)", re.IGNORECASE)
THIS_NOT = re.compile(r"(?P<rest>[^,]+),\s*not\s+(?P<old>[^,]+)", re.IGNORECASE)
ASKS = re.compile(r"(?:what|what's|whats|how|when|where|who|which|why|is|are|will|can|could|would|should|do|does|did"
                  r"|tell|give|find|show|set|send|book|check|look|get|play|call|text|remind|add|turn|open|search"
                  r"|let's|i want|i need|i'd like)\b", re.IGNORECASE)
CHAT_WORDS = frozenset("thanks thank you ok okay cool great nice awesome perfect got it sounds good alright all right "
                       "hello hi hey bye goodbye yes yeah yep sure wow so much very that's that is lovely".split())
MAX_REPLACEMENT_WORDS = 5


def _words(text):
    return re.findall(r"[a-z']+", text.lower())


def _chat(text):
    words = _words(text)
    return bool(words) and all(w in CHAT_WORDS for w in words)


def _clean(text):
    text = text.replace("\N{RIGHT SINGLE QUOTATION MARK}", "'").strip()
    return TRAILING.sub("", LEADING_FILLER.sub("", text))


def _replacement(rest):
    """The words a correction states instead, without "..., not Saturday" or a trailing "instead"."""
    return _clean(re.sub(r",\s*not\s+[^,]+$", "", _clean(rest), flags=re.IGNORECASE)) or None


def utterance_intent(text):
    """The intent of a whole final transcript."""
    said = _clean(text or "")
    if not said or _chat(said):
        return UtteranceIntent(Intent.CHITCHAT)
    if REJECTS.fullmatch(said):
        return UtteranceIntent(Intent.CORRECTION)
    rejected = REJECTS_THEN.fullmatch(said)
    if rejected:
        said = _clean(rejected["rest"])
    if match := RESTATED.fullmatch(said):
        return UtteranceIntent(Intent.CORRECTION, _replacement(match["rest"]))
    if rejected:
        if _chat(said):
            return UtteranceIntent(Intent.CHITCHAT)
        if REJECTS.fullmatch(said):
            return UtteranceIntent(Intent.CORRECTION)
        if ASKS.match(said) or len(_words(said)) > MAX_REPLACEMENT_WORDS:
            return UtteranceIntent(Intent.REQUEST)
        return UtteranceIntent(Intent.CORRECTION, _replacement(said))
    for pattern in (NOT_THIS_BUT, THIS_NOT):
        match = pattern.fullmatch(said)
        if match and len(_words(match["rest"])) <= MAX_REPLACEMENT_WORDS and not ASKS.match(match["rest"]):
            return UtteranceIntent(Intent.CORRECTION, _replacement(match["rest"]))
    return UtteranceIntent(Intent.REQUEST)
