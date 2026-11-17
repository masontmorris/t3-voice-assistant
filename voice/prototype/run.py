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

--shadow on adds the end-of-utterance decider call a `gated` turn does not make, after the reply's audio has been
released, and writes one line per clip to shadow.jsonl in --out: the decider's task decisions beside the turn's LFM3
tool calls, with per-task agreement (voice/live/shadow.py). It acts on nothing. Off by default here, so a research
run's recorded decider call counts stay comparable; live mode, where the shadow runs on every gated turn, turns it on.

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

from voice.live.facts import asks_to_choose, names_machinery, speaks_of_person, ungrounded
from voice.live.overlap import transcribe as overlap_transcribe
from voice.live.recall import recall_note
from voice.live import shadow as shadow_log
from voice.local_decider.client import decider as make_decider, default_decider
from voice.local_decider.gpu import gpu_used_mib
from voice.local_decider.reuse import ReusingClient
from voice.prototype.release import Release
from voice.prototype.realtime import MODEL_RATE, VOICE_HOME, Server, Session, read_pcm16, write_pcm16
from voice.prototype.segmenter import (DECIDER_BUDGET, MAX_429_SHARE, SEGMENTERS, FreshClient, coalesce, live_calls,
                                       rate_limited_share, use_decider_cache)
from voice.prototype.tasks import ASK_INSTEAD_LINE, ASK_WHAT_CHANGED, MockTaskService
from voice.segmentation.contract import Intent
from voice.segmentation.gate import PLAN_A
from voice.segmentation.intent import utterance_intent
from voice.prototype.stagger import StaggeredAsr, StaggeredTranscript
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
REPEAT_NOTE = (NOTE_OPEN + "you said that already. Answer what they said in this turn, in one short sentence, and "
               "do not say it again or give them anything you have already given them.]")
NEW_WORDS_NOTE = (NOTE_OPEN + "you repeated yourself. Set your last answer aside and answer only what they said in "
                  "this turn, in one short sentence, with no weather, forecast, city, day, time or number in it.]")
FINAL_NOTE = (NOTE_OPEN + "your last reply still cannot be said. Answer what they said in this turn, in one short "
              "sentence, as yourself and to them, and name no weather, forecast, city, day, time or number.]")
# A line this short is not screened for repeating: "I understand." twice is conversation, not a stale reply.
REPEAT_WORDS = 8
# What a task that could not start is missing, in the words a note asks for it with.
NEEDS_ASK = {"place": "which city or place they mean"}
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


def retry_note(found, running, repeats=False):
    """The note a refused reply is answered with. A reply that stated values nothing gave it is told which, because
    asked again with a general note the model repeated the same sentence (measured: callfix3 replay fix1, turn 2). A
    reply that only said what the assistant already said is told that instead: naming no value would leave the model
    with nothing to change."""
    if running:
        return STILL_RUNNING_NOTE
    values = [fact.split(" ", 1)[1] for fact in found.get("ungrounded", ())]
    if values:
        return (f"{NOTE_OPEN}your last reply was not spoken, because nothing gave you {', '.join(values)}. Reply again "
                "without that. If they asked for something you were not given, say you can't look that up.]")
    if repeats:
        return REPEAT_NOTE
    return RETRY_NOTE


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
    completes the task (`MockTaskService.answer`)."""
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
    if status == "needs_clarification" and entry.get("needs"):
        asked = " and ".join(NEEDS_ASK.get(field, f"the {field}") for field in entry["needs"])
        return (f"{NOTE_OPEN}{name} has not started because they have not said {asked}. Ask them in one short "
                "sentence, and don't guess one.]")
    return None


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


def may_repeat(text, earlier):
    """True while the start of a reply could still turn out `repeated`: its words so far are the opening of a line of
    at least REPEAT_WORDS words the assistant already said, or it already opens with the line just said. Released, a
    stale reply's opening could not be taken back."""
    said = normalised(text).split()
    lines = [normalised(line).split() for line in earlier]
    if lines and len(lines[-1]) >= REPEAT_WORDS and said[:len(lines[-1])] == lines[-1]:
        return True
    return any(len(line) >= REPEAT_WORDS and line[:len(said)] == said for line in lines)


