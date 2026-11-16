"""The local voice-assistant prototype, end to end: a WAV in, a spoken reply and a record of every event out.

    python -m voice.prototype.run CLIP.wav [CLIP.wav ...] [--eou gated|handoff|decider] [--inject a|b|c]
        [--segmenter hybrid|baseline] [--fresh] [--lfm-ctx N] [--out DIR] [--mock-tools]
        [--decider-cache DIR --decider-budget N]

The clip streams at real-time pace into two sessions on one llama-server: a `realtime` voice session and a
`?intent=transcription` session. Transcription deltas feed the segmenter, the segmenter's dispatches feed the mock
task service, and at the turn boundary the task snapshot goes into the voice session before it replies. Mock tasks
that finish after the reply reach the voice as a `task_update` at the next boundary.

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
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from pathlib import Path

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
                   tools=None, eou="gated", persona=PERSONA, on_voice_event=None):
    """One turn. `source` is a WavInput or voice/live's microphone turn: it has `name`, `stem`, `rate`, `audio_s`
    once streamed, and `stream(voice, asr)`, which returns the clock time the input ended."""
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

    voice = await Session.open(url, "voice", clock, on_event=on_voice_event)
    asr = await Session.open(url + "?intent=transcription", "asr", clock, on_event=on_asr)
    def partial_close(update):
        return segmenter.close(update, settle=False)

    tool_session = await Session.open(url, "tools", clock) if eou in TOOL_SESSIONS else None
    worker = None
    try:
        if tool_session is not None:
            await tool_session.wait_for("session.created")
            await tool_session.send(type="session.update", session={
                "type": "realtime", "instructions": TOOL_SESSIONS[eou][0], "tools": TOOL_SESSIONS[eou][1],
                "output_modalities": ["text"]})
            await tool_session.wait_for("session.updated")
        created = await voice.wait_for("session.created")
        out_rate = created["session"]["audio"]["output"]["format"]["rate"]
        partial["events"] = {"voice": voice.events, "voice_sent": voice.sent, "asr": asr.events, "asr_sent": asr.sent}
        await asr.wait_for("session.created")
        rate = source.rate
        session = {"type": "realtime", "instructions": persona + FORMAT_LINES[inject],
                   "output_modalities": ["text", "audio"]}
        if inject == "b":
            session["tools"] = TOOLS[1:] if status_only else TOOLS
        if rate != MODEL_RATE:
            session["audio"] = {"input": {"format": {"type": "audio/pcm", "rate": rate}}}
            await asr.send(type="session.update", session={"audio": session["audio"]})
            await asr.wait_for("session.updated")
        await voice.send(type="session.update", session=session)
        await voice.wait_for("session.updated")
        worker = asyncio.create_task(segment_worker())

        audio_end = await source.stream(voice, asr)
        audio_s = source.audio_s
        log("audio_end", audio_s=audio_s)
        adapter.end_input()
        await asr.send(type="input_audio_buffer.commit", final=True)
        await voice.send(type="input_audio_buffer.commit")

        replies, tool_turn, injection = [], None, []
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
            injection.append(await create_item(voice, user_text(SNAPSHOT_PREFIX, answered)))
            log("injection", items=injection)
            reply, reply_pcm = await respond(voice, out_rate, audio_end)
            replies.append({"kind": "snapshot", **reply})
            log("reply", reply="snapshot", text=reply["text"])
            write_pcm16(out / f"{stem}_reply.wav", reply_pcm, out_rate)
        elif inject == "a":
            call = {"type": "function_call", "call_id": SYNTHETIC_CALL_ID, "name": "get_task_status", "arguments": "{}"}
            injection.append(await create_item(voice, call))
            injection.append(await create_item(voice, {"type": "function_call_output", "call_id": SYNTHETIC_CALL_ID,
                                                       "output": json.dumps(snapshot)}))
        elif inject == "b":
            for call in tool_turn["function_calls"]:
                injection.append(await create_item(voice, {"type": "function_call_output", "call_id": call["call_id"],
                                                           "output": json.dumps(snapshot)}))
            if not tool_turn["function_calls"]:
                injection.append({"item_type": "function_call_output", "accepted": False,
                                  "answer": "the model made no call"})
        else:
            injection.append(await create_item(voice, user_text(SNAPSHOT_PREFIX, snapshot)))
        if eou not in TOOL_SESSIONS:
            log("injection", items=injection)
            reply, reply_pcm = await respond(voice, out_rate, audio_end, **extra)
            replies.append({"kind": "snapshot", **reply})
            log("reply", reply="snapshot", text=reply["text"])
            write_pcm16(out / f"{stem}_reply.wav", reply_pcm, out_rate)

        while (due := service.next_due()) is not None:
            await wait_for_work(service, due - clock())
            for task_id in service.poll(clock()):
                log("service", task_id=task_id, event=service.executions[task_id].state.value)
        task_updates = service.take_updates()
        if task_updates:
            boundary = clock()
            log("task_updates", updates=task_updates)
            injection_update = await create_item(voice, user_text(UPDATE_PREFIX, {"updates": task_updates}))
            update_reply, update_pcm = await respond(voice, out_rate, boundary, **extra)
            replies.append({"kind": "update", "injection": injection_update, **update_reply})
            log("reply", reply="update", text=update_reply["text"])
            write_pcm16(out / f"{stem}_update.wav", update_pcm, out_rate)

    finally:
        if worker is not None:
            worker.cancel()
        executor.shutdown(wait=False, cancel_futures=True)
        service.close()
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
        "events": {"voice": voice.events, "voice_sent": voice.sent, "asr": asr.events, "asr_sent": asr.sent,
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
