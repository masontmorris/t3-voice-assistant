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
snapshot as in c and replies, so it never sees a tool call of its own. handoff is the earlier design with no gate: a
close rule settles the tasks, side effects stay held, and LFM3 gets the tail. decider is the earliest turn: one more
decider call on the final transcript, then the snapshot by --inject. gated and handoff need the hybrid and
--inject c.

Read-only weather and time tasks make real lookups through `voice/tools/registry.py`: Open-Meteo, and the
`mcp-server-time` MCP server over stdio, which runs for the whole run. VOICE_DEFAULT_PLACE is the place for a request
that names none. --mock-tools sends every task to the mock worker instead.

--inject picks how the snapshot enters the voice session under --eou decider:
  a  a client-made `function_call` item plus its `function_call_output`
  b  the output of the model's own `delegate` or `get_task_status` call (the first response must call a tool);
     --status-only offers just `get_task_status`
  c  a user text message with a fixed machine prefix

--decider picks the decider: cloud (pplx-decider-flash on OpenRouter, the default, or $VOICE_DECIDER) or d1 (d1-2B on
a second local llama-server, see voice/local_decider/). --reuse asks only the questions whose answers it has not kept.
--lfm-ctx sets LFM3's total context (-c), 32768 by default.

--segmenter picks the segmenter, the hybrid by default. The segmenter takes transcript updates one at a time. When
newer updates queue behind a decision, only the newest is processed, and the timeline logs the dropped revisions.

--fresh sends every cloud decider request live, even when it is cached, so timings include each call.

