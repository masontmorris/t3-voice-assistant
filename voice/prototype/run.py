"""The local voice-assistant prototype, end to end: a WAV in, a spoken reply and a record of every event out.

    python -m voice.prototype.run CLIP.wav [CLIP.wav ...] [--eou gated|handoff|decider] [--inject a|b|c]
        [--segmenter hybrid|baseline] [--fresh] [--lfm-ctx N] [--out DIR] [--mock-tools]
        [--decider-cache DIR --decider-budget N]

The clip streams at real-time pace into two sessions on one llama-server: a `realtime` voice session and a
`?intent=transcription` session. Transcription deltas feed the segmenter, the segmenter's dispatches feed the mock
task service, and at the turn boundary the task snapshot goes into the voice session before it replies. Mock tasks
that finish after the reply reach the voice as a `task_update` at the next boundary. Live mode passes its own
long-lived voice session as `voice=`, so every turn of a call adds to one conversation.

--eou picks how the turn runs and ends. gated, the default, is plan A. During speech, a task the orchestrator is
about to dispatch runs only when the task gate (voice/segmentation/gate.py) passes it; otherwise it waits. There is
no decider call at the end of the utterance and no step that settles tasks. A third, text-only LFM3 session on the
same server reads the snapshot: every task with its task_id and status, a note on each task that has not run, and
the unreviewed end of the transcript. It may call run_task, delegate, cancel_task or amend_task, and its calls are
final. The task service checks only that each call is well formed. The voice session then hears the updated
snapshot as in c and replies, so it never sees a tool call of its own. The segmenter's close says what the whole
utterance is (voice/segmentation/intent.py), and only a request may start a new task. A correction of an earlier
turn skips the tools session: nothing it said runs, and either the call's newest task runs again with the value the
person stated ("no, Sunday"), or the snapshot asks the voice to find out what to change. handoff is the earlier
design with no gate: a close rule settles the tasks, side effects stay held, and LFM3 gets the tail. decider is the
earliest turn: one more decider call on the final transcript, then the snapshot by --inject. gated and handoff need
the hybrid and --inject c.

Read-only weather and time tasks make real lookups through `voice/tools/registry.py`: Open-Meteo, and the
`mcp-server-time` MCP server over stdio, which runs for the whole run. VOICE_DEFAULT_PLACE is the place for a request
that names none. --mock-tools sends every task to the mock worker instead.

--inject picks how the snapshot enters the voice session under --eou decider:
  a  a client-made `function_call` item plus its `function_call_output`
  b  the output of the model's own `delegate` or `get_task_status` call (the first response must call a tool);
     --status-only offers just `get_task_status`
  c  a user text message with a fixed machine prefix

--decider picks the decider: liquid (Liquid AI's hosted d1.2, the default, or $VOICE_DECIDER), cloud
(pplx-decider-flash on OpenRouter, kept as the fallback) or d1 (d1-2B on a second local llama-server, see
voice/local_decider/). --reuse asks only the questions whose answers it has not kept.
--lfm-ctx sets LFM3's total context (-c), 32768 by default.

--segmenter picks the segmenter, the hybrid by default. The segmenter takes transcript updates one at a time. When
newer updates queue behind a decision, only the newest is processed, and the timeline logs the dropped revisions.

--fresh sends every cloud or liquid decider request live, even when it is cached, so timings include each call.

Needs the GPU (run it inside the lease). The cloud decider needs VOICE_ENV_FILE or OPENROUTER_API_KEY and the liquid
decider LIQUID_API_KEY in it, for calls that miss the cache. Writes CLIP.json, CLIP_reply.wav, and CLIP_update.wav to
--out.
"""

import argparse
import asyncio
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import asdict
from pathlib import Path

import numpy as np

from voice.live.facts import names_machinery, speaks_of_person, ungrounded
from voice.live.recall import recall_note
from voice.local_decider.bench import gpu_used_mib
from voice.local_decider.client import decider as make_decider, default_decider
from voice.local_decider.reuse import ReusingClient
from voice.prototype.realtime import MODEL_RATE, VOICE_HOME, Server, Session, read_pcm16, write_pcm16
from voice.prototype.segmenter import (DECIDER_BUDGET, MAX_429_SHARE, SEGMENTERS, FreshClient, coalesce, live_calls,
                                       rate_limited_share, use_decider_cache)
from voice.prototype.tasks import ASK_INSTEAD_LINE, ASK_WHAT_CHANGED, MockTaskService
from voice.segmentation.contract import Intent
from voice.segmentation.gate import PLAN_A
from voice.segmentation.intent import utterance_intent
from voice.prototype.transcript import TranscriptAdapter
from voice.tools.registry import ToolCall, open_registry

