"""What one whole utterance is: a new request, a correction of an earlier turn, or chit-chat.

Read in code from the final transcript, with no decider call, so the end of a gated turn costs nothing more. The
measured failure it exists for: after "And what about Saturday?", the person said "That's not what I meant.", the
tools session delegated those words as a new request, and the assistant said it was running Saturday's weather again
and then that it was done. A correction is classified here only when the utterance as a whole is one; a request that
corrects itself mid-utterance ("Saturday, no, Sunday") is the decider's per-segment CORRECTION.

A question about this call's own earlier turns ("What did you tell me about Chicago?") is chit-chat too, in
`asks_about_call`. Measured failure it exists for: on the twelve-question recall set every one of the twelve was
labelled a request, the decider was asked about it, and for one of them the decider dispatched a failing task whose
snapshot turned the reply into a refusal. The recall note reads the same predicate, so every question this file calls
conversation is one the call's own record answers.

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
REJECTED_FIRST = re.compile(r"^not\s+[^,]+\s*,\s*", re.IGNORECASE)
ASKS = re.compile(r"\b(?:what|what's|whats|how|when|where|who|which|why|is|are|will|can|could|would|should|do|does|did"
                  r"|tell|give|find|show|set|send|book|check|look|get|play|call|text|remind|add|turn|open|search"
                  r"|let's|i want|i need|i'd like)\b", re.IGNORECASE)
CHAT_WORDS = frozenset("thanks thank you ok okay cool great nice awesome perfect got it sounds good alright all right "
                       "hello hi hey bye goodbye yes yeah yep sure wow so much very that's that is lovely".split())
MAX_REPLACEMENT_WORDS = 5
CONVERSATION = re.compile(
    r"(?:hey[\s,]+)*(?:can|could) you hear me|"
    r"(?:what|which) (?:was|is) (?:my|the) (?:first|last|previous) (?:message|question|request)"
    r"(?: in (?:this|the) conversation)?",
    re.IGNORECASE,
)
CALL_HISTORY = re.compile(
    r"\b(?:did|do)\s+(?:i|you)\s+(?:ask|say|tell|mention|request)\b"
    r"|\b(?:what|which|who|when|where|how|did|do)\b[^?]*?"
    r"(?:\b(?:i|you)\s+(?:asked|said|told|mentioned|requested|answered)\b"
    r"|\b(?:questions?|requests?|things?|messages?)\s+(?:i|you)\s+(?:made|asked|said)\b)",
    re.IGNORECASE,
)
ACTION = re.compile(r"\b(?:please|can you|could you|would you|what|where|when|why|how|who|which|tell|give|find|show|set|send|book|check|look|get|play|call|text|remind|add|turn|open|search|fix|restart|translate)\b", re.IGNORECASE)


def _words(text):
    return re.findall(r"[a-z']+", text.lower())


def _chat(text):
    words = _words(text)
    return bool(words) and all(w in CHAT_WORDS for w in words)


def _clean(text):
    text = text.replace("\N{RIGHT SINGLE QUOTATION MARK}", "'").strip()
    return TRAILING.sub("", LEADING_FILLER.sub("", text))


def _replacement(rest):
    """The words a correction states instead, without the value it rejects. "Not Saturday, Sunday" and "Sunday, not
    Saturday" both state Sunday."""
    said = REJECTED_FIRST.sub("", _clean(rest))
    return _clean(re.sub(r",\s*not\s+[^,]+$", "", said, flags=re.IGNORECASE)) or None


def asks_about_call(text):
    """Whether the utterance asks about a turn of this call, so it is answered from the call and runs no task."""
    said = _clean(text or "")
    return bool(CONVERSATION.fullmatch(said) or CALL_HISTORY.search(said))


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
    if (re.match(r"(?:okay[\s,]+)?(?:this|we're|we are)\b", said, re.IGNORECASE)
            and not (text or "").strip().endswith("?") and not ACTION.search(said)):
        return UtteranceIntent(Intent.CHITCHAT)
    if match := RESTATED.fullmatch(said):
        return UtteranceIntent(Intent.CORRECTION, _replacement(match["rest"]))
    if rejected:
        if _chat(said):
            return UtteranceIntent(Intent.CHITCHAT)
        if REJECTS.fullmatch(said):
            return UtteranceIntent(Intent.CORRECTION)
        if asks_about_call(said):
            return UtteranceIntent(Intent.CHITCHAT)
        if ASKS.match(said) or len(_words(said)) > MAX_REPLACEMENT_WORDS:
            return UtteranceIntent(Intent.REQUEST)
        return UtteranceIntent(Intent.CORRECTION, _replacement(said))
    for pattern in (NOT_THIS_BUT, THIS_NOT):
        match = pattern.fullmatch(said)
        if match and len(_words(match["rest"])) <= MAX_REPLACEMENT_WORDS and not ASKS.match(match["rest"]):
            return UtteranceIntent(Intent.CORRECTION, _replacement(match["rest"]))
    if asks_about_call(said):
        return UtteranceIntent(Intent.CHITCHAT)
    return UtteranceIntent(Intent.REQUEST)
