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
import bisect
import difflib
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import asdict
from pathlib import Path

import numpy as np
from websockets.exceptions import WebSocketException

from voice.live.facts import names_machinery, speaks_of_person, ungrounded
from voice.live.overlap import transcribe as overlap_transcribe
from voice.live.recall import recall_note
from voice.local_decider.client import decider as make_decider, default_decider
from voice.local_decider.gpu import gpu_used_mib
from voice.local_decider.reuse import ReusingClient
from voice.t3.acts import recites
from voice.t3.speech import Rendered, speakable
from voice.prototype.realtime import MODEL_RATE, VOICE_HOME, Server, Session, read_pcm16, write_pcm16
from voice.prototype.segmenter import (DECIDER_BUDGET, MAX_429_SHARE, SEGMENTERS, FreshClient, coalesce, live_calls,
                                       rate_limited_share, use_decider_cache)
from voice.prototype.tasks import ASK_INSTEAD_LINE, ASK_WHAT_CHANGED, MockTaskService, amends
from voice.segmentation.contract import Intent, UtteranceIntent
from voice.segmentation.gate import PLAN_A
from voice.segmentation.intent import utterance_intent
from voice.prototype.stagger import StaggeredAsr, StaggeredTranscript
from voice.prototype.transcript import TranscriptAdapter
from voice.tools.registry import T3_TOOLS, ToolCall, open_registry

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
    "request. Greetings, reactions and insults, questions about you or this conversation or what you heard, and "
    "descriptions of a problem are not tasks; the voice assistant answers them directly. "
    "For a task that has not run, call run_task if the user wants it "
    "as it is, amend_task with the change if the user changed it, or cancel_task if the user took it back. If "
    "unreviewed_speech asks for something new that no task covers, call delegate with just that request. If the "
    "user took back a running task, call cancel_task. If nothing needs doing, call no tool and answer none."
    "\n\nRespond with function calls, and interleaved text and audio.\n\nList of tools:"
)
HANDOFF_TOOLS = [
    {"type": "function", "name": "delegate", "description": "Start work the user asked for: an action, or a fact to "
                                                             "find out. Never for conversation, an insult, a reaction, "
                                                             "thanks, or a question about you or this conversation.",
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
HISTORY_TURNS = 8
NOTE_OPEN = "[Note for you, not said by the person: "
ASK_NOTE = (NOTE_OPEN + "they said your last answer was not what they wanted, and did not say what they want "
            "instead. Ask them what they would like instead.]")
# After a question the person heard is dropped unanswered, the voice session still holds its words as the nearest thing
# to answer, and a free reply can say them again. A pick question the reply took back gets the note below, which says
# what they did and names no choices: a note that told the
# voice not to ask the question again was answered with the question, 8 of 8, and one that did not also say not to
# repeat any earlier line was answered with two earlier lines when a status line came before the question, 11 of 12
# (voice/t3/eval/withdraw_eval.py).
WITHDRAWN_NOTE = (NOTE_OPEN + "they took your last question back: they do not want any of the choices it offered, and "
                  "they are not choosing. Do not list, offer or ask about those choices again. Do not repeat any "
                  "earlier line of yours. Answer what they said in this turn.]")
# Any other question dropped unanswered keeps the note it always had.
DROPPED_NOTE = (NOTE_OPEN + "they did not answer your last question and have moved on. Answer what they said in this "
                "turn, and do not repeat or ask that question again.]")
# The holding line before a correction: the voice model words it each time from this meaning alone, as it words the
# line that holds a running lookup (`task_note`). It names no thread and no word to say. Said after a free reply, a note
# that only asked for it was not followed (12 of 12 answered the audio again, with a fresh claim about the thread); one
# that states the situation was, and closing it with "nothing else, ask nothing" stopped a made-up fact and cut the
# questions from 6 of 12 replies to 1 of 12 (docs/voice/live-reply-check.md, GPU).
TRANSITION_NOTE = (NOTE_OPEN + "your last reply about their coding thread was wrong. Take it back now: say you need "
                   "to double check it. Say nothing else about the thread, and ask them nothing.]")
# A line this short is not logged as a repeat: "I understand." twice is conversation, not a stale reply.
REPEAT_WORDS = 8
# What a task that could not start is missing, in the words a note asks for it with.
NEEDS_ASK = {"place": "which city or place they mean"}
SYNTHETIC_CALL_ID = "snapshot_1"


def reply_summary(session, mark, since, rate):
    events = session.events[mark:]
    done = next(e for e in events if e["type"] in ("response.done", "error"))
    response = done.get("response", {})
    audio_events = [e for e in events if e["type"] == "response.output_audio.delta"]
    pcm = b"".join(chunk for index, chunk in session.audio if index >= mark)
    return {
        "text": "".join(e.get("delta", "") for e in events if e["type"] == "response.output_text.delta"),
        "status": response.get("status") or done.get("error", {}).get("type"),
        "status_reason": (response.get("status_details") or {}).get("reason"),
        "function_calls": [o for o in response.get("output", []) if o.get("type") == "function_call"],
        "first_audio_after_s": audio_events[0]["_t"] - since if audio_events else None,
        "speech_s": len(pcm) / 2 / rate,
        "done_at": done["_t"],
    }, pcm


async def floor_free(session):
    """Wait while the live server holds the floor: the person made a sound after their turn ended that is not yet
    known to be words or not (`Session.floor`). A response or note made now could answer a request that is still
    growing, and a note stays in the session even when its turn is then cancelled."""
    floor = getattr(session, "floor", None)
    if floor is not None:
        await floor.wait()


async def respond(session, rate, since, **response):
    await floor_free(session)
    mark = len(session.events)
    await session.send(type="response.create", **({"response": response} if response else {}))
    await session.wait_for("response.done", "error", after=mark)
    return reply_summary(session, mark, since, rate)


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
    not. A task that is pending or needs clarification gets no note, so the model answers the person's own words,
    unless the task names what it is waiting for: then the note asks them for it, and the next turn's answer
    completes the task (`MockTaskService.answer`). A T3 entry gets no note here: its acts are worded by `say_acts`."""
    if entry.get("acts") or entry.get("capability") in T3_TOOLS:
        return None
    status, name = entry["status"], lookup_name(entry)
    if status == "running":
        body = f"{name} is running and has no answer yet. Tell them you're checking."
    elif status == "completed":
        body = f"{name} finished. Result: {entry['result_summary']} Tell them this result."
    elif status == "failed":
        body = f"{name} could not be done. Tell them briefly that you couldn't check that."
    elif status == "cancelled":
        body = f"{name} was dropped, as they asked. Tell them so."
    elif status == "needs_clarification" and entry.get("needs"):
        asked = " and ".join(NEEDS_ASK.get(field, f"the {field}") for field in entry["needs"])
        body = (f"{name} has not started because they have not said {asked}. Ask them for it, and "
                "don't guess one.")
    else:
        return None
    return f"{NOTE_OPEN}{body}]"


def task_notes(entries):
    """The notes for a turn's tasks as one text, or None when no task needs words."""
    lines = [note for note in map(task_note, entries) if note]
    return "\n".join(lines) or None


def given_results(prior_results, noted, tasks):
    """The earlier lookup results the fact check counts as given on a turn.

    Beside a running or completed lookup they are dropped, or the model could say an old result for the new one
    (measured: callfix3 replay fix1, turn 25). Beside a failed task, or none, they stay: "What did you tell me about
    Lisbon?" beside a failed task was refused for repeating the call's own result (recall-memory note, turn 035)."""
    if not noted or any(task.get("status") in ("running", "completed") for task in tasks or ()):
        return ()
    return tuple(prior_results)


def fact_check(text, sources, previous=()):
    """What the screens would have said about a spoken reply, for the record. Every list is empty for a clean one.

    This check never holds, cuts, mutes or asks again. It only reports (ruling, 2026-11-18)."""
    found = {"ungrounded": [f"{fact.kind} {fact.surface}" for fact in ungrounded(text, sources)],
             "machinery": names_machinery(text), "third_person": speaks_of_person(text),
             "repeated": repeated(text, previous)}
    return found


def repeated(text, earlier):
    """True when `text` says again a line the assistant already said in this call, word for word.

    A reply that opens with the line just said, or that repeats an earlier line exactly, is stale. Measured on the
    tool-pick replays' first arm: turns 25, 26 and 28 to 37 re-said "Hello! I'm checking the weather in a city on the
    East Coast. Which one?", and the reply-path replay's baseline re-said its New York forecast for nine turns. The
    model, with no note and its own last reply as the nearest thing in the append-only session, continues itself."""
    said = normalised(text)
    if len(said.split()) < REPEAT_WORDS or not earlier:
        return False
    last = normalised(earlier[-1])
    if len(last.split()) >= REPEAT_WORDS and said.startswith(last):
        return True
    return any(said == normalised(line) for line in earlier)


def interrupted_note():
    """The note after the person talked over a reply. It does not quote the words they heard: quoting them primed
    the model to go on with the old reply, and one reply read the note back (measured, docs/voice/barge-in.md)."""
    return (NOTE_OPEN + "they interrupted your last reply and heard only its start. Answer what they said in this "
            "turn, and do not repeat or continue that reply.]")


async def stream_reply(session, rate, since, out=None, stop=None, **response):
    """One response, read event by event as llama.cpp sends it. `out(text, pcm)` gets each piece of audio the moment
    it arrives, with the reply's text so far. `stop(text)` is asked after each text delta and, when it says yes,
    cancels the response where it is: llama.cpp keeps every token a response generated in the session
    (server-realtime.cpp: a cancel ends the response at the token it reached, and no event removes an item), so
    stopping early is the only way to leave less of a reply behind. Returns the response's summary and audio."""
    await floor_free(session)
    mark = len(session.events)
    await session.send(type="response.create", **({"response": response} if response else {}))
    seen, audio_at, text, stopping = mark, bisect.bisect_left(session.audio, (mark,)), "", False
    done = None
    while done is None:
        await session.wait_for(*STREAM_EVENTS, after=seen)
        while done is None and seen < len(session.events):
            event = session.events[seen]
            kind = event.get("type")
            if kind == "response.output_text.delta":
                text += event.get("delta", "")
                if stop is not None and not stopping and stop(text):
                    stopping = True
                    await session.send(type="response.cancel")
            elif kind == "response.output_audio.delta":
                while session.audio[audio_at][0] < seen:
                    audio_at += 1
                if out is not None:
                    out(text, session.audio[audio_at][1])
            elif kind in ("response.done", "error"):
                done = event
            seen += 1
    return reply_summary(session, mark, since, rate)


STREAM_EVENTS = ("response.output_text.delta", "response.output_audio.delta", "response.done", "error")
WORD = re.compile(r"[A-Za-z0-9']+")


def heard_prefix(text, transcript):
    """The start of `text` up to the last of its words that the transcript of its released audio matched.

    The released audio lags the text it was released on by a median of 7 words at a block boundary (measured,
    docs/voice/stream-release.md), so the text a stopped reply was released on overstates what was heard."""
    spans = [(match.group(0).lower(), match.end()) for match in WORD.finditer(text or "")]
    said = WORD.findall((transcript or "").lower())
    matcher = difflib.SequenceMatcher(a=[word for word, _ in spans], b=said, autojunk=False)
    ends = [block.a + block.size for block in matcher.get_matching_blocks() if block.size]
    return text[:spans[max(ends) - 1][1]].strip() if ends else ""


# Silence after a stopped reply's audio before it is transcribed. Without it the ASR read nothing from 16 of 24
# one-block fragments (0.94 s); with 1 s of it, 4 of 24 (measured, docs/voice/stream-release.md).
EAR_PAD_S = 1.0


def by_ear(url, rate):
    """A `transcribe` for a played reply's audio, at 16 kHz and followed by EAR_PAD_S of silence, through
    llama-server's transcription session."""
    async def transcribe(pcm):
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
        positions = np.arange(0, len(samples), rate / MODEL_RATE)
        audio = np.interp(positions, np.arange(len(samples)), samples).astype("<i2").tobytes()
        return await overlap_transcribe(url, audio + bytes(int(EAR_PAD_S * MODEL_RATE) * 2), Session)
    return transcribe


async def answer(session, rate, since, sources, log, speak=None, previous=(), watch=None, **response):
    """A reply, spoken to `speak` as it is generated: each piece of audio goes out the moment it arrives.

    Nothing holds, cuts, mutes or regenerates it. `fact_check` reads the finished text against `sources`, which is the
    person's words in this call and the newest machine note, and the timeline records what it would have refused. It
    asks the model nothing ([live-fact-check.md](../../docs/voice/live-fact-check.md)). `previous`
    is what the assistant already said in this call, for the same record. `speak(text, pcm, done)` gets each piece of
    audio with the text so far, then one call with `done` set once the reply is over; it is never called for a reply
    with no audio."""
    pcm = bytearray()

    def out(text, chunk):
        pcm.extend(chunk)
        if speak is not None:
            speak(text, chunk, False)

    reply, _ = await stream_reply(session, rate, since, out, **response)
    if watch is not None:
        # The text is complete here and its audio is still playing: the reply check starts now and never holds it.
        watch(reply["text"])
    found = fact_check(reply["text"], sources, previous)
    if any(found.values()):
        log("fact_check", reply=reply["text"], **found)
    if speak is not None and pcm:
        speak(reply["text"], b"", True)
    return {**reply, "speech_s": len(pcm) / 2 / rate}, bytes(pcm)


async def answer_unheard(session, rate, since, log):
    """A text-only reply to the turn's audio, which is never spoken and never reaches the caller. It is there so the
    audio has an answer in the session before a relay: llama.cpp takes no assistant item
    (`server-realtime.cpp`, `on_item_create`), so the model writes it."""
    reply, _ = await respond(session, rate, since, output_modalities=["text"])
    log("unheard_answer", text=reply["text"], status=reply["status"])
    return reply


async def speak_note(session, rate, since, note, sources, log, speak=None, previous=(), watch=None, **response):
    """`note` goes in the session and the reply it asks for is spoken. A task update takes this path at the end of a
    turn. Returns the item's answer, the reply, and its audio."""
    item = await create_item(session, plain_text(note))
    reply, pcm = await answer(session, rate, since, [*sources, note], log, speak=speak, previous=previous, watch=watch,
                              **response)
    return item, reply, pcm


T3_NOTE = ("[Private note for you, not said by the person. What they just said is already handled, so do not answer it. "
           'Say exactly these words to them now, from the first word to the last, as "I" to "you", with nothing before '
           "or after them and no mention of this note. The words: ")
MAX_RELAYS = 2


def relay_note(text, attempt=0):
    """End on the checked script because the voice can read out instructions that follow it."""
    head = T3_NOTE
    if attempt:
        head = head.replace(', as "I" to "you"', "").replace("The words: ", "Reply with only the quoted words. The words: ")
    return f'{head}"{text}"]'


# A refused draft is worded again once in the same turn, with what was refused in the request: the writer shortened a
# long thread title (1 of 10 rehearsal calls) and a connection reset made the checker refuse a line it never judged.
MAX_DRAFTS = 2


def _verdict(rendered):
    verdict = rendered.verdict
    return None if verdict is None else {"passed": verdict.passed, "ms": verdict.ms, "failed": list(verdict.failed),
                                         "error": verdict.error}


async def say_acts(session, rate, since, entry, words, turns, log, *, send_audio, record_delivery=None,
                   is_muted=lambda: False, lead=None):
    """Check the draft before handing it to the call, and say its audio only when the call's reply is the draft's words.
    `lead` is said while the first draft is made, and finishes before the line is relayed: the call's one voice session
    cannot say two things at once."""
    for attempt in range(MAX_DRAFTS):
        started = session.clock()
        drafting = asyncio.ensure_future(asyncio.to_thread(words.say, entry, turns))
        if attempt == 0 and lead is not None:
            await lead()
        draft = await drafting
        log("t3_words", acts=entry.get("acts"), started=started, line=draft.raw,
            problems=draft.problems, error=draft.error,
            decider=_verdict(draft), attempt=attempt + 1)
        if draft.line is not None:
            break
    delivery, sent, first_sent = "unsent", b"", None
    reply = fixed_reply("", b"", rate, 0)
    spoken = None
    if draft.line is not None and not is_muted():
        for attempt in range(MAX_RELAYS):
            reply, pcm = await relay_draft(session, rate, since, draft.line, abandoned=is_muted, attempt=attempt)
            if reply["status"] == "completed" and not reply["function_calls"] and pcm:
                spoken = words.accept(entry, reply["text"], draft.line)
                if spoken.line is not None:
                    sent, first_sent = await send_audio(spoken.line, pcm)
                    delivery = "sent" if len(sent) == len(pcm) else "partial" if sent else "unsent"
            else:
                raw = None if recites(reply["text"], draft.line.text) else reply["text"]
                spoken = Rendered(None, raw=raw, error="call reply missing audio or incomplete")
            if spoken.line is None:
                words.refuse(entry, spoken)
            log("t3_call_reply", text=reply["text"], status=reply["status"], generated_audio_s=reply["speech_s"],
                problems=spoken.problems, error=spoken.error, attempt=attempt + 1)
            if (spoken.line is not None or reply["status"] not in ("completed", "cancelled", "incomplete")
                    or reply.get("status_reason") == "turn_detected" or is_muted()):
                break
    if delivery == "sent":
        entry["spoken"] = spoken.line.text
    if record_delivery is not None:
        record_delivery(delivery)
    log("t3_delivery", delivery=delivery, audio_s=len(sent) / 2 / rate)
    return {
        **reply, "speech_s": len(sent) / 2 / rate, "delivery": delivery,
        "worded_by": draft.line.worded_by if draft.line is not None else None,
        "draft_check": _verdict(draft),
        "first_audio_generated_after_s": reply["first_audio_after_s"],
        "first_audio_after_s": None if first_sent is None else first_sent - since,
    }, sent


async def relay_draft(session, rate, since, line, abandoned=lambda: False, attempt=0):
    """The call's own voice says the checked draft. The response stops at the first word that is not the draft's, so
    a reply that goes off the words leaves only its start in the session. `abandoned()` is true once the turn is
    cancelled or muted; a relay that finds it so after the floor opens makes no response."""
    text = speakable(line, "relay_draft").text

    async def generate():
        try:
            await floor_free(session)
            if abandoned():
                return {**fixed_reply("", b"", rate, 0), "status": "abandoned"}, b""
            item = await create_item(session, plain_text(relay_note(text, attempt)))
            if item["accepted"] is True:
                # The floor can close while the note is acknowledged, and the sound can then continue the turn.
                await floor_free(session)
                if abandoned():
                    return {**fixed_reply("", b"", rate, 0), "status": "abandoned"}, b""
                return await stream_reply(session, rate, since, stop=lambda said: not recites(said, text, arriving=True))
            status = item["answer"]
        except (OSError, SessionRefused, WebSocketException) as exc:
            status = str(exc)
        return {**fixed_reply("", b"", rate, 0), "status": status}, b""

    # Outside the shield: a turn cancelled while the floor is held has sent nothing, and must not send it later.
    await floor_free(session)
    response = asyncio.create_task(generate())
    try:
        return await asyncio.shield(response)
    except asyncio.CancelledError:
        # The next turn shares this socket and must start after the discarded response ends.
        await response
        raise


def fixed_reply(line, pcm, rate, after_s):
    """A reply the turn speaks without asking the voice model: `line` and its pre-rendered `pcm`. With no clip the
    record still says the line, and nothing plays."""
    return {"text": line, "status": "fixed", "fixed": True, "function_calls": [],
            "first_audio_after_s": after_s if pcm else None, "speech_s": len(pcm) / 2 / rate, "done_at": None}


async def create_item(session, item):
    """Sends one item and returns what the server answered: the added item or an error. A note sent while the floor
    is held would stay in the session if the sound turns out to continue the turn, so it waits too."""
    await floor_free(session)
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


def decider_arguments(decider=None, *, t3_enabled=False):
    """Whether the decider reads the tool's place and day in the same request. On by default; VOICE_DECIDER_ARGS=off
    turns it off. Measured at least as accurate as the word rules on both surfaces: 9 of 10 labeled eval items against
    their 8, and on the real turns it read the place the word rules cannot ("what about San Diego?" has no preposition
    for them), which turned that turn's Portland forecast into a San Diego one.

    On T3 calls, skip the questions for the free cloud decider, which declares text_questions = False.
    VOICE_DECIDER_ARGS still decides when set."""
    if getattr(decider, "typed_answers", True) is False:
        # The local decider cannot answer a text question at all, whatever the setting says.
        return False
    setting = os.environ.get("VOICE_DECIDER_ARGS")
    if setting is not None:
        return setting.lower() != "off"
    return not t3_enabled or getattr(decider, "text_questions", True) is not False


def segmenter_for(name, decider, label, tools):
    """The segmenter a turn runs. The hybrid is told the tool question's options, so a dispatch in this turn can use
    the decider's own tool answer; a run with no registry asks nothing about tools."""
    if name != "hybrid" or tools is None:
        return SEGMENTERS[name](decider, label)
    choices = tools.choices()
    segmenter = SEGMENTERS[name](decider, label, tools=choices,
                                 arguments=decider_arguments(decider, t3_enabled=bool(T3_TOOLS & choices.keys())))
    segmenter.pending_requests = list(getattr(tools, "approvals", ()))
    segmenter.pending_questions = list(getattr(tools, "questions", ()))
    return segmenter


def with_thread(resolved, thread):
    """The earlier tasks a follow-up can continue, with the thread the call last spoke about as the newest of them.
    A T3 task has no lookup result to carry, so without this the decider judged a follow-up right after a thread's
    status with no thread in view: chat that needs clarifying, though it picked the T3 tool."""
    if not thread:
        return tuple(resolved)
    asked = {"ref": "thread", "instruction": f"the coding thread {thread}", "side_effect": False,
             "call": {"tool": "t3_ask", "arguments": {"thread": thread}}}
    return (*tuple(resolved)[-2:], asked)


def turn_intent(text, prior_tasks=()):
    """What the whole utterance is, as `utterance_intent` reads it, except that one worded as a correction whose words
    change nothing in the call's newest task is a request: "I wanted an update on the thread from before" after a status
    line is asked of the decider, not answered with "what did you want instead?"."""
    intent = utterance_intent(text)
    if intent.intent == Intent.CORRECTION and intent.replacement and not amends(prior_tasks, intent.replacement):
        return UtteranceIntent(Intent.REQUEST)
    return intent


def close_with_decider(segmenter, update, prior_tasks=()):
    decision, error = None, None
    intent = turn_intent(update.text, prior_tasks)
    if intent.intent == Intent.REQUEST:
        try:
            decision = segmenter.update(update)
        except Exception as exc:
            error = repr(exc)
    close = segmenter.close(update, settle=False)
    close["intent"] = intent
    close["final_decision"] = decision
    close["final_decision_error"] = error
    return close


def live_attempts(decider):
    """The decider's own HTTP attempts so far, read from the ledger that this decider writes to."""
    return decider.live_calls() if hasattr(decider, "live_calls") else live_calls()


async def run_clip(source, inject, status_only, url, decider, delay_s, out, partial, segmenter_name="hybrid",
                   tools=None, eou="gated", persona=PERSONA, on_voice_event=None, voice=None,
                   snapshot_mode="reply", prior_snapshot=None, speak=None, prior_utterances=(),
                   prior_tasks=(), ask_pcm=b"", simulate_tasks=True, final_decider=False,
                   recall="off", context=(), prior_results=(), asr_stagger=1, prior_replies=(),
                   task_service=None, send_checked=None, is_muted=lambda: False, reply_check=None,
                   on_heard=None):
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

    `on_heard(text)` gets the turn's final transcript once it is known, before the reply, for live captions.

    `speak(text, pcm, done)` takes each piece of reply audio as it is generated, when the caller streams the reply
    itself, as live mode does, and one call with `done` set at the end of each reply (`answer`). The turn writes the
    audio it spoke to `CLIP_reply.wav` and `CLIP_update.wav` either way, so a record is readable without the client's
    copy. `prior_utterances` is what the person said earlier in the call, which the fact check logs against because
    the shipped snapshot no longer carries their words. The last `HISTORY_TURNS` of them also go into
    the snapshot item as numbered `call_turns`, so a question about what was said earlier has the call's own text to
    read.

    `recall` says how a question about the call reaches the model. "off" is the shipped behaviour, the numbered
    `call_turns` inside the snapshot item. "note" keeps that list and adds one plain sentence naming the turn the
    question asks about (voice/live/recall.py) as the newest item before the reply.

    `snapshot_mode` "note" is live mode's default. The voice session then holds only the call itself: each turn's
    audio and reply, plus one `task_notes` text after the audio on a turn that has a lookup to speak about. No
    snapshot JSON, no list of earlier turns and no recall note go in, because the model answers the newest text it
    reads in place of the audio (measured: 20261116-152653, where replies answered the previous turn's words). A
    reply is spoken as generated and checked only for the record (`answer`). `context` is the call so far for the
    general agent, as `ConversationLedger.context` gives it, and `prior_results` is every lookup result the call has
    had, which the record counts as said. `prior_replies` is what the assistant already said in this call.

    T3 entries go through the draft check and then the call's own voice. In note mode a turn that also has a lookup
    to speak about says the lookup's reply and then the T3 line. A turn whose words answered a T3 question, and a turn
    in any other mode, says only its T3 line.

    `reply_check` (voice/live/replycheck.py) watches each free reply once its text is complete. A reply that says
    something about a thread that T3 does not support is corrected after it: the turn says a typed correction in the
    call's voice, or leaves it for the quiet moment when the caller is already talking.

    `prior_tasks` is what earlier turns of the call ran, oldest first, as `carried_tasks` gives it. `ask_pcm` is the
    audio for `ASK_INSTEAD_LINE`, which a correction that names no value speaks in place of a composed reply.

    A `prior_tasks` entry with `waiting` is a task an earlier turn could not start for a missing argument, which it
    asked the person for. When this turn's words fill it and name no other tool's subject, that task runs with what
    they said (`MockTaskService.answer`), and the decider is asked nothing: the turn answers the assistant's own
    question, so it is not a new decision."""
    t0 = time.perf_counter()
    if prior_snapshot is not None and "tasks" in prior_snapshot:
        prior_snapshot = {**prior_snapshot, "tasks": [entry for entry in prior_snapshot["tasks"] if not entry.get("acts")]}

    def clock():
        return time.perf_counter() - t0

    stem = source.stem
    timeline = []
    partial["timeline"] = timeline

    def log(kind, **detail):
        timeline.append({"t": round(clock(), 4), "kind": kind, **detail})

    segmenter = segmenter_for(segmenter_name, decider, stem, tools)
    if segmenter_name == "hybrid":
        segmenter.prior_resolved_tasks = tuple(t for t in prior_tasks if t.get("call"))[-3:]
        segmenter.conversation_mode = final_decider
    if eou == "gated":
        segmenter.orch.gate, segmenter.orch.side_effects_wait_for_final = PLAN_A, True
    service = (task_service or MockTaskService)(
        segmenter, f"conv_{stem}", delay_s, tools, holds_side_effects=eou != "gated",
        earlier=next((ToolCall(**t["call"]) for t in reversed(prior_tasks) if t.get("call")), None),
        simulate=simulate_tasks, context=list(context))
    waiting = service.open_question() or next((t for t in reversed(prior_tasks) if t.get("waiting")), None)
    if segmenter_name == "hybrid":
        segmenter.prior_resolved_tasks = with_thread(segmenter.prior_resolved_tasks,
                                                     getattr(service, "recent_thread", lambda: None)())
        segmenter.withdrawn_question = getattr(service, "declined_state", lambda: None)()
    adapter = StaggeredTranscript(asr_stagger) if asr_stagger > 1 else TranscriptAdapter()
    updates_q: asyncio.Queue = asyncio.Queue()
    final_seen = asyncio.Event()
    loop = asyncio.get_running_loop()
    executor = ThreadPoolExecutor(max_workers=1)
    calls_before = live_attempts(decider)
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
                started, before = clock(), live_attempts(decider)
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
                    for event in point.get("answered") or ():
                        log("service", **event)
                    if point.get("final_decision") is not None:
                        log("final_decision", decision=point["final_decision"])
                    if point.get("final_decision_error") is not None:
                        log("final_decision_error", error=point["final_decision_error"])
                    log("close", revision=point["revision"], started=round(started, 4), intent=asdict(point["intent"]),
                        covered_words=point["covered_words"], tail_words=point["tail_words"],
                        unreviewed_speech=point["unreviewed_speech"], segments=[s["text"] for s in point["segments"]])
                elif point is not None:
                    log("decision_point", source=kind, revision=point["revision"], is_final=point["is_final"],
                        started=round(started, 4), live_calls=live_attempts(decider) - before, calls=point["calls"],
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
    if asr_stagger > 1:
        asr = await StaggeredAsr.open(url + "?intent=transcription", "asr", clock, on_asr, asr_stagger, session_cls=Session)
    else:
        asr = await Session.open(url + "?intent=transcription", "asr", clock, on_event=on_asr)
    def partial_close(update):
        answered = service.answer(waiting, update.text, clock()) if waiting is not None else []
        if answered:
            point = segmenter.close(update, settle=False)
            point["answered"] = answered
            return point
        return (close_with_decider(segmenter, update, prior_tasks) if final_decider
                else segmenter.close(update, settle=False))

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

        replies, tool_turn, injection, answered, t3_only, owed = [], None, [], None, False, []
        record_delivery = getattr(service, "record_delivery", None)

        async def say_t3(entries, kind, since, *, words=service, delivered=record_delivery, render_context=True):
            for index, entry in enumerate(entries):
                delivery = None if delivered is None else lambda state: delivered(entry, state)
                said = [r["text"] for r in replies if r.get("text") and r.get("delivery", "sent") == "sent"]
                turns = [*context, {"user": adapter.text(), "assistant": said}] if render_context else ()
                reply, pcm = await say_acts(voice, out_rate, since, entry, words, turns, log,
                                            send_audio=send_checked, record_delivery=delivery, is_muted=is_muted)
                replies.append({"kind": kind, "row": entry.get("row"), **reply})
                log("reply", reply=kind, text=reply["text"])
                if pcm:
                    suffix = f"_{index + 1}" if index else ""
                    write_pcm16(out / f"{stem}_{kind}{suffix}.wav", pcm, out_rate)

        watch = reply_check.watch if reply_check is not None else None

        async def correct():
            """Say the correction for each free reply the check found wrong, in the call's voice."""
            if reply_check is None:
                return
            plans = await reply_check.settled()
            for record in reply_check.take_records():
                log("claim_check", **record)
            for plan in plans:
                if plan.hold:
                    service.t3.hold_news(plan.update)
                    continue
                entry = service.correction_entry(plan.update)
                said = [r["text"] for r in replies if r.get("text") and r.get("delivery", "sent") == "sent"]
                turns = [*context, {"user": adapter.text(), "assistant": said}]
                flagged = voice.clock()

                async def transition():
                    """The holding line: the voice, in its own words, takes back what it said and says it is checking.
                    It is a free reply, so it is not itself checked against T3."""
                    if is_muted():
                        return
                    _, held, held_pcm = await speak_note(voice, out_rate, flagged, TRANSITION_NOTE, [], log,
                                                         speak=speak, previous=said)
                    replies.append({"kind": "transition", **held})
                    log("reply", reply="transition", text=held["text"], flag_to_audio_s=held["first_audio_after_s"])
                    if held_pcm:
                        write_pcm16(out / f"{stem}_transition.wav", held_pcm, out_rate)

                reply, pcm = await say_acts(voice, out_rate, flagged, entry, service, turns, log,
                                            send_audio=send_checked, is_muted=is_muted, lead=transition,
                                            record_delivery=lambda state: record_delivery(entry, state))
                replies.append({"kind": "correction", "row": entry.get("row"), **reply})
                log("reply", reply="correction", text=reply["text"])
                if reply["delivery"] == "sent":
                    service.t3.spoke(plan.update)
                if pcm:
                    write_pcm16(out / f"{stem}_correction.wav", pcm, out_rate)

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
        if on_heard is not None and adapter.text():
            on_heard(adapter.text())
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
            elif (not final_decider and not closed.get("answered")
                  and (intent is None or intent.intent != Intent.CHITCHAT)):
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
            dropped_question = getattr(service, "withdrawn", lambda: None)()
            t3_entries = [entry for entry in answered["tasks"] if entry.get("acts")]
            ordinary = {**answered, "tasks": [entry for entry in answered["tasks"] if not entry.get("acts")]}
            # A turn with a T3 line speaks nothing free about the person's words: what it says about T3 work is the
            # checked line, and its lookups are said after it as updates. When it has a lookup to speak about, the
            # audio is first answered in text nobody hears, since the call's voice refuses a relay that comes straight
            # after that audio (docs/voice/spoken-acts.md). Words that answered a T3 question are only that answer,
            # and the modes no live call uses keep a T3 turn to its T3 line.
            t3_only = bool(t3_entries) and (not noted or bool(closed.get("answered")))
            owed = [entry for entry in ordinary["tasks"] if task_note(entry)] if t3_entries and not t3_only else []
            if t3_entries:
                if owed:
                    await answer_unheard(voice, out_rate, audio_end, log)
                await say_t3(t3_entries, "t3", audio_end)
                if not owed and all(r.get("worded_by") is None for r in replies):
                    # No line passed its checks, so nothing answered the audio, and unanswered audio is answered by
                    # the next turn's reply, with thread facts no check saw.
                    await answer_unheard(voice, out_rate, audio_end, log)
            item = None if t3_entries else shown(ordinary, snapshot_mode, adapter.text())
            if item is not None and prior_utterances:
                item = with_call_turns(item, prior_utterances)
                log("history", turn_count=len(item["call_turns"]), omitted=item["omitted_turns"])
            if item is not None:
                injection.append(await create_item(voice, user_text(SNAPSHOT_PREFIX, item)))
            note = (recall_note(adapter.text(), prior_utterances)
                    if recall == "note" and not noted and not t3_entries else None)
            if note is not None:
                injection.append(await create_item(voice, plain_text(note)))
                log("recall_note", note=note)
            if noted and not t3_entries:
                asks = service.correction is ASK_WHAT_CHANGED and not answered["tasks"]
                item = ASK_NOTE if asks else task_notes(ordinary["tasks"])
                if item is None and dropped_question is not None:
                    item = WITHDRAWN_NOTE if getattr(dropped_question, "took_back", False) else DROPPED_NOTE
                if item is not None:
                    injection.append(await create_item(voice, plain_text(item)))
                    log("task_note", note=item)
            log("injection", items=injection)
            tasks = ordinary["tasks"]
            sources = [adapter.text(), *prior_utterances, *given_results(prior_results, noted, tasks), read_before,
                       item]

            if t3_entries:
                reply = None
            elif noted:
                reply, reply_pcm = await answer(voice, out_rate, audio_end, sources, log, speak=speak,
                                                previous=prior_replies, watch=watch)
            elif service.correction is ASK_WHAT_CHANGED:
                reply, reply_pcm = fixed_reply(ASK_INSTEAD_LINE, ask_pcm, out_rate, clock() - audio_end), ask_pcm
                if reply_pcm and speak is not None:
                    speak(reply["text"], reply_pcm, True)
                elif send_checked is not None:
                    _, reply, reply_pcm = await speak_note(
                        voice, out_rate, audio_end, ASK_NOTE, sources, log, speak=speak)
            else:
                reply, reply_pcm = await answer(voice, out_rate, audio_end, sources, log, speak=speak,
                                                previous=prior_replies, watch=watch)
            if reply is not None:
                replies.append({"kind": "snapshot", **reply})
                log("reply", reply="snapshot", text=reply["text"])
                if reply_pcm:
                    write_pcm16(out / f"{stem}_reply.wav", reply_pcm, out_rate)
                await correct()
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
                                            speak=speak, previous=prior_replies, **extra)
            replies.append({"kind": "snapshot", **reply})
            log("reply", reply="snapshot", text=reply["text"])
            if reply_pcm:
                write_pcm16(out / f"{stem}_reply.wav", reply_pcm, out_rate)

        # A turn kept to its T3 line says nothing more, and T3 news never rides on a turn: it waits for a quiet moment
        # (the live server's idle path).
        task_updates = []
        while not t3_only and (due := service.next_due()) is not None:
            await wait_for_work(service, due - clock())
            for task_id in service.poll(clock()):
                log("service", task_id=task_id, event=service.executions[task_id].state.value)
        if owed:
            now = {entry["task_id"]: entry for entry in service.snapshot("utt_1", adapter.text(), unreviewed)["tasks"]}
            owed = [now.get(entry["task_id"], entry) for entry in owed]
        if not t3_only:
            task_updates = getattr(service, "take_task_updates", service.take_updates)()
        ordinary_updates = [entry for entry in task_updates if not entry.get("acts")]
        updates = None
        if snapshot_mode == "note" and (owed or ordinary_updates):
            updates = task_notes([*owed, *service.update_entries(ordinary_updates)])
        elif ordinary_updates:
            updates = shown({"updates": ordinary_updates}, snapshot_mode, adapter.text(), update=True)
        if updates is not None:
            boundary = clock()
            log("task_updates", updates=task_updates, owed=owed)
            said = (*prior_replies, *[r["text"] for r in replies if r.get("text") and r.get("delivery", "sent") == "sent"])
            if snapshot_mode == "note":
                injection_update, update_reply, update_pcm = await speak_note(
                    voice, out_rate, boundary, updates, [adapter.text(), *prior_utterances], log,
                    speak=speak, previous=said, watch=watch, **extra)
            else:
                injection_update = await create_item(voice, user_text(UPDATE_PREFIX, updates))
                update_reply, update_pcm = await answer(voice, out_rate, boundary,
                                                        [adapter.text(), *prior_utterances, updates], log,
                                                        speak=speak, previous=said, **extra)
            replies.append({"kind": "update", "injection": injection_update, **update_reply})
            log("reply", reply="update", text=update_reply["text"])
            if update_pcm:
                write_pcm16(out / f"{stem}_update.wav", update_pcm, out_rate)
            await correct()

        unanswered = getattr(service, "unanswered", None)
        interrupted = any(r.get("delivery") == "partial" or r.get("status_reason") == "turn_detected" for r in replies)
        if (unanswered is not None and send_checked is not None and adapter.text() and not is_muted()
                and not interrupted):
            fallback = unanswered(has_audio=any(r["speech_s"] for r in replies))
            if fallback is not None:
                entry, words, delivered = fallback
                await say_t3([entry], "t3", audio_end, words=words, delivered=lambda _, state: delivered(state),
                             render_context=False)

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
                        if r["kind"] not in ("update", "t3_update") and r["first_audio_after_s"] is not None), None)
    return {
        "clip": source.name, "eou": eou, "inject": inject, "segmenter": segmenter_name, "status_only": status_only,
        "delay_s": delay_s, "audio_s": audio_s,
        "audio_end": audio_end,
        "metrics": {
            "final_transcript_after_audio_end_s": final_t - audio_end,
            "snapshot_ready_after_audio_end_s": snapshot_t - audio_end,
            "first_reply_audio_after_audio_end_s": first_audio,
            "decider_live_calls": live_attempts(decider) - calls_before,
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
                   **({"asr_lanes": asr.extra_events} if asr_stagger > 1 else {}),
                   **({"tools": tool_session.events, "tools_sent": tool_session.sent}
                      if tool_session is not None else {})},
    }


def carried_tasks(record):
    """The tasks one turn ran, as later turns of the call see them. `ref` names the turn and the task, because every
    turn numbers its tasks from task_1. `call` is the lookup a task resolved, when it answered, so a later follow-up
    can continue its place; a failed lookup carries nothing.

    A task that could not start for a missing argument carries `waiting` instead: the tool it would run, the
    arguments it has, and what it lacks. The next turn's words answer that question and run it."""
    turn = Path(record.get("clip") or "turn").stem
    executions = record.get("executions", {})
    carried = []
    for t in record.get("tasks", []):
        if t["status"] == "needs_clarification" and t.get("waiting"):
            carried.append({"ref": f"{turn}:{t['task_id']}", "instruction": t["instruction"],
                            "side_effect": t["side_effect"], "call": None, "waiting": t["waiting"]})
            continue
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
