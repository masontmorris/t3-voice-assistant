"""The thread view a local summariser reads, the prompts of each ladder step, and the code form check of a gist.

A view is the seam's read plus the thread's own messages, oldest first, as JSON:

    {"thread": title, "status": activity, "waiting_for": need or null, "pending": prompt or null,
     "messages": [{"from": "request" | "thread", "text": ...}], "left_out": earlier messages not shown}

`request` is a message sent to the thread (by the person or the agent that delegated it) and `thread` is the thread's
own reply, because the 1.7B mixed up who acted when it read only the last reply (3 of 30 in PR #64). Tool calls and
their output are left out: the seam's records keep them apart from messages, they are where code and paths come from,
and the thread's own messages already say what its tools found. The last-reply view is the seam's read today; a
larger view shows messages whole until its token budget runs out.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from typing import Callable, Sequence

from voice.t3 import types as T
from voice.t3.acts import Kind, act, check
from voice.t3.eval.summary_eval import cut_at_sentence
from voice.t3.lifecycle import need_of
from voice.t3.local_summary import (ADJACENT, EXAMPLES, FIELDS_HEAD, GIST_SCHEMA, MAX_WORDS, MESSAGE_FIELDS, NEED_FIELDS,
                                     NOT_SPOKEN, SHORT_ANSWERS, STATUS_FIELD, SYSTEM, UNDONE_EXAMPLE, VIEW_FIELDS)
from voice.t3.speech import summary_messages

# Below this many tokens a cut message says too little to be worth showing.
MIN_PIECE = 100


@dataclass(frozen=True)
class Message:
    role: str  # "user" or "assistant", as T3 stores it
    text: str


def fit(text: str, room: int, count: Callable[[str], int]) -> str | None:
    """`text` cut at a sentence to at most `room` tokens, or None when too little of it would fit to be worth it."""
    if count(text) <= room:
        return text
    if room < MIN_PIECE:
        return None
    low, high = 0, len(text)
    while low < high:
        mid = (low + high + 1) // 2
        if count(cut_at_sentence(text, mid)) <= room:
            low = mid
        else:
            high = mid - 1
    return cut_at_sentence(text, low) if low else None


def thread_view(state: T.ThreadState, history: Sequence[Message], budget: int | None,
                count: Callable[[str], int]) -> dict:
    """`state` with the thread's messages that fit in `budget` tokens of message text. None is the seam's read today:
    the last reply cut at 1,200 characters. With a budget, messages are whole: the last reply first, then the first
    request, since it says what the thread was asked to do, then the newest messages back. Only the message that
    crosses the budget is cut."""
    last = state.last_reply.text if state.last_reply is not None else None
    items = [m for m in history if m.text.strip()] if budget is not None else []
    if last is not None and items and items[-1].role == "assistant" and cut_at_sentence(items[-1].text) == last:
        last = items.pop().text
    shown: dict[int, str] = {}
    if budget is not None:
        last = fit(last, budget, count) if last is not None else None
        room = budget - (count(last) if last else 0)
        first = next((i for i, m in enumerate(items) if m.role == "user"), None)
        order = ([first] if first is not None else []) + [i for i in reversed(range(len(items))) if i != first]
        for i in order:
            text = fit(items[i].text, room, count)
            if text is None:
                if i == first:
                    continue
                break
            shown[i] = text
            room -= count(text)
            if text != items[i].text:
                break
    messages = [{"from": "request" if items[i].role == "user" else "thread", "text": shown[i]} for i in sorted(shown)]
    if last is not None:
        messages.append({"from": "thread", "text": last})
    pending = state.pending
    return {"thread": state.title, "status": state.activity.value,
            "waiting_for": need_of(pending.kind, pending.source) if pending is not None else None,
            "pending": pending.prompt if pending is not None else None,
            "messages": messages, "left_out": len(items) - len(shown)}


def replies_view(state: T.ThreadState, history: Sequence[Message], budget: int, count: Callable[[str], int]) -> dict:
    """The seam's read with the thread's earlier replies, newest first, within `budget` tokens in all. Requests are
    left out, since the 1.7B read them as the thread's own words, and the seam's last reply is marked as the current
    one, since it repeated results a later reply had replaced."""
    latest = state.last_reply.text if state.last_reply is not None else None
    replies = [m.text for m in history if m.role == "assistant" and m.text.strip()]
    if latest is not None and replies and cut_at_sentence(replies[-1]) == latest:
        replies.pop()
    room = budget - (count(latest) if latest else 0)
    earlier: list[str] = []
    for text in reversed(replies):
        piece = fit(text, room, count)
        if piece is None:
            break
        earlier.append(piece)
        room -= count(piece)
        if piece != text:
            break
    pending = state.pending
    return {"thread": state.title, "status": state.activity.value,
            "waiting_for": need_of(pending.kind, pending.source) if pending is not None else None,
            "pending": pending.prompt if pending is not None else None,
            "latest_reply": latest, "earlier_replies": earlier, "left_out": len(replies) - len(earlier)}


REPLY_FIELDS = (" `latest_reply` is the thread's latest reply and says where it stands now. `earlier_replies` are its "
                "earlier replies, newest first, and anything a later reply says replaces them. `left_out` counts "
                "earlier replies not shown. Long replies are cut.")


@dataclass(frozen=True)
class Step:
    """One rung of the ladder: each adds one change to the one before it."""

    name: str
    change: str
    sampling: bool = True
    system: str = "old"  # "old" is SUMMARY_SYSTEM over PR #64's records, "new" is SYSTEM
    view: bool = False
    examples: bool = False
    adjacent: bool = False
    max_tokens: int = 200
    json: bool = False
    status: bool = True
    undone: bool = False
    replies: bool = False
    words: int = MAX_WORDS
    # A grammar bound in words, so a forced stop falls between words and still ends the sentence.
    bound: int | None = None
    # The model reasons before it answers, as Liquid's guide recommends; the server keeps the reasoning out of the
    # answer, and the schema constrains only the answer.
    thinking: bool = False


LADDER = (
    Step("S1", "the card's sampling: temperature 0.1, top_k 50, repeat penalty 1.1"),
    Step("S2", "a system prompt with the conflicting lines removed", system="new"),
    Step("S3", "the thread view JSON with each field described", system="new", view=True),
    Step("S4", "worked examples on made-up threads as earlier chat turns", system="new", view=True, examples=True),
    Step("S5", "a short instruction beside the input", system="new", view=True, examples=True, adjacent=True),
    Step("S6", "max_tokens 60", system="new", view=True, examples=True, adjacent=True, max_tokens=60),
    Step("S7", "JSON schema output with no markdown or code characters", system="new", view=True, examples=True,
         adjacent=True, max_tokens=60, json=True),
)
S7 = LADDER[-1]
# The second set's arms, each against S7 on its own input: (a) is S7 itself.
FOLLOW_UP = (
    replace(S7, name="S8", change="the status left out of what the model sees", status=False),
    replace(S7, name="S9", change="an example of a run that ended with its work undone", undone=True),
    replace(S7, name="S10", change="the status left out and the undone example", status=False, undone=True),
    replace(S7, name="R1", change="the thread's own replies, latest marked current, about 1k tokens, no examples",
            examples=False, replies=True, max_tokens=100),
)
S9 = FOLLOW_UP[1]
# The third set's arms: S9 itself, S9 asking for 20 words, and that with a 25-word bound in the grammar.
SHORTER = (
    replace(S9, name="S11", change="20 words asked for in the prompt, the instruction and the examples", words=20),
    replace(S9, name="S12", change="20 words asked for, and the grammar bounded at 25 words ending a sentence",
            words=20, bound=MAX_WORDS),
)
S11 = SHORTER[0]
# The fourth set's arm: S11 with reasoning on and room for it in the output limit.
REASONING = (replace(S11, name="S13", change="reasoning on, with a 4,096-token limit", thinking=True, max_tokens=4096),)
STEPS = {step.name: step for step in (*LADDER, *FOLLOW_UP, *SHORTER, *REASONING)}


def schema(step: Step) -> dict:
    """The gist's JSON schema. With a bound, the string is at most `bound` space-separated words and its last word
    ends a sentence, so the grammar can stop it only between words, at a full stop."""
    if step.bound is None:
        return GIST_SCHEMA
    word = f"[^ {NOT_SPOKEN}]+"
    pattern = f"^({word} ){{0,{step.bound - 1}}}[^ {NOT_SPOKEN}]*[.!?]$"
    return {**GIST_SCHEMA, "properties": {"gist": {"type": "string", "pattern": pattern}}}


def _words(text: str, step: Step) -> str:
    return text.replace(f"at most {MAX_WORDS} words", f"at most {step.words} words")


def _user(view: dict, question: str | None, step: Step) -> str:
    # Records before the question, so a second question about the same thread reuses the cached prompt.
    body = json.dumps({"records": view, "question": question})
    return f"{body}\n\n{_words(ADJACENT, step)}" if step.adjacent else body


def _answer(gist: str, step: Step) -> str:
    if step.words < MAX_WORDS:
        gist = SHORT_ANSWERS.get(gist, gist)
    return json.dumps({"gist": gist}) if step.json else gist


def _shown(view: dict, step: Step) -> dict:
    return view if step.status else {key: value for key, value in view.items() if key != "status"}


def messages(step: Step, state: T.ThreadState, view: dict, question: str | None) -> list[dict]:
    if step.system == "old":
        return summary_messages(state, question)
    if not step.view:
        return [{"role": "system", "content": SYSTEM}, summary_messages(state, question)[1]]
    fields = (FIELDS_HEAD + (STATUS_FIELD if step.status else "") + NEED_FIELDS
              + (REPLY_FIELDS if step.replies else MESSAGE_FIELDS))
    out = [{"role": "system", "content": _words(SYSTEM, step) + fields}]
    if step.examples:
        for example, asked, gist in (*EXAMPLES, *((UNDONE_EXAMPLE,) if step.undone else ())):
            out += [{"role": "user", "content": _user(_shown(example, step), asked, step)},
                    {"role": "assistant", "content": _answer(gist, step)}]
    out.append({"role": "user", "content": _user(_shown(view, step), question, step)})
    return out


def gist_of(text: str, step: Step) -> tuple[str | None, str | None]:
    """The gist and, when the output is not one, why."""
    if not step.json:
        return " ".join(text.split()) or None, None
    try:
        gist = json.loads(text)["gist"]
    except (ValueError, KeyError, TypeError) as exc:
        return None, f"not the schema's JSON: {type(exc).__name__}"
    return " ".join(str(gist).split()) or None, None


def form(gist: str | None, state: T.ThreadState) -> list[str]:
    """The code form check of a gist: `acts.check` for a status act with no thread slot, since the live writer says
    the name, and the 25-word limit."""
    if not gist:
        return ["empty"]
    pending = state.pending
    status = act(Kind.THREAD_STATUS, status=state.activity.value,
                 need=need_of(pending.kind, pending.source) if pending is not None else None,
                 waiting_for=pending.kind.value if pending is not None else None)
    problems = check(gist, [status])
    words = len(re.findall(r"\S+", gist))
    if words > MAX_WORDS:
        problems.append(f"{words} words")
    return problems