EVIDENCE = VOICE_HOME / "evidence" / "prototype"
PERSONA = (
    "You are a friendly voice assistant. A separate task service carries out what the user asks for. "
    "After the user speaks, you receive a task status snapshot as JSON. Talk to the user directly, as \"you\", in "
    "one or two short sentences, and say only what the snapshot says. When work is running, briefly acknowledge it, "
    "such as 'Checking that', without repeating the request or inventing details. running means the task has started. pending "
    "means it is waiting on the service, not waiting for the user's permission. An explicit request in this turn "
    "authorizes carrying it out, including external actions; do not ask again just because it is external. "
    "completed means it is done, and you may say its result_summary. failed means it could not be "
    "done, and you may say its result_summary. held_for_confirmation "
    "means you need the user's OK before doing it. needs_clarification means you must ask the user. Never say a task "
    "is done unless its status is completed, never promise a step the snapshot does not show, and never make up a "
    "result."
)
# Live mode's persona, for a call whose only machine text is a `task_note`. Measured on Mason's 37 recorded turns:
# a longer persona, or one that describes the call ("a live call with one person"), made the model narrate itself in
# the third person from the first turn, and a call's own text in the instructions made it answer that text instead of
# the audio.
NOTE_PERSONA = (
    "You are the voice of a personal assistant, on a call with the person you help. Answer what they just said in "
    "one or two short, warm sentences, speaking as \"I\" to \"you\". A bracketed note is private, about a lookup you "
    "started for them: follow it for that one reply, never read it aloud, and never repeat an old result unless "
    "they ask. With no note, just talk with them, and don't state numbers, dates or facts you weren't given."
)
FORMAT_LINES = {
    "a": "\n\nRespond with interleaved text and audio.",
    "b": " Always call a tool first.\n\nRespond with function calls, and interleaved text and audio.\n\nList of tools:",
    "c": "\n\nRespond with interleaved text and audio.",
}
TOOLS = [
    {"type": "function", "name": "delegate", "description": "Hand a request to the task service.",
     "parameters": {"type": "object", "properties": {"instruction": {"type": "string"}}, "required": ["instruction"]}},
    {"type": "function", "name": "get_task_status", "description": "Get the status of the user's tasks.",
     "parameters": {"type": "object", "properties": {}}},
]
HANDOFF_INSTRUCTIONS = (
    "You keep a voice assistant's task list up to date. You receive a task service snapshot as JSON. It lists every "
    "task, each with its task_id, and final_transcript, which is everything the user said. The service has read all "
    "of it except unreviewed_speech, the user's last words. Act on unreviewed_speech alone. If it asks for something "
    "new that no task covers, call delegate with just that request. If it takes back a task, call cancel_task with "
    "that task's task_id. If it changes a task, call amend_task with that task's task_id and the change. If it does "
    "none of these, or there is no unreviewed_speech, call no tool and answer none."
    "\n\nRespond with function calls, and interleaved text and audio.\n\nList of tools:"
)
GATED_INSTRUCTIONS = (
    "You decide what happens to a voice assistant's tasks now that the user has finished speaking. You receive a "
    "task service snapshot as JSON. It lists every task with its task_id and status, and final_transcript, which is "
    "everything the user said. A running or completed task has already started. A pending or held_for_confirmation "
    "task has not run, and its note says why. unreviewed_speech, when present, holds the user's last words, which "
    "the service has not read. Your calls are final. The user's explicit request authorizes carrying it out, "
    "including external actions. Low service confidence is not a request for user confirmation. "
    "Read the complete final_transcript for context before acting on its unreviewed tail. A fragment alone is not a "
    "request. Greetings, reactions, questions about this conversation, and descriptions of a problem are not tasks; "
    "the voice assistant answers them directly. "
    "For a task that has not run, call run_task if the user wants it "
    "as it is, amend_task with the change if the user changed it, or cancel_task if the user took it back. If "
    "unreviewed_speech asks for something new that no task covers, call delegate with just that request. If the "
    "user took back a running task, call cancel_task. If nothing needs doing, call no tool and answer none."
    "\n\nRespond with function calls, and interleaved text and audio.\n\nList of tools:"
)
HANDOFF_TOOLS = [
    {"type": "function", "name": "delegate", "description": "Hand a new request from the user to the task service.",
     "parameters": {"type": "object", "properties": {
         "instruction": {"type": "string", "description": "The request, in the user's words."},
         "side_effect": {"type": "boolean",
                         "description": "true if it sends, buys, books, posts, deletes, or changes something."}},
         "required": ["instruction", "side_effect"]}},
    {"type": "function", "name": "cancel_task", "description": "Cancel a task the user took back.",
     "parameters": {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]}},
    {"type": "function", "name": "amend_task", "description": "Change a task the user corrected.",
     "parameters": {"type": "object", "properties": {"task_id": {"type": "string"}, "new_request": {"type": "string"}},
                    "required": ["task_id", "new_request"]}},
]
GATED_TOOLS = [
    {"type": "function", "name": "run_task", "description": "Start a task that has not run yet.",
     "parameters": {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]}},
    *HANDOFF_TOOLS,
]
TOOL_SESSIONS = {"handoff": (HANDOFF_INSTRUCTIONS, HANDOFF_TOOLS), "gated": (GATED_INSTRUCTIONS, GATED_TOOLS)}
SNAPSHOT_PREFIX = "[task service snapshot, machine-generated, not spoken by the user] "
UPDATE_PREFIX = "[task service update, machine-generated, not spoken by the user] "
FACT_CHECK_PREFIX = "[fact check, machine-generated, not spoken by the user] "
HISTORY_TURNS = 8
FACT_CHECK_REJECTED = {
    "type": "fact_check",
    "result": "rejected",
    "reason": "the last reply named a day, date, time, number or place that nothing in this call said or stated, "
              "described this note instead of answering, or spoke of the person in the third person",
    "instruction": "Answer again as yourself, to the person as you, using only what the person said or this note "
                   "states. If you do not have a value, say plainly that you do not have it. Never guess one, and "
                   "never talk about this note.",
}
RUNNING_REJECTED = {
    "type": "fact_check",
    "result": "rejected",
    "reason": "a task for the person's request is already running, so the reply may say it is checking it, not that "
              "it lacks access or cannot do it",
    "instruction": "Answer again as yourself, in one short sentence. A task for the person's request is already "
                   "running, so say you are checking it. Use only values the person said or this note states. Do not "
                   "say you lack access, cannot do it, or suggest another app or service.",
}
RUNNING_REFUSAL = re.compile(
    r"\b(?:i (?:do not|don't) have (?:access|real[- ]time|live|the ability)"
    r"|i (?:cannot|can't|could not|couldn't) (?:access|provide|check|get|look|tell|give|help)"
    r"|i(?:'m| am) (?:unable|not able)"
    r"|no (?:access to )?(?:real[- ]time|live) (?:data|information|weather)"
    r"|you (?:might|may|should) (?:want to |need to )?(?:check|use|try|consult)"
    r"|you(?:'ll| will) need to)\b", re.IGNORECASE)
# What a turn says when the check fails twice. Under eight words, in the persona's own register, naming no fact and
# no machinery. Measured context: 1 of 157 spoken replies on disk contains "note", which the screen below refuses.
FALLBACK_LINE = "Sorry, I can't confirm that right now."
NOTE_OPEN = "[Note for you, not said by the person: "
RETRY_NOTE = (NOTE_OPEN + "your last reply was not spoken. Reply again in one short sentence, as \"I\", talking to "
              "them, using only what they said and what a note gave you.]")
STILL_RUNNING_NOTE = (NOTE_OPEN + "the lookup is still running and has no answer yet. Just tell them you're "
                      "checking, in a few words.]")
ASK_NOTE = (NOTE_OPEN + "they said your last answer was not what they wanted, and did not say what they want "
            "instead. Ask them what they would like instead, in one short sentence.]")
SYNTHETIC_CALL_ID = "snapshot_1"


class Fallback:
    """The one short line a turn may speak when the check fails twice, and the audio for it.

    One per turn, shared by the snapshot reply and the task update, so a turn that loses both says it once."""

    def __init__(self, pcm=b"", line=FALLBACK_LINE):
        self.line, self.pcm, self.spoken = line, pcm, False