def may_choose(text, tasks):
    """True when the person handed the assistant the choice and no lookup has a place of its own.

    A lookup with a place has made the choice already, and its place is in the note. Measured on callfix3 replay fix2,
    turn 025: the weather lookup for San Diego was running while the reply chose Boston, so speaking the reply's own
    pick there would announce one city and then report another."""
    return asks_to_choose(text) and not any((task.get("arguments") or {}).get("place") for task in tasks or ())


def place_only(found):
    """True when a reply's only screen objection is a place nothing in the call states."""
    return (bool(found["ungrounded"]) and not found["machinery"] and not found["third_person"]
            and not found.get("contradicts")
            and all(fact.startswith("place ") for fact in found["ungrounded"]))


# A continuation that opens with this many of the words the person already heard is saying them again.
RESTATE_WORDS = 4
STREAM_EVENTS = ("response.output_text.delta", "response.output_audio.delta", "response.done", "error")
RESPONSE_KEYS = ("text", "passed", "heard", "releases", "screened_against")


def screened(text, sources, running=False, heard="", final=True, previous=(), choose=False):
    """Why `text` may not be released, by screen: `refused`, `repeated` against `previous`, and `restated` for a
    continuation that opens with the words the person already heard. With `choose`, a place is the person's choice
    when it is the only objection. Mid-reply (`final` False), text that could still turn into a repeat or into the
    heard opening is held."""
    found = refused(text, sources, running)
    if choose and place_only(found):
        found["ungrounded"] = []
    if previous and (repeated(text, previous) if final else may_repeat(text, previous)):
        found["repeated"] = True
    opening = normalised(heard).split()[:RESTATE_WORDS]
    said = normalised(text).split()
    shared = min(len(said), len(opening))
    if shared and said[:shared] == opening[:shared] and (not final or shared >= 2):
        found["restated"] = said[:shared]
    return found


def chosen_places(text, sources, running):
    """The places a reply named that nothing gave it, when they are its only objection: the person's choice."""
    found = refused(text, sources, running)
    return [fact.split(" ", 1)[1] for fact in found["ungrounded"]] if place_only(found) else []


def continue_note(heard, found, running):
    """The note after a reply stopped part way. It quotes what the person heard, because asked again from the top
    the model would say that part twice, and it names a value that stopped the rest so the model drops it.

    While a lookup runs, the note says so and names no value, as `retry_note` does. A value stopped then is an earlier
    result said for the new lookup, and "nothing gave you" it was false: measured on demo call 20261117-013438, turn
    004, the model took it as having no forecast and said "I don't know the weather in Raleigh" for four turns."""
    values = [fact.split(" ", 1)[1] for fact in found.get("ungrounded", ())]
    why = (", because the lookup is still running and has no answer yet" if running
           else f", because nothing gave you {', '.join(values)}" if values else "")
    return (f"{NOTE_OPEN}they heard only the start of your last reply, \"{heard}\", and not the rest{why}. Go on "
            "from there in one short sentence, as \"I\", talking to them, without saying again what they heard.]")


class Spoken:
    """What one reply sends to the speaker across its answer and its re-asks: audio as each response's gate releases
    it, under the text heard so far, then one end."""

    def __init__(self, speak, clock):
        self.speak, self.clock = speak, clock
        self.before, self.text, self.pcm, self.first_t = "", "", bytearray(), None

    def add(self, text, pcm):
        self.text = f"{self.before} {text.strip()}".strip()
        self.pcm += pcm
        if self.first_t is None:
            self.first_t = self.clock()
        if self.speak is not None:
            self.speak(self.text, pcm, False)

    def stopped(self, heard):
        """A response stopped after `heard`: the next one continues from there."""
        self.before = self.text = f"{self.before} {heard}".strip()

    def finish(self, last, rate, since, responses, **more):
        if self.speak is not None and self.pcm:
            self.speak(self.text, b"", True)
        return {**last, **more, "text": self.text, "speech_s": len(self.pcm) / 2 / rate,
                "first_audio_after_s": responses[0]["first_audio_after_s"],
                "first_release_after_s": None if self.first_t is None else self.first_t - since,
                "responses": [{key: r[key] for key in RESPONSE_KEYS} for r in responses]}, bytes(self.pcm)