Needs the GPU (run it inside the lease). The cloud decider needs VOICE_ENV_FILE or OPENROUTER_API_KEY for calls
that miss the cache. Writes CLIP.json, CLIP_reply.wav, and CLIP_update.wav to --out.
"""

import argparse
import asyncio
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from pathlib import Path

from voice.live.facts import ungrounded
from voice.local_decider.bench import gpu_used_mib
from voice.local_decider.client import decider as make_decider, default_decider
from voice.local_decider.reuse import ReusingClient
from voice.prototype.realtime import MODEL_RATE, VOICE_HOME, Server, Session, read_pcm16, write_pcm16
from voice.prototype.segmenter import (DECIDER_BUDGET, MAX_429_SHARE, SEGMENTERS, FreshClient, coalesce, live_calls,
                                       rate_limited_share, use_decider_cache)
from voice.prototype.tasks import MockTaskService
from voice.segmentation.gate import PLAN_A
from voice.prototype.transcript import TranscriptAdapter
from voice.tools.registry import open_registry

EVIDENCE = VOICE_HOME / "evidence" / "prototype"
PERSONA = (
    "You are a friendly voice assistant. A separate task service carries out what the user asks for. "
    "After the user speaks, you receive a task status snapshot as JSON. Talk to the user directly, as \"you\", in "
    "one or two short sentences, and say only what the snapshot says. running means the task has started. pending "
    "means it is waiting on the service, not waiting for the user's permission. An explicit request in this turn "
    "authorizes carrying it out, including external actions; do not ask again just because it is external. "
    "completed means it is done, and you may say its result_summary. failed means it could not be "
    "done, and you may say its result_summary. held_for_confirmation "
    "means you need the user's OK before doing it. needs_clarification means you must ask the user. Never say a task "
    "is done unless its status is completed, never promise a step the snapshot does not show, and never make up a "
    "result."
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
FACT_CHECK_REJECTED = {
    "type": "fact_check",
    "result": "rejected",
    "reason": "the last reply named a day, date, time, number or place that nothing in this call said or stated",
    "instruction": "Answer again with only facts the person said or this note states. If you do not have a value, "
                   "say plainly that you do not have it. Never guess one.",
}
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


async def answer(session, rate, since, sources, log, **response):
    """A reply that states only facts the call gave it.

    `sources` is what the assistant may say: the person's words in this call and the newest machine note, never its
    own earlier replies. A reply that names a day, date, time, number or place no source states is not spoken: the
    turn says so in the record, sends one machine note saying the value is not available, and asks again once. A
    second bad answer is dropped rather than said, and the record keeps both texts. Nothing is substituted for a
    wrong value, because a different wrong value is worse than saying less."""
    reply, pcm = await respond(session, rate, since, **response)
    missing = ungrounded(reply["text"], sources)
    if not missing:
        return reply, pcm
    log("fact_check", reply=reply["text"], ungrounded=[f"{fact.kind} {fact.surface}" for fact in missing],
        asked_again=True)
    await create_item(session, user_text(FACT_CHECK_PREFIX, FACT_CHECK_REJECTED))
    retry, retry_pcm = await respond(session, rate, since, **response)
    again = ungrounded(retry["text"], sources)
    if not again:
        return retry, retry_pcm
    log("fact_check", reply=retry["text"], ungrounded=[f"{fact.kind} {fact.surface}" for fact in again], blocked=True)
    return {**retry, "text": "", "blocked": {"reply": retry["text"],
                                             "ungrounded": [f"{fact.kind} {fact.surface}" for fact in again]}}, b""


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


async def run_clip(source, inject, status_only, url, decider, delay_s, out, partial, segmenter_name="hybrid",
                   tools=None, eou="gated", persona=PERSONA, on_voice_event=None, voice=None,
                   snapshot_mode="reply", prior_snapshot=None, seed=None, speak=None, prior_utterances=()):
    """One turn. `source` is a WavInput or voice/live's microphone turn: it has `name`, `stem`, `rate`, `audio_s`
    once streamed, and `stream(voice, asr)`, which returns the clock time the input ended.

    `voice` is an already-open voice session the caller keeps across turns, as live mode does for a call. The turn
    then adds its audio, its injections and its replies to that session's conversation instead of opening one, and
    leaves it open. Its session settings are sent only when they change, so a reused session keeps its history. The
    per-turn `asr` and `tools` sessions are still opened and closed here. `seed` is an item for a session the caller
    has just opened. It goes in after the settings, because llama.cpp refuses new instructions once a session holds a
    message, and before the turn's audio. A refused session.update raises SessionRefused with the server's error.

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
    the shipped snapshot no longer carries their words."""
    t0 = time.perf_counter()

    def clock():
        return time.perf_counter() - t0

    stem = source.stem
    timeline = []
    partial["timeline"] = timeline

    def log(kind, **detail):
        timeline.append({"t": round(clock(), 4), "kind": kind, **detail})

    segmenter = SEGMENTERS[segmenter_name](decider, stem)
    if eou == "gated":
        segmenter.orch.gate, segmenter.orch.side_effects_wait_for_final = PLAN_A, False
    service = MockTaskService(segmenter, f"conv_{stem}", delay_s, tools, holds_side_effects=eou != "gated")
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
                    log("close", revision=point["revision"], started=round(started, 4),
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
        return segmenter.close(update, settle=False)

    tool_session = await Session.open(url, "tools", clock) if eou in TOOL_SESSIONS else None
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

        replies, tool_turn, injection = [], None, []
        if seed is not None:
            injection.append(await create_item(voice, seed))
            log("seed", answer=injection[-1])
        if snapshot_mode == "before-audio" and prior_snapshot is not None:
            injection.append(await create_item(voice, user_text(SNAPSHOT_PREFIX, prior_snapshot)))
            log("snapshot_before_audio", snapshot=prior_snapshot)
        audio_end = await source.stream(voice, asr)
        audio_s = source.audio_s
        log("audio_end", audio_s=audio_s)
        adapter.end_input()
        await asr.send(type="input_audio_buffer.commit", final=True)
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
            for_tools = (service.snapshot("utt_1", adapter.text(), unreviewed, notes=True) if eou == "gated"
                         else snapshot)
            injection.append(await create_item(tool_session, user_text(SNAPSHOT_PREFIX, for_tools)))
            tool_turn, _ = await respond(tool_session, out_rate, audio_end, tool_choice="auto")
            log("tool_turn", calls=tool_turn["function_calls"], text=tool_turn["text"])
            for call in tool_turn["function_calls"]:
                result, events = service.call_tool(call.get("name"), parse_arguments(call), unreviewed, clock())
                for event in events:
                    log("service", **event)
                tool_results.append({"name": call.get("name"), "arguments": parse_arguments(call), "result": result})
                log("tool_call", **tool_results[-1])
            service.poll(clock())
            answered = service.snapshot("utt_1", adapter.text(), unreviewed)
            log("snapshot_after_tools", snapshot=answered)
            if (item := shown(answered, snapshot_mode, adapter.text())) is not None:
                injection.append(await create_item(voice, user_text(SNAPSHOT_PREFIX, item)))
            log("injection", items=injection)
            sources = [adapter.text(), *prior_utterances, read_before, item]
            reply, reply_pcm = await answer(voice, out_rate, audio_end, sources, log)
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
                                            [adapter.text(), *prior_utterances, read_before, item], log, **extra)
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
        if updates is not None:
            boundary = clock()
            log("task_updates", updates=task_updates)
            injection_update = await create_item(voice, user_text(UPDATE_PREFIX, updates))
            update_reply, update_pcm = await answer(voice, out_rate, boundary,
                                                    [adapter.text(), *prior_utterances, updates], log, **extra)
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
    parser.add_argument("--decider", choices=["cloud", "d1"], default=default_decider())
    parser.add_argument("--reuse", action="store_true", help="ask only questions with no kept answer")
    parser.add_argument("--fresh", action="store_true", help="send cached cloud decider requests again, for timing")
    parser.add_argument("--lfm-ctx", type=int, default=32768, help="LFM3's total context (default 32768)")
    parser.add_argument("--mock-tools", action="store_true", help="no real lookups: every task runs on the mock")
    parser.add_argument("--decider-cache", type=Path, default=EVIDENCE / "decider-cache",
                        help="decider cache and call ledger, kept out of the repo")
    parser.add_argument("--decider-budget", type=int, default=DECIDER_BUDGET,
                        help="stop when the ledger holds this many attempts")
    args = parser.parse_args()
    if args.eou in TOOL_SESSIONS and (args.inject != "c" or args.segmenter != "hybrid"):
        parser.error(f"--eou {args.eou} injects as in c and needs the hybrid segmenter")
    if args.fresh and args.decider != "cloud":
        parser.error("--fresh resends cached cloud requests; the d1 decider has no such cache")
    missing = [str(c) for c in args.clips if not c.is_file()]
    if missing:
        parser.error(f"no such clip: {', '.join(missing)}")
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