def reply_summary(session, mark, since, rate):
    events = session.events[mark:]
    done = next(e for e in events if e["type"] in ("response.done", "error"))
    response = done.get("response", {})
    audio_events = [e for e in events if e["type"] == "response.output_audio.delta"]
    pcm = b"".join(chunk for index, chunk in session.audio if index >= mark)
    return {
        "text": "".join(e.get("delta", "") for e in events if e["type"] == "response.output_text.delta"),
        "status": response.get("status") or done.get("error", {}).get("type"),
        "function_calls": [o for o in response.get("output", []) if o.get("type") == "function_call"],
        "first_audio_after_s": audio_events[0]["_t"] - since if audio_events else None,
        "speech_s": len(pcm) / 2 / rate,
        "done_at": done["_t"],
    }, pcm


async def respond(session, rate, since, **response):
    mark = len(session.events)
    await session.send(type="response.create", **({"response": response} if response else {}))
    await session.wait_for("response.done", "error", after=mark)
    return reply_summary(session, mark, since, rate)


def retry_note(found, running):
    """The note a refused reply is answered with. A reply that stated values nothing gave it is told which, because
    asked again with a general note the model repeated the same sentence (measured: callfix3 replay fix1, turn 2)."""
    if running:
        return STILL_RUNNING_NOTE
    values = [fact.split(" ", 1)[1] for fact in found.get("ungrounded", ())]
    if not values:
        return RETRY_NOTE
    return (f"{NOTE_OPEN}your last reply was not spoken, because nothing gave you {', '.join(values)}. Reply again "
            "without that. If they asked for something you were not given, say you can't look that up.]")


def lookup_name(entry):
    """What a note calls a task: its tool and place, never the person's words, which the model would repeat."""
    tool = entry.get("capability")
    if tool in (None, "agent"):
        return "the lookup"
    place = (entry.get("arguments") or {}).get("place")
    return f"the {tool} lookup" + (f" for {place}" if place else "")


def task_note(entry):
    """One task's state as a private direction to the voice model, or None for a state that needs no words.

    A note tells the model what to do with the state, because the model treats the newest text it reads as the
    thing to answer. Measured on Mason's recorded turns: a bare status line was read aloud, and this wording was
    not. A task that is pending or needs clarification gets no note, so the model answers the person's own words."""
    status, name = entry["status"], lookup_name(entry)
    if status == "running":
        return f"{NOTE_OPEN}{name} is running and has no answer yet. Tell them you're checking, in a few words.]"
    if status == "completed":
        return (f"{NOTE_OPEN}{name} finished. Result: {entry['result_summary']} "
                "Tell them this result in one or two sentences.]")
    if status == "failed":
        return f"{NOTE_OPEN}{name} could not be done. Tell them briefly that you couldn't check that.]"
    if status == "cancelled":
        return f"{NOTE_OPEN}{name} was dropped, as they asked. Tell them so in a few words.]"
    return None


def task_notes(entries):
    """The notes for a turn's tasks as one text, or None when no task needs words."""
    lines = [note for note in map(task_note, entries) if note]
    return "\n".join(lines) or None


def is_running(snapshot):
    """Screen refusals only when no failed task could make them truthful."""
    statuses = {task.get("status") for task in (snapshot or {}).get("tasks", ())}
    return "running" in statuses and "failed" not in statuses


def refused(text, sources, running=False):
    """Why a reply may not be spoken, by screen. Every list is empty for a reply that may."""
    found = {"ungrounded": [f"{fact.kind} {fact.surface}" for fact in ungrounded(text, sources)],
             "machinery": names_machinery(text), "third_person": speaks_of_person(text)}
    if running and (refusals := [match.group(0) for match in RUNNING_REFUSAL.finditer(text or "")]):
        found["contradicts"] = refusals
    return found


SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
PAUSE_SEARCH = 0.2
PAUSE_WINDOW_S = 0.1
PAUSE_RATIO = 0.25


def cut_at_pause(pcm, rate, fraction):
    """`pcm` up to the pause nearest `fraction` of the way through it, or None when there is no pause there.

    The model's text and audio carry no alignment, so the end of a sentence is estimated from its share of the
    reply's characters and then moved to the quietest 100 ms within a fifth of the clip either side. Only a window
    well below the clip's own level counts as a pause, so a reply spoken without one is never cut mid-word."""
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
    window, hop = int(rate * PAUSE_WINDOW_S), max(1, int(rate * 0.01))
    if len(samples) < 4 * window:
        return None
    starts = np.arange(0, len(samples) - window, hop)
    squares = np.concatenate(([0.0], np.cumsum(samples ** 2)))
    level = np.sqrt((squares[starts + window] - squares[starts]) / window)
    centre = fraction * len(samples)
    near = np.abs(starts + window / 2 - centre) <= PAUSE_SEARCH * len(samples)
    if not near.any():
        return None
    quietest = starts[near][np.argmin(level[near])]
    if level[near].min() > PAUSE_RATIO * np.median(level):
        return None
    return pcm[:int(quietest + window / 2) * 2]


def spoken_part(reply, pcm, rate, sources, running):
    """The leading sentences of a refused reply that pass every screen, with their audio, or None.

    Measured on the callfix3 probes: a reply to a running lookup often opened with "I'm checking the weather in
    Durham." and then invented a forecast. Refused whole and asked again, the model repeated itself and the turn
    said nothing. The opening sentence was the right reply, so it is spoken and the rest is dropped."""
    sentences = SENTENCE_END.split((reply["text"] or "").strip())
    kept = []
    for sentence in sentences[:-1]:
        if any(refused(" ".join([*kept, sentence]), sources, running).values()):
            break
        kept.append(sentence)
    if not kept:
        return None
    text = " ".join(kept)
    part = cut_at_pause(pcm, rate, len(text) / max(1, len(reply["text"].strip())))
    if part is None:
        return None
    return {**reply, "text": text, "speech_s": len(part) / 2 / rate, "dropped": reply["text"][len(text):].strip()}, part