async def stream_reply(session, rate, since, gate, **response):
    """One response, fed to `gate` event by event as llama.cpp sends it, so its audio is released while the rest is
    generated. Returns the response's summary with the gate's verdict once it is done."""
    mark = len(session.events)
    await session.send(type="response.create", **({"response": response} if response else {}))
    seen, audio_at, done = mark, bisect.bisect_left(session.audio, (mark,)), None
    while done is None:
        await session.wait_for(*STREAM_EVENTS, after=seen)
        while done is None and seen < len(session.events):
            event = session.events[seen]
            kind = event.get("type")
            if kind == "response.output_text.delta":
                gate.on_text(event.get("delta", ""))
            elif kind == "response.output_audio.delta":
                while session.audio[audio_at][0] < seen:
                    audio_at += 1
                gate.on_audio(session.audio[audio_at][1], event["_t"])
            elif kind in ("response.done", "error"):
                gate.finish(event["_t"])
                done = event
            seen += 1
    summary, _ = reply_summary(session, mark, since, rate)
    return {**summary, "passed": gate.passed, "heard": gate.heard(), "releases": gate.releases}, bytes(
        gate.pcm[:gate.sent])


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
    """A `transcribe` for `answer`: the released audio of a stopped reply, at 16 kHz and followed by EAR_PAD_S of
    silence, through llama-server's transcription session."""
    async def transcribe(pcm):
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
        positions = np.arange(0, len(samples), rate / MODEL_RATE)
        audio = np.interp(positions, np.arange(len(samples)), samples).astype("<i2").tobytes()
        return await overlap_transcribe(url, audio + bytes(int(EAR_PAD_S * MODEL_RATE) * 2), Session)
    return transcribe


async def answer(session, rate, since, sources, log, fallback=None, running=False, retry_item=None, escalate=None,
                 speak=None, previous=(), asked_to_choose=False, last_ask=False, transcribe=None, **response):
    """A reply that states only facts the call gave it, never names the machinery, and is not one it already said,
    released to `speak` while it is generated.

    `sources` is what the assistant may say: the person's words in this call and the newest machine note, never its
    own earlier replies. A reply that names a day, date, time, number or place no source states, that talks about
    the note, the check or the task list, that speaks of the person in the third person ("the user wants"), or that
    says it lacks access while `running` holds, is not spoken past the text that passed: `voice/prototype/release.py`
    holds its audio a block behind the screened text. The turn says so in the record, sends one machine note, and
    asks again once. When part of the reply was already heard, the note quotes it and asks the model to go on from
    there (`continue_note`), and the continuation may not open by saying it again. If the second answer is bad too,
    it stops where its screens stopped it, and a turn that has said nothing at all speaks `fallback`'s one short
    line, only if that turn has not said it yet. Nothing is substituted for a wrong value, because a different wrong
    value is worse than saying less.

    `asked_to_choose` says the person handed the assistant a choice ("Pick a city on the east coast to check the
    weather for."). A reply whose only ungrounded value is the place it chose is then spoken, and the place comes back
    on `established` so the call's later turns may name it: measured on the 2026-11-16 call, turns 25, 27 and 28, the
    screen refused the chosen city on every turn and two of them went silent.

    `previous` is what the assistant has already said in this call. A reply that repeats one of those lines word for
    word, or that opens with the line just said, is asked again with `REPEAT_NOTE`; while its start could still be
    such a repeat, none of it is released (`may_repeat`). With `last_ask`, a turn that has still said nothing after
    the retry gets one last ask with a note that takes the content away (`NEW_WORDS_NOTE` after a repeat,
    `FINAL_NOTE` otherwise), so a live turn that would have gone silent may still say one short, uncheckable thing.
    That last answer is not held for repeating, because silence after the person speaks is worse than a stale line.
    A reply already partly heard is not asked a third time: the person is not left in silence.

    `speak(text, pcm, done)` gets each released piece of audio with the text heard so far, then one call with `done`
    set once the reply is over. It is never called for a reply that released nothing. `transcribe(pcm)`, when given,
    reads back what a stopped reply's released audio said, so the continuation note quotes the words the person heard
    rather than the text the audio was released on (`heard_prefix`). Without it, or when it fails or reads nothing,
    the note quotes the text of the released blocks: a continuation that skips a few words is better than one asked
    from the top, which says the heard opening again.

    `retry_item` replaces the machine note a refused reply that released nothing is answered with; given a function,
    it is called with what the screens found and whether the reply only repeated itself, and returns the item.
    `escalate`, when given, is called with the facts a reply stated that nothing gave it: it may start a lookup for
    the person's words and return the note for it, so a fact the model does not have is looked up instead of refused.
    The turn then says it is checking, and the result arrives as a task update."""
    spoken = Spoken(speak, session.clock)

    def check(text, heard="", final=True, repeats=True):
        return screened(text, sources, running, heard, final, previous if repeats else (), asked_to_choose)

    async def ask(heard="", repeats=True):
        gate = Release(lambda text, final: not any(check(text, heard, final, repeats).values()), spoken.add)
        result, released = await stream_reply(session, rate, since, gate, **response)
        result["screened_against"] = {"sources": list(sources), "running": running, "heard": heard,
                                      "previous": list(previous) if repeats else [], "choose": asked_to_choose}
        if released and not result["passed"] and transcribe is not None:
            try:
                transcript = await transcribe(released)
            except Exception as exc:
                log("heard_by_ear_failed", error=repr(exc))
            else:
                by_ear_text = heard_prefix(result["text"], transcript)
                log("heard_by_ear", transcript=transcript, heard=by_ear_text, released_on=result["heard"])
                if by_ear_text and len(by_ear_text) <= len(result["heard"]):
                    result["heard"] = by_ear_text
        return result

    def done(last, responses, **more):
        chosen = chosen_places(spoken.text, sources, running) if asked_to_choose else []
        if chosen:
            log("picked", reply=spoken.text, places=chosen)
            more["established"] = chosen
        return spoken.finish(last, rate, since, responses, **more)

    reply = await ask()
    if reply["passed"]:
        return done(reply, [reply])
    heard, found = reply["heard"], check(reply["text"])
    stale = bool(found.get("repeated"))
    log("fact_check", reply=reply["text"], **found, heard=heard, asked_again=True)
    looked_up = None
    if escalate is not None and found["ungrounded"] and not found["machinery"] and not found["third_person"]:
        looked_up = escalate(found["ungrounded"])
    if looked_up is not None:
        log("escalated", note=looked_up)
        sources, running = [*sources, looked_up], True
    if heard:
        item = plain_text(continue_note(heard, found, running))
    elif looked_up is not None:
        item = plain_text(looked_up)
    elif retry_item is not None:
        item = retry_item(found, stale) if callable(retry_item) else retry_item
    else:
        item = user_text(FACT_CHECK_PREFIX, RUNNING_REJECTED if running else FACT_CHECK_REJECTED)
    await create_item(session, item)
    spoken.stopped(heard)
    retry = await ask(heard)
    responses = [reply, retry]
    if retry["passed"]:
        return done(retry, responses)
    again = check(retry["text"], heard)
    spoken.stopped(retry["heard"])
    if last_ask and not spoken.pcm:
        log("fact_check", reply=retry["text"], **again, asked_twice=True)
        await create_item(session, plain_text(NEW_WORDS_NOTE if again.get("repeated") else FINAL_NOTE))
        fresh = await ask(repeats=False)
        responses.append(fresh)
        if fresh["passed"]:
            if repeated(fresh["text"], previous):
                log("repeat", reply=fresh["text"], spoken=True)
            return done(fresh, responses)
        again, retry = check(fresh["text"], repeats=False), fresh
        spoken.stopped(fresh["heard"])
    blocked = {"reply": retry["text"], **again, "heard": retry["heard"]}
    log("fact_check", **blocked, blocked=True)
    if spoken.pcm or fallback is None or fallback.spoken:
        return done(retry, responses, blocked=blocked)
    fallback.spoken = True
    log("fallback", text=fallback.line, blocked=blocked)
    spoken.add(fallback.line, fallback.pcm)
    return done(retry, responses, blocked=blocked, fallback=True)


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