async def answer(session, rate, since, sources, log, fallback=None, running=False, retry_item=None, escalate=None,
                 keep_opening=False, **response):
    """A reply that states only facts the call gave it, and never names the machinery.

    `sources` is what the assistant may say: the person's words in this call and the newest machine note, never its
    own earlier replies. A reply that names a day, date, time, number or place no source states, that talks about
    the note, the check or the task list, that speaks of the person in the third person ("the user wants"), or that
    says it lacks access while `running` holds, is not spoken: the turn says so in the record, sends one machine note,
    and asks again once. If the second answer is bad too, the turn speaks `fallback`'s one short line, and only if that
    turn has not said it yet. Nothing is substituted for a wrong value, because a different wrong value is worse than
    saying less.

    `retry_item` replaces the machine note a refused reply is answered with; given a function, it is called with
    what the screens found and returns the item. `escalate`, when given, is called
    with the facts a reply stated that nothing gave it: it may start a lookup for the person's words and return
    the note for it, so a fact the model does not have is looked up instead of refused. The turn then says it is
    checking, and the result arrives as a task update. With `keep_opening`, a refused reply whose opening
    sentences pass is spoken up to there and not asked again (`spoken_part`)."""
    reply, pcm = await respond(session, rate, since, **response)
    if not any((found := refused(reply["text"], sources, running)).values()):
        return reply, pcm
    if keep_opening and (part := spoken_part(reply, pcm, rate, sources, running)) is not None:
        log("fact_check", reply=reply["text"], **found, kept=part[0]["text"])
        return part
    log("fact_check", reply=reply["text"], **found, asked_again=True)
    looked_up = None
    if escalate is not None and found["ungrounded"] and not found["machinery"] and not found["third_person"]:
        looked_up = escalate(found["ungrounded"])
    if looked_up is not None:
        log("escalated", note=looked_up)
        sources, running = [*sources, looked_up], True
        await create_item(session, plain_text(looked_up))
    elif retry_item is not None:
        await create_item(session, retry_item(found) if callable(retry_item) else retry_item)
    else:
        await create_item(session, user_text(FACT_CHECK_PREFIX, RUNNING_REJECTED if running else FACT_CHECK_REJECTED))
    retry, retry_pcm = await respond(session, rate, since, **response)
    if not any((again := refused(retry["text"], sources, running)).values()):
        return retry, retry_pcm
    if keep_opening and (part := spoken_part(retry, retry_pcm, rate, sources, running)) is not None:
        log("fact_check", reply=retry["text"], **again, kept=part[0]["text"])
        return part
    blocked = {"reply": retry["text"], **again}
    log("fact_check", **blocked, blocked=True)
    if fallback is None or fallback.spoken:
        return {**retry, "text": "", "blocked": blocked}, b""
    fallback.spoken = True
    log("fallback", text=fallback.line, blocked=blocked)
    return {**retry, "text": fallback.line, "blocked": blocked, "fallback": True}, fallback.pcm


def fixed_reply(line, pcm, rate, after_s):
    """A reply the turn speaks without asking the voice model: `line` and its pre-rendered `pcm`. With no clip the
    record still says the line, and nothing plays."""
    return {"text": line, "status": "fixed", "fixed": True, "function_calls": [],
            "first_audio_after_s": after_s if pcm else None, "speech_s": len(pcm) / 2 / rate, "done_at": None}


async def create_item(session, item):
    """Sends one item and returns what the server answered: the added item or an error."""
    mark = len(session.events)
    await session.send(type="conversation.item.create", item=item)
    try:
        answer = await session.wait_for("conversation.item.added", "conversation.item.done", "error", after=mark,
                                        timeout=5)
    except asyncio.TimeoutError:
        return {"item_type": item["type"], "accepted": None, "answer": "no event in 5 s"}
    return {"item_type": item["type"], "accepted": answer["type"] != "error",
            "answer": answer.get("error") if answer["type"] == "error" else answer["type"]}


def user_text(prefix, payload):
    return {"type": "message", "role": "user",
            "content": [{"type": "input_text", "text": prefix + json.dumps(payload)}]}


def plain_text(text):
    """A machine item whose body is already words, so a short note does not arrive as JSON."""
    return {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}


PERSON_FIELDS = frozenset({"final_transcript", "unreviewed_speech", "segment_text", "instruction", "description",
                           "clarification_needed"})
CONTRACTIONS = (("n't", " not"), ("'re", " are"), ("'s", " is"), ("'ll", " will"), ("'m", " am"), ("'ve", " have"),
                ("'d", " would"))


def normalised(text):
    """Lower case, straight quotes, contractions spelled out, and words joined by single spaces."""
    text = re.sub("[\N{LEFT SINGLE QUOTATION MARK}\N{RIGHT SINGLE QUOTATION MARK}]", "'", text.lower())
    for short, full in CONTRACTIONS:
        text = text.replace(short, full)
    return " ".join(re.findall(r"\w+", text))


def overlaps(text, transcript):
    """True when `text` holds the person's words or a run of two or more of them, after normalising both.

    A single word is not compared against the transcript, so a status or a place name the person happened to say
    stays."""
    words, said = normalised(text), normalised(transcript or "")
    if not words or not said:
        return False
    return f" {said} " in f" {words} " or (" " in words and f" {words} " in f" {said} ")


def without_utterance(payload, transcript, within=None):
    """The payload with the person's words removed, at any depth.

    Fields that hold the person's words or the decision model's copy of them go whatever their content: the
    PERSON_FIELDS and the `text` of each entry the decider `read`. Any other string, in a dict or a list, goes when it
    overlaps the transcript. A status, a task_id, a note and a result summary stay."""
    if isinstance(payload, dict):
        return {key: without_utterance(value, transcript, key) for key, value in payload.items()
                if key not in PERSON_FIELDS and not (key == "text" and within == "read")
                and not (isinstance(value, str) and overlaps(value, transcript))}
    if isinstance(payload, list):
        return [without_utterance(item, transcript, within) for item in payload
                if not (isinstance(item, str) and overlaps(item, transcript))]
    return payload


def shown(payload, snapshot_mode, transcript, update=False):
    """What a machine item after the turn's audio carries under `snapshot_mode`, or None when no item goes in.

    before-audio sends its snapshot ahead of the audio, so it sends none after it, but it still sends task updates."""
    if snapshot_mode == "reply-no-utterance":
        return without_utterance(payload, transcript)
    if snapshot_mode == "reply" or (update and snapshot_mode == "before-audio"):
        return payload
    return None


def with_call_turns(item, prior_utterances, keep=HISTORY_TURNS):
    """The snapshot plus the person's own earlier turns, numbered oldest first, so a question about what they asked
    has the call's text in the item the model already reads for task state."""
    recent = list(prior_utterances[-keep:])
    first = len(prior_utterances) - len(recent) + 1
    return {**item, "omitted_turns": first - 1,
            "call_turns": [{"turn": first + i, "user": text} for i, text in enumerate(recent)]}


class SessionRefused(RuntimeError):
    """llama.cpp answered a session.update with an error, so the session does not have those settings."""