def decider_arguments():
    """Whether the decider reads the tool's place and day in the same request. On by default; VOICE_DECIDER_ARGS=off
    turns it off. Measured at least as accurate as the word rules on both surfaces: 9 of 10 labeled eval items against
    their 8, and on the real turns it read the place the word rules cannot ("what about San Diego?" has no preposition
    for them), which turned that turn's Portland forecast into a San Diego one."""
    return os.environ.get("VOICE_DECIDER_ARGS", "on").lower() != "off"


def segmenter_for(name, decider, label, tools):
    """The segmenter a turn runs. The hybrid is told the tool question's options, so a dispatch in this turn can use
    the decider's own tool answer; a run with no registry asks nothing about tools."""
    if name != "hybrid" or tools is None:
        return SEGMENTERS[name](decider, label)
    return SEGMENTERS[name](decider, label, tools=tools.choices(), arguments=decider_arguments())


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


def live_attempts(decider):
    """The decider's own HTTP attempts so far, read from the ledger that this decider writes to."""
    return decider.live_calls() if hasattr(decider, "live_calls") else live_calls()


async def run_clip(source, inject, status_only, url, decider, delay_s, out, partial, segmenter_name="hybrid",
                   tools=None, eou="gated", persona=PERSONA, on_voice_event=None, voice=None,
                   snapshot_mode="reply", prior_snapshot=None, speak=None, prior_utterances=(),
                   fallback_pcm=b"", prior_tasks=(), ask_pcm=b"", simulate_tasks=True, final_decider=False,
                   recall="off", context=(), prior_results=(), asr_stagger=1, prior_replies=(), established=(),
                   shadow=False):
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

    `speak(text, pcm, done)` takes each piece of reply audio as its text passes the screens, when the caller streams
    the reply itself, as live mode does, and one call with `done` set at the end of each reply (`answer`). The turn
    writes the audio it released to `CLIP_reply.wav` and `CLIP_update.wav` either way, so a record is readable
    without the client's copy. `prior_utterances` is what the person said earlier in the call, which the fact check
    needs because the shipped snapshot no longer carries their words. The last `HISTORY_TURNS` of them also go into
    the snapshot item as numbered `call_turns`, so a question about what was said earlier has the call's own text to
    read.

    `recall` says how a question about the call reaches the model. "off" is the shipped behaviour, the numbered
    `call_turns` inside the snapshot item. "note" keeps that list and adds one plain sentence naming the turn the
    question asks about (voice/live/recall.py) as the newest item before the reply.

    `snapshot_mode` "note" is live mode's default. The voice session then holds only the call itself: each turn's
    audio and reply, plus one `task_notes` text after the audio on a turn that has a lookup to speak about. No
    snapshot JSON, no list of earlier turns and no recall note go in, because the model answers the newest text it
    reads in place of the audio (measured: 20261116-152653, where replies answered the previous turn's words). A
    refused reply is asked again with one short note, and a reply that fails twice stops where it failed; no fixed
    line is played. A reply that states a fact nothing gave it starts a lookup for the person's words when the
    registry has an agent. `context` is the call so far for that agent, as `ConversationLedger.context` gives it, and
    `prior_results` is every lookup result the call has had, which the fact check accepts as said.
    `prior_replies` is what the assistant already said in this call, which a reply must not repeat word for word, and
    `established` is every place an earlier turn chose at the person's request, which this turn may name.

    `fallback_pcm` is the audio for `FALLBACK_LINE`, rendered by
    the caller; without it a turn that loses both replies still says the line in its record and plays nothing.
    `prior_tasks` is what earlier turns of the call ran, oldest first, as `carried_tasks` gives it. `ask_pcm` is the
    audio for `ASK_INSTEAD_LINE`, which a correction that names no value speaks in place of a composed reply.

    A `prior_tasks` entry with `waiting` is a task an earlier turn could not start for a missing argument, which it
    asked the person for. When this turn's words fill it and name no other tool's subject, that task runs with what
    they said (`MockTaskService.answer`), and the decider is asked nothing: the turn answers the assistant's own
    question, so it is not a new decision."""
    t0 = time.perf_counter()

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
    service = MockTaskService(segmenter, f"conv_{stem}", delay_s, tools, holds_side_effects=eou != "gated",
                              earlier=next((ToolCall(**t["call"]) for t in reversed(prior_tasks) if t.get("call")),
                                           None), simulate=simulate_tasks, context=list(context))
    # The newest task an earlier turn left waiting on a question about its own arguments, as `carried_tasks` gives it.
    waiting = next((t for t in reversed(prior_tasks) if t.get("waiting")), None)
    adapter = StaggeredTranscript(asr_stagger) if asr_stagger > 1 else TranscriptAdapter()
    updates_q: asyncio.Queue = asyncio.Queue()
    final_seen = asyncio.Event()
    loop = asyncio.get_running_loop()
    executor = ThreadPoolExecutor(max_workers=1)
    calls_before = live_attempts(decider)
    closed = {}
    # The shadow call is one more decider question on the final transcript, sent by the caller after the reply has
    # played (voice/live/shadow.py). The state is copied before the turn's own final decision point, so the question
    # is the one `decider` mode would have asked.
    captured = {}
    shadow_request = None

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
                    if shadow:
                        captured.update(segmenter.shadow_state())
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
        hear = by_ear(url, out_rate)
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
        given, picked = [*established], []
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
        choosing = may_choose(adapter.text(), (snapshot or {}).get("tasks"))

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
            tasks = (answered or {}).get("tasks")
            choosing = may_choose(adapter.text(), tasks)
            sources = [adapter.text(), *prior_utterances, *given_results(prior_results, noted, tasks), *given,
                       read_before, item]

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
                    voice, out_rate, audio_end, sources, log, running=running, escalate=look_up, speak=speak,
                    transcribe=hear,
                    previous=prior_replies, asked_to_choose=choosing, last_ask=True,
                    retry_item=lambda found, repeats=False: plain_text(retry_note(found, running, repeats)))
            elif service.correction is ASK_WHAT_CHANGED:
                reply, reply_pcm = fixed_reply(ASK_INSTEAD_LINE, ask_pcm, out_rate, clock() - audio_end), ask_pcm
                if speak is not None and reply_pcm:
                    speak(reply["text"], reply_pcm, True)
            else:
                reply, reply_pcm = await answer(voice, out_rate, audio_end, sources, log, fallback=fallback,
                                                running=is_running(answered), speak=speak, transcribe=hear,
                                                previous=prior_replies,
                                                asked_to_choose=choosing)
            picked.extend(reply.get("established", ()))
            given.extend(reply.get("established", ()))
            replies.append({"kind": "snapshot", **reply})
            log("reply", reply="snapshot", text=reply["text"])
            if reply_pcm:
                write_pcm16(out / f"{stem}_reply.wav", reply_pcm, out_rate)
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
                                            [adapter.text(), *prior_utterances, *given, read_before, item], log,
                                            fallback=fallback, running=is_running(snapshot), speak=speak,
                                            transcribe=hear,
                                            previous=prior_replies, asked_to_choose=choosing, **extra)
            picked.extend(reply.get("established", ()))
            given.extend(reply.get("established", ()))
            replies.append({"kind": "snapshot", **reply})
            log("reply", reply="snapshot", text=reply["text"])
            if reply_pcm:
                write_pcm16(out / f"{stem}_reply.wav", reply_pcm, out_rate)

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
            said = (*prior_replies, *[r["text"] for r in replies if r.get("text")])
            if snapshot_mode == "note":
                injection_update = await create_item(voice, plain_text(updates))
                update_reply, update_pcm = await answer(
                    voice, out_rate, boundary, [adapter.text(), *prior_utterances, *given, updates], log,
                    speak=speak, transcribe=hear, previous=said, last_ask=True,
                    retry_item=lambda found, repeats=False: plain_text(retry_note(found, False, repeats)), **extra)
            else:
                injection_update = await create_item(voice, user_text(UPDATE_PREFIX, updates))
                update_reply, update_pcm = await answer(voice, out_rate, boundary,
                                                        [adapter.text(), *prior_utterances, *given, updates], log,
                                                        fallback=fallback, speak=speak, transcribe=hear,
                                                        previous=said, **extra)
            picked.extend(update_reply.get("established", ()))
            given.extend(update_reply.get("established", ()))
            replies.append({"kind": "update", "injection": injection_update, **update_reply})
            log("reply", reply="update", text=update_reply["text"])
            if update_pcm:
                write_pcm16(out / f"{stem}_update.wav", update_pcm, out_rate)

        if shadow and "tasks" in captured and closed.get("kind") == "close":
            body = segmenter.final_request(captured)
            if body is not None:
                shadow_request = shadow_log.request(
                    utterance=stem, turn_record=f"{stem}.json",
                    authority="decider" if final_decider else "lfm", body=body,
                    lfm=shadow_log.lfm_calls(tool_results),
                    outcomes=[{"task": t["task_id"], "status": t["status"], "instruction": t["instruction"]}
                              for t in segmenter.records()],
                    intent=closed["intent"].intent.value)

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
        "established": list(dict.fromkeys(picked)),
        "task_updates": task_updates,
        "tasks": segmenter.records(),
        "dispatch_log": segmenter.dispatch_log,
        "executions": {k: {**vars(v), "state": v.state.value} for k, v in service.executions.items()},
        "timeline": timeline,
        **({"shadow_request": shadow_request} if shadow_request is not None else {}),
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
    shadow_out = shadow_log.Log(out) if args.shadow == "on" else None
    tools_context = nullcontext() if args.mock_tools else open_registry()
    server_context = Server(args.port, out / "llama-server.log", ("-c", str(args.lfm_ctx)), slots)
    with tools_context as tools, server_context as server, make_decider(args.decider, out) as decider:
        shadow_lane, shadow_decider = shadow_log.Lane(), decider
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
                                        args.segmenter, tools, args.eou, shadow=shadow_out is not None)
            except Exception as exc:
                record = {"clip": wav.name, "error": repr(exc), "metrics": {"error": repr(exc)}, **partial}
            asked = record.pop("shadow_request", None)
            if shadow_out is not None and asked is not None:
                try:
                    shadow_out.write(shadow_log.run(shadow_lane.open(shadow_decider), asked, name=args.decider))
                finally:
                    shadow_lane.finish()
                record["shadow"] = shadow_log.FILE
            record["server_startup_s"] = server.startup_s
            record["mcp_startup_s"] = tools.mcp.startup_s if tools is not None else None
            record["config"] = {"eou": args.eou, "decider": args.decider, "reuse": args.reuse, "fresh": args.fresh,
                                "shadow": args.shadow, "lfm_ctx": args.lfm_ctx, "slots": slots,
                                "vram_mib_after_load": vram_mib}
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
    parser.add_argument("--shadow", choices=["on", "off"], default="off",
                        help="after each turn's reply, ask the decider's end-of-utterance question and log it, "
                             "acting on nothing (default off: a research run's recorded call counts stay comparable)")
    parser.add_argument("--decider-cache", type=Path, default=EVIDENCE / "decider-cache",
                        help="decider cache and call ledger, kept out of the repo")
    parser.add_argument("--decider-budget", type=int, default=DECIDER_BUDGET,
                        help="stop when the ledger holds this many attempts")
    args = parser.parse_args()
    if args.eou in TOOL_SESSIONS and (args.inject != "c" or args.segmenter != "hybrid"):
        parser.error(f"--eou {args.eou} injects as in c and needs the hybrid segmenter")
    if args.fresh and args.decider not in ("cloud", "liquid"):
        parser.error("--fresh resends cached cloud or liquid requests; the d1 decider has no such cache")
    if args.shadow == "on" and args.eou != "gated":
        parser.error("--shadow runs in gated mode only")
    missing = [str(c) for c in args.clips if not c.is_file()]
    if missing:
        parser.error(f"no such clip: {', '.join(missing)}")
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