async def update_session(session, settings):
    mark = len(session.events)
    await session.send(type="session.update", session=settings)
    answer = await session.wait_for("session.updated", "error", after=mark)
    if answer["type"] == "error":
        raise SessionRefused(f"{session.name} session.update refused: {answer.get('error')}")


def parse_arguments(call):
    try:
        arguments = json.loads(call.get("arguments") or "{}")
    except ValueError:
        return {}
    return arguments if isinstance(arguments, dict) else {}


async def wait_for_work(service, timeout_s):
    """Sleep until the next mock task is due, or until a running lookup returns, whichever comes first."""
    calls = [asyncio.wrap_future(call) for call in service.pending_calls()]
    if calls:
        await asyncio.wait(calls, timeout=max(0.0, timeout_s))
    else:
        await asyncio.sleep(max(0.0, timeout_s))


class WavInput:
    """A clip as a turn's audio, streamed at real-time pace into both sessions."""

    def __init__(self, wav):
        self.name, self.stem = wav.name, wav.stem
        self.pcm, self.rate, self.audio_s = read_pcm16(wav)

    async def stream(self, voice, asr):
        (_, voice_end), (_, asr_end) = await asyncio.gather(voice.stream(self.pcm, self.rate),
                                                            asr.stream(self.pcm, self.rate))
        return max(voice_end, asr_end)


def close_with_decider(segmenter, update):
    decision, error = None, None
    if utterance_intent(update.text).intent == Intent.REQUEST:
        try:
            decision = segmenter.update(update)
        except Exception as exc:
            error = repr(exc)
    close = segmenter.close(update, settle=False)
    close["final_decision"] = decision
    close["final_decision_error"] = error
    return close


async def run_clip(source, inject, status_only, url, decider, delay_s, out, partial, segmenter_name="hybrid",
                   tools=None, eou="gated", persona=PERSONA, on_voice_event=None, voice=None,
                   snapshot_mode="reply", prior_snapshot=None, speak=None, prior_utterances=(),
                   fallback_pcm=b"", prior_tasks=(), ask_pcm=b"", simulate_tasks=True, final_decider=False,
                   recall="off", context=(), prior_results=()):
    """One turn. `source` is a WavInput or voice/live's microphone turn: it has `name`, `stem`, `rate`, `audio_s`
    once streamed, and `stream(voice, asr)`, which returns the clock time the input ended.

    `voice` is an already-open voice session the caller keeps across turns, as live mode does for a call. The turn
    then adds its audio, its injections and its replies to that session's conversation instead of opening one, and
    leaves it open. Its session settings are sent only when they change, so a reused session keeps its history. The
    per-turn `asr` and `tools` sessions are still opened and closed here. A refused session.update raises SessionRefused with the server's error.

    `snapshot_mode` says where the task snapshot enters the voice session. "reply" is the shipped behaviour: one user
    item built after the turn's tools ran, immediately before the reply. "before-audio" sends the caller's
    `prior_snapshot`, the call's task state when the turn started, before the turn's audio and answers with no item
    after the tools turn; the turn's own transcript is then not in the text the model reads. "reply-no-utterance" is
    "reply" with the person's words dropped from the snapshot and from the task update (see `without_utterance`), so
    the item still carries the task list, its statuses and its results. "off" sends no machine text at all, whatever
    `eou` and `inject` are. The task service, the tool session and its snapshot are the same in all four.

    `speak` takes the reply text and PCM that passed the fact check, when the caller streams the reply itself, as live
    mode does. The turn writes `CLIP_reply.wav` and `CLIP_update.wav` either way, so a record is readable without the
    client's copy. `prior_utterances` is what the person said earlier in the call, which the fact check needs because
    the shipped snapshot no longer carries their words. The last `HISTORY_TURNS` of them also go into the snapshot item
    as numbered `call_turns`, so a question about what was said earlier has the call's own text to read.

    `recall` says how a question about the call reaches the model. "off" is the shipped behaviour, the numbered
    `call_turns` inside the snapshot item. "note" keeps that list and adds one plain sentence naming the turn the
    question asks about (voice/live/recall.py) as the newest item before the reply.

    `snapshot_mode` "note" is live mode's default. The voice session then holds only the call itself: each turn's
    audio and reply, plus one `task_notes` text after the audio on a turn that has a lookup to speak about. No
    snapshot JSON, no list of earlier turns and no recall note go in, because the model answers the newest text it
    reads in place of the audio (measured: 20261116-152653, where replies answered the previous turn's words). A
    refused reply is asked again with one short note, and a reply that fails twice is not spoken; no fixed line is
    played. A reply that states a fact nothing gave it starts a lookup for the person's words when the registry has
    an agent. `context` is the call so far for that agent, as `ConversationLedger.context` gives it, and
    `prior_results` is every lookup result the call has had, which the fact check accepts as said.

    `fallback_pcm` is the audio for `FALLBACK_LINE`, rendered by
    the caller; without it a turn that loses both replies still says the line in its record and plays nothing.
    `prior_tasks` is what earlier turns of the call ran, oldest first, as `carried_tasks` gives it. `ask_pcm` is the
    audio for `ASK_INSTEAD_LINE`, which a correction that names no value speaks in place of a composed reply."""
    t0 = time.perf_counter()

    def clock():
        return time.perf_counter() - t0

    stem = source.stem
    timeline = []
    partial["timeline"] = timeline

    def log(kind, **detail):
        timeline.append({"t": round(clock(), 4), "kind": kind, **detail})

    segmenter = SEGMENTERS[segmenter_name](decider, stem)
    if segmenter_name == "hybrid":
        segmenter.prior_resolved_tasks = tuple(t for t in prior_tasks if t.get("call"))[-3:]
        segmenter.conversation_mode = final_decider
    if eou == "gated":
        segmenter.orch.gate, segmenter.orch.side_effects_wait_for_final = PLAN_A, True
    service = MockTaskService(segmenter, f"conv_{stem}", delay_s, tools, holds_side_effects=eou != "gated",
                              earlier=next((ToolCall(**t["call"]) for t in reversed(prior_tasks) if t.get("call")),
                                           None), simulate=simulate_tasks, context=list(context))
    adapter = TranscriptAdapter()
    updates_q: asyncio.Queue = asyncio.Queue()
    final_seen = asyncio.Event()
    loop = asyncio.get_running_loop()
    executor = ThreadPoolExecutor(max_workers=1)
    calls_before = live_calls()
    closed = {}

    def enqueue(update):
        log("transcript_update", revision=update.revision, text=update.text, is_final=update.is_final)
        updates_q.put_nowait(("update", update))
        if update.is_final:
            final_seen.set()

    def on_asr(event):
        update = adapter.feed(event)
        if update is not None:
            enqueue(update)

    async def segment_worker():
        while True:
            batch = [await updates_q.get()]
            while not updates_q.empty():
                batch.append(updates_q.get_nowait())
            kept, dropped = coalesce(batch)
            if dropped:
                log("coalesced", revisions=[u.revision for u in dropped])
            for kind, payload in kept:
                started, before = clock(), live_calls()
                if kind == "update" and payload.is_final and eou in TOOL_SESSIONS:
                    work = partial_close if eou == "gated" else segmenter.close
                else:
                    work = segmenter.update if kind == "update" else service.submit_delegate
                try:
                    point = await loop.run_in_executor(executor, work, payload)
                except Exception as exc:
                    log("segmenter_error", error=repr(exc))
                    point = None
                if point is not None and point["kind"] == "close":
                    closed.update(point)
                    if point.get("final_decision") is not None:
                        log("final_decision", decision=point["final_decision"])
                    if point.get("final_decision_error") is not None:
                        log("final_decision_error", error=point["final_decision_error"])
                    log("close", revision=point["revision"], started=round(started, 4), intent=asdict(point["intent"]),
                        covered_words=point["covered_words"], tail_words=point["tail_words"],
                        unreviewed_speech=point["unreviewed_speech"], segments=[s["text"] for s in point["segments"]])
                elif point is not None:
                    log("decision_point", source=kind, revision=point["revision"], is_final=point["is_final"],
                        started=round(started, 4), live_calls=live_calls() - before, calls=point["calls"],
                        problems=point["problems"],
                        segments=[s["text"] for s in point["segments"]], decisions=point["decisions"])
                for event in service.sync(clock()):
                    log("service", **event)
            for _ in batch:
                updates_q.task_done()

    own_voice = voice is None
    if own_voice:
        voice = await Session.open(url, "voice", clock, on_event=on_voice_event)
    voice_mark, voice_sent_mark = len(voice.events), len(voice.sent)
    asr = await Session.open(url + "?intent=transcription", "asr", clock, on_event=on_asr)
    def partial_close(update):
        return close_with_decider(segmenter, update) if final_decider else segmenter.close(update, settle=False)

    tool_session = (await Session.open(url, "tools", clock) if eou in TOOL_SESSIONS and not final_decider
                    else None)
    worker = None
    try:
        if tool_session is not None:
            await tool_session.wait_for("session.created")
            await update_session(tool_session, {
                "type": "realtime", "instructions": TOOL_SESSIONS[eou][0], "tools": TOOL_SESSIONS[eou][1],
                "output_modalities": ["text"]})
        created = await voice.wait_for("session.created")
        out_rate = created["session"]["audio"]["output"]["format"]["rate"]
        partial["events"] = {"voice": voice.events[voice_mark:], "voice_sent": voice.sent[voice_sent_mark:],
                             "asr": asr.events, "asr_sent": asr.sent}
        await asr.wait_for("session.created")
        rate = source.rate
        session = {"type": "realtime", "instructions": persona + FORMAT_LINES[inject],
                   "output_modalities": ["text", "audio"]}
        if inject == "b":
            session["tools"] = TOOLS[1:] if status_only else TOOLS
        if rate != MODEL_RATE:
            session["audio"] = {"input": {"format": {"type": "audio/pcm", "rate": rate}}}
            await update_session(asr, {"audio": session["audio"]})
        if getattr(voice, "config", None) != session:
            await update_session(voice, session)
            voice.config = session
        worker = asyncio.create_task(segment_worker())

        replies, tool_turn, injection, answered = [], None, [], None
        fallback = Fallback(fallback_pcm)
        if snapshot_mode == "before-audio" and prior_snapshot is not None:
            injection.append(await create_item(voice, user_text(SNAPSHOT_PREFIX, prior_snapshot)))
            log("snapshot_before_audio", snapshot=prior_snapshot)
        audio_end = await source.stream(voice, asr)
        audio_s = source.audio_s
        log("audio_end", audio_s=audio_s)
        adapter.end_input()
        await asr.send(type="input_audio_buffer.commit", final=True)
        if getattr(source, "voice_at_end", False):
            await source.send_voice(voice)
        await voice.send(type="input_audio_buffer.commit")

        if inject == "b":
            tool_turn, _ = await respond(voice, out_rate, audio_end, tool_choice="required", output_modalities=["text"])
            log("tool_turn", calls=tool_turn["function_calls"], text=tool_turn["text"])
            for call in tool_turn["function_calls"]:
                if call.get("name") == "delegate":
                    instruction = json.loads(call.get("arguments") or "{}").get("instruction", "")
                    updates_q.put_nowait(("delegate", instruction))

        try:
            await asyncio.wait_for(final_seen.wait(), 30)
        except asyncio.TimeoutError:
            log("asr_final_missing")
            enqueue(adapter.finish())
        await updates_q.join()
        service.poll(clock())
        unreviewed = closed.get("unreviewed_speech")
        snapshot = service.snapshot("utt_1", adapter.text(), unreviewed if eou in TOOL_SESSIONS else None)
        log("snapshot", snapshot=snapshot)
        read_before = prior_snapshot if snapshot_mode == "before-audio" else None

        tool_results, extra = [], {"tool_choice": "none"} if inject == "b" else {}
        if eou in TOOL_SESSIONS:
            intent = closed.get("intent")
            if intent is not None and intent.intent == Intent.CORRECTION:
                for event in service.correct(intent.replacement, prior_tasks, clock()):
                    log("service", **event)
                log("correction", replacement=intent.replacement, outcome=service.correction)
            elif not final_decider and (intent is None or intent.intent != Intent.CHITCHAT):
                for_tools = (service.snapshot("utt_1", adapter.text(), unreviewed, notes=True) if eou == "gated"
                             else snapshot)
                injection.append(await create_item(tool_session, user_text(SNAPSHOT_PREFIX, for_tools)))
                tool_turn, _ = await respond(tool_session, out_rate, audio_end, tool_choice="auto")
                log("tool_turn", calls=tool_turn["function_calls"], text=tool_turn["text"])
                new_requests = intent is None or intent.intent == Intent.REQUEST
                for call in tool_turn["function_calls"]:
                    result, events = service.call_tool(call.get("name"), parse_arguments(call), unreviewed, clock(),
                                                       new_requests)
                    for event in events:
                        log("service", **event)
                    tool_results.append({"name": call.get("name"), "arguments": parse_arguments(call),
                                         "result": result})
                    log("tool_call", **tool_results[-1])
            noted = snapshot_mode == "note"
            if noted and service.correction is ASK_WHAT_CHANGED and intent.replacement:
                for event in service.ask(adapter.text(), clock()):
                    log("service", **event)
            service.poll(clock())
            answered = service.snapshot("utt_1", adapter.text(), unreviewed)
            log("snapshot_after_tools", snapshot=answered)
            item = shown(answered, snapshot_mode, adapter.text())
            if item is not None and prior_utterances:
                item = with_call_turns(item, prior_utterances)
                log("history", turn_count=len(item["call_turns"]), omitted=item["omitted_turns"])
            if item is not None:
                injection.append(await create_item(voice, user_text(SNAPSHOT_PREFIX, item)))
            note = recall_note(adapter.text(), prior_utterances) if recall == "note" and not noted else None
            if note is not None:
                injection.append(await create_item(voice, plain_text(note)))
                log("recall_note", note=note)
            if noted:
                asks = service.correction is ASK_WHAT_CHANGED and not answered["tasks"]
                item = ASK_NOTE if asks else task_notes(answered["tasks"])
                if item is not None:
                    injection.append(await create_item(voice, plain_text(item)))
                    log("task_note", note=item)
            log("injection", items=injection)
            # An earlier lookup's values count as given only on a turn with no note of its own. Beside a note they
            # would let the model say an old result for the new lookup (measured: callfix3 replay fix1, turn 25).
            earlier_results = prior_results if noted and item is None else ()
            sources = [adapter.text(), *prior_utterances, *earlier_results, read_before, item]

            def look_up(_facts):
                """Start a lookup for the person's words when the reply stated a fact nothing gave it."""
                if answered["tasks"] or service.correction is not None:
                    return None
                events = service.ask(adapter.text(), clock())
                for event in events:
                    log("service", **event)
                return STILL_RUNNING_NOTE if events else None

            if noted:
                running = is_running(answered)
                reply, reply_pcm = await answer(
                    voice, out_rate, audio_end, sources, log, running=running, escalate=look_up,
                    keep_opening=True, retry_item=lambda found: plain_text(retry_note(found, running)))
            elif service.correction is ASK_WHAT_CHANGED:
                reply, reply_pcm = fixed_reply(ASK_INSTEAD_LINE, ask_pcm, out_rate, clock() - audio_end), ask_pcm
            else:
                reply, reply_pcm = await answer(voice, out_rate, audio_end, sources, log, fallback=fallback,
                                                running=is_running(answered))
            replies.append({"kind": "snapshot", **reply})
            log("reply", reply="snapshot", text=reply["text"])
            if reply_pcm:
                write_pcm16(out / f"{stem}_reply.wav", reply_pcm, out_rate)
            if speak is not None and reply_pcm:
                speak(reply["text"], reply_pcm)
        elif (item := shown(snapshot, snapshot_mode, adapter.text())) is None:
            pass
        elif inject == "a":
            call = {"type": "function_call", "call_id": SYNTHETIC_CALL_ID, "name": "get_task_status", "arguments": "{}"}
            injection.append(await create_item(voice, call))
            injection.append(await create_item(voice, {"type": "function_call_output", "call_id": SYNTHETIC_CALL_ID,
                                                       "output": json.dumps(item)}))
        elif inject == "b":
            for call in tool_turn["function_calls"]:
                injection.append(await create_item(voice, {"type": "function_call_output", "call_id": call["call_id"],
                                                           "output": json.dumps(item)}))
            if not tool_turn["function_calls"]:
                injection.append({"item_type": "function_call_output", "accepted": False,
                                  "answer": "the model made no call"})
        else:
            injection.append(await create_item(voice, user_text(SNAPSHOT_PREFIX, item)))
        if eou not in TOOL_SESSIONS:
            log("injection", items=injection)
            reply, reply_pcm = await answer(voice, out_rate, audio_end,
                                            [adapter.text(), *prior_utterances, read_before, item], log,
                                            fallback=fallback, running=is_running(snapshot), **extra)
            replies.append({"kind": "snapshot", **reply})
            log("reply", reply="snapshot", text=reply["text"])
            if reply_pcm:
                write_pcm16(out / f"{stem}_reply.wav", reply_pcm, out_rate)
            if speak is not None and reply_pcm:
                speak(reply["text"], reply_pcm)

        while (due := service.next_due()) is not None:
            await wait_for_work(service, due - clock())
            for task_id in service.poll(clock()):
                log("service", task_id=task_id, event=service.executions[task_id].state.value)
        task_updates = service.take_updates()
        updates = shown({"updates": task_updates}, snapshot_mode, adapter.text(), update=True) if task_updates else None
        if snapshot_mode == "note" and task_updates:
            updates = task_notes(service.update_entries(task_updates))
        if updates is not None:
            boundary = clock()
            log("task_updates", updates=task_updates)
            if snapshot_mode == "note":
                injection_update = await create_item(voice, plain_text(updates))
                update_reply, update_pcm = await answer(
                    voice, out_rate, boundary, [adapter.text(), *prior_utterances, updates], log,
                    keep_opening=True, retry_item=lambda found: plain_text(retry_note(found, False)), **extra)
            else:
                injection_update = await create_item(voice, user_text(UPDATE_PREFIX, updates))
                update_reply, update_pcm = await answer(voice, out_rate, boundary,
                                                        [adapter.text(), *prior_utterances, updates], log,
                                                        fallback=fallback, **extra)
            replies.append({"kind": "update", "injection": injection_update, **update_reply})
            log("reply", reply="update", text=update_reply["text"])
            if update_pcm:
                write_pcm16(out / f"{stem}_update.wav", update_pcm, out_rate)
            if speak is not None and update_pcm:
                speak(update_reply["text"], update_pcm)

    finally:
        if worker is not None:
            worker.cancel()
        executor.shutdown(wait=False, cancel_futures=True)
        service.close()
        if own_voice:
            await voice.close()
        await asr.close()
        if tool_session is not None:
            await tool_session.close()
    final_t = next(e["t"] for e in timeline if e["kind"] == "transcript_update" and e["is_final"])
    snapshot_t = next(e["t"] for e in timeline if e["kind"] == "snapshot")
    first_audio = next((r["first_audio_after_s"] for r in replies
                        if r["kind"] != "update" and r["first_audio_after_s"] is not None), None)
    return {
        "clip": source.name, "eou": eou, "inject": inject, "segmenter": segmenter_name, "status_only": status_only,
        "delay_s": delay_s, "audio_s": audio_s,
        "audio_end": audio_end,
        "metrics": {
            "final_transcript_after_audio_end_s": final_t - audio_end,
            "snapshot_ready_after_audio_end_s": snapshot_t - audio_end,
            "first_reply_audio_after_audio_end_s": first_audio,
            "decider_live_calls": live_calls() - calls_before,
            "decider_calls": sum(len(p["calls"]) for p in getattr(segmenter, "points", [])),
            "tool_calls": len(tool_results),
            "decision_points": sum(e["kind"] == "decision_point" for e in timeline),
            "segmenter_errors": sum(e["kind"] == "segmenter_error" for e in timeline),
            "coalesced_revisions": sum(len(e["revisions"]) for e in timeline if e["kind"] == "coalesced"),
        },
        "final_transcript": adapter.text(),
        "snapshot": snapshot,
        "snapshot_after_tools": answered,
        "unreviewed_speech": closed.get("unreviewed_speech"),
        "tool_results": tool_results,
        "injection": injection,
        "tool_turn": tool_turn,
        "replies": replies,
        "task_updates": task_updates,
        "tasks": segmenter.records(),
        "dispatch_log": segmenter.dispatch_log,
        "executions": {k: {**vars(v), "state": v.state.value} for k, v in service.executions.items()},
        "timeline": timeline,
        "events": {"voice": voice.events[voice_mark:], "voice_sent": voice.sent[voice_sent_mark:],
                   "asr": asr.events, "asr_sent": asr.sent,
                   **({"tools": tool_session.events, "tools_sent": tool_session.sent}
                      if tool_session is not None else {})},
    }


def carried_tasks(record):
    """The tasks one turn ran, as later turns of the call see them. `ref` names the turn and the task, because every
    turn numbers its tasks from task_1. `call` is the lookup a task resolved, when it answered, so a later follow-up
    can continue its place; a failed lookup carries nothing."""
    turn = Path(record.get("clip") or "turn").stem
    executions = record.get("executions", {})
    carried = []
    for t in record.get("tasks", []):
        if t["status"] != "dispatched":
            continue
        tool = (executions.get(t["task_id"]) or {}).get("tool") or {}
        call = {"tool": tool["tool"], "arguments": tool["arguments"]} if tool.get("outcome") == "ok" else None
        carried.append({"ref": f"{turn}:{t['task_id']}", "instruction": t["instruction"],
                        "side_effect": t["side_effect"], "call": call})
    return carried


async def main_async(args):
    use_decider_cache(args.decider_cache, args.decider_budget)
    out = args.out or EVIDENCE / f"{args.eou}-{args.inject}{'-status' if args.status_only else ''}"
    out.mkdir(parents=True, exist_ok=True)
    start = live_calls()
    slots = 3 if args.eou in TOOL_SESSIONS else 2
    tools_context = nullcontext() if args.mock_tools else open_registry()
    server_context = Server(args.port, out / "llama-server.log", ("-c", str(args.lfm_ctx)), slots)
    with tools_context as tools, server_context as server, make_decider(args.decider, out) as decider:
        if args.fresh:
            decider = FreshClient(decider)
        if tools is not None:
            print(f"MCP server {tools.mcp.server_info} up after {tools.mcp.startup_s:.2f} s", flush=True)
        print(f"llama-server pid {server.proc.pid} healthy after {server.startup_s:.1f} s", flush=True)
        vram_mib = gpu_used_mib()
        if args.reuse:
            decider = ReusingClient(decider)
        for wav in args.clips:
            partial = {}
            try:
                record = await run_clip(WavInput(wav), args.inject, args.status_only,
                                        f"ws://127.0.0.1:{args.port}/v1/realtime", decider, args.delay, out, partial,
                                        args.segmenter, tools, args.eou)
            except Exception as exc:
                record = {"clip": wav.name, "error": repr(exc), "metrics": {"error": repr(exc)}, **partial}
            record["server_startup_s"] = server.startup_s
            record["mcp_startup_s"] = tools.mcp.startup_s if tools is not None else None
            record["config"] = {"eou": args.eou, "decider": args.decider, "reuse": args.reuse, "fresh": args.fresh,
                                "lfm_ctx": args.lfm_ctx, "slots": slots, "vram_mib_after_load": vram_mib}
            (out / f"{wav.stem}.json").write_text(json.dumps(record, indent=1) + "\n", encoding="utf-8")
            print(wav.stem, json.dumps(record["metrics"]), flush=True)
            attempts, share = rate_limited_share(start)
            if attempts >= 10 and share > MAX_429_SHARE:
                print(f"stopping: {share:.0%} of {attempts} decider attempts drew a 429", flush=True)
                break


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("clips", nargs="+", type=Path)
    parser.add_argument("--eou", choices=["gated", "handoff", "decider"], default="gated")
    parser.add_argument("--inject", choices="abc", default="c")
    parser.add_argument("--status-only", action="store_true", help="with b, offer get_task_status and no delegate")
    parser.add_argument("--segmenter", choices=sorted(SEGMENTERS), default="hybrid")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--delay", type=float, default=4.0, help="seconds a mock task takes (default 4)")
    parser.add_argument("--port", type=int, default=8095)
    parser.add_argument("--decider", choices=["liquid", "cloud", "d1"], default=default_decider())
    parser.add_argument("--reuse", action="store_true", help="ask only questions with no kept answer")
    parser.add_argument("--fresh", action="store_true",
                        help="send cached cloud or liquid decider requests again, for timing")
    parser.add_argument("--lfm-ctx", type=int, default=32768, help="LFM3's total context (default 32768)")
    parser.add_argument("--mock-tools", action="store_true", help="no real lookups: every task runs on the mock")
    parser.add_argument("--decider-cache", type=Path, default=EVIDENCE / "decider-cache",
                        help="decider cache and call ledger, kept out of the repo")
    parser.add_argument("--decider-budget", type=int, default=DECIDER_BUDGET,
                        help="stop when the ledger holds this many attempts")
    args = parser.parse_args()
    if args.eou in TOOL_SESSIONS and (args.inject != "c" or args.segmenter != "hybrid"):
        parser.error(f"--eou {args.eou} injects as in c and needs the hybrid segmenter")
    if args.fresh and args.decider not in ("cloud", "liquid"):
        parser.error("--fresh resends cached cloud or liquid requests; the d1 decider has no such cache")
    missing = [str(c) for c in args.clips if not c.is_file()]
    if missing:
        parser.error(f"no such clip: {', '.join(missing)}")
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
