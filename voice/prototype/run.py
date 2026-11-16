"""The local voice-assistant prototype, end to end: a WAV in, a spoken reply and a record of every event out.

    python -m voice.prototype.run CLIP.wav [CLIP.wav ...] [--inject a|b|c] [--segmenter hybrid|baseline] [--out DIR]

The clip streams at real-time pace into two sessions on one llama-server: a `realtime` voice session and a
`?intent=transcription` session. Transcription deltas feed the segmenter, the segmenter's dispatches feed the mock
task service, and at the turn boundary the task snapshot goes into the voice session before it replies. Mock tasks
that finish after the reply reach the voice as a `task_update` at the next boundary.

--inject picks how the snapshot enters the voice session:
  a  a client-made `function_call` item plus its `function_call_output`
  b  the output of the model's own `delegate` or `get_task_status` call (the first response must call a tool);
     --status-only offers just `get_task_status`
  c  a user text message with a fixed machine prefix

--decider picks the decider: cloud (pplx-decider-flash on OpenRouter, the default, or $VOICE_DECIDER) or d1 (d1-2B on
a second local llama-server, see voice/local_decider/). --reuse asks only the questions whose answers it has not kept.
--lfm-ctx sets LFM3's total context (-c); left out, the server takes the model's 128k.

--segmenter picks the segmenter, the hybrid by default. The segmenter takes transcript updates one at a time. When
newer updates queue behind a decision, only the newest is processed, and the timeline logs the dropped revisions.

Needs the GPU (run it inside the lease). The cloud decider needs VOICE_ENV_FILE or OPENROUTER_API_KEY for calls
that miss the cache. Writes CLIP.json, CLIP_reply.wav, and CLIP_update.wav to --out.
"""

import argparse
import asyncio
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from voice.local_decider.bench import gpu_used_mib
from voice.local_decider.client import decider as make_decider, default_decider
from voice.local_decider.reuse import ReusingClient
from voice.prototype.realtime import MODEL_RATE, VOICE_HOME, Server, Session, read_pcm16, write_pcm16
from voice.prototype.segmenter import DECIDER_BUDGET, SEGMENTERS, coalesce, live_calls, use_decider_cache
from voice.prototype.tasks import MockTaskService
from voice.prototype.transcript import TranscriptAdapter

EVIDENCE = VOICE_HOME / "evidence" / "prototype"
PERSONA = (
    "You are a friendly voice assistant. A separate task service carries out what the user asks for. "
    "After the user speaks, you receive a task status snapshot as JSON. Talk to the user directly, as \"you\", in "
    "one or two short sentences, and say only what the snapshot says. running means the task has started. pending "
    "means it is waiting. completed means it is done, and you may say its result_summary. held_for_confirmation "
    "means you need the user's OK before doing it. needs_clarification means you must ask the user. Never say a task "
    "is done unless its status is completed, never promise a step the snapshot does not show, and never make up a "
    "result."
)
INSTRUCTIONS = {
    "a": PERSONA + "\n\nRespond with interleaved text and audio.",
    "b": PERSONA + " Always call a tool first.\n\nRespond with function calls, and interleaved text and audio."
                   "\n\nList of tools:",
    "c": PERSONA + "\n\nRespond with interleaved text and audio.",
}
TOOLS = [
    {"type": "function", "name": "delegate", "description": "Hand a request to the task service.",
     "parameters": {"type": "object", "properties": {"instruction": {"type": "string"}}, "required": ["instruction"]}},
    {"type": "function", "name": "get_task_status", "description": "Get the status of the user's tasks.",
     "parameters": {"type": "object", "properties": {}}},
]
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


async def run_clip(wav, inject, status_only, url, decider, delay_s, out, partial, segmenter_name="hybrid"):
    t0 = time.perf_counter()

    def clock():
        return time.perf_counter() - t0

    stem = wav.stem
    timeline = []
    partial["timeline"] = timeline

    def log(kind, **detail):
        timeline.append({"t": round(clock(), 4), "kind": kind, **detail})

    segmenter = SEGMENTERS[segmenter_name](decider, stem)
    service = MockTaskService(segmenter, f"conv_{stem}", delay_s)
    adapter = TranscriptAdapter()
    updates_q: asyncio.Queue = asyncio.Queue()
    final_seen = asyncio.Event()
    loop = asyncio.get_running_loop()
    executor = ThreadPoolExecutor(max_workers=1)
    calls_before = live_calls()

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
                work = segmenter.update if kind == "update" else service.submit_delegate
                try:
                    point = await loop.run_in_executor(executor, work, payload)
                except Exception as exc:
                    log("segmenter_error", error=repr(exc))
                    point = None
                if point is not None:
                    log("decision_point", source=kind, revision=point["revision"], is_final=point["is_final"],
                        started=round(started, 4), live_calls=live_calls() - before, calls=point["calls"],
                        problems=point["problems"],
                        segments=[s["text"] for s in point["segments"]], decisions=point["decisions"])
                for event in service.sync(clock()):
                    log("service", **event)
            for _ in batch:
                updates_q.task_done()

    voice = await Session.open(url, "voice", clock)
    asr = await Session.open(url + "?intent=transcription", "asr", clock, on_event=on_asr)
    worker = None
    try:
        created = await voice.wait_for("session.created")
        out_rate = created["session"]["audio"]["output"]["format"]["rate"]
        partial["events"] = {"voice": voice.events, "voice_sent": voice.sent, "asr": asr.events, "asr_sent": asr.sent}
        await asr.wait_for("session.created")
        pcm, rate, audio_s = read_pcm16(wav)
        session = {"type": "realtime", "instructions": INSTRUCTIONS[inject], "output_modalities": ["text", "audio"]}
        if inject == "b":
            session["tools"] = TOOLS[1:] if status_only else TOOLS
        if rate != MODEL_RATE:
            session["audio"] = {"input": {"format": {"type": "audio/pcm", "rate": rate}}}
            await asr.send(type="session.update", session={"audio": session["audio"]})
            await asr.wait_for("session.updated")
        await voice.send(type="session.update", session=session)
        await voice.wait_for("session.updated")
        worker = asyncio.create_task(segment_worker())

        (_, voice_end), (_, asr_end) = await asyncio.gather(voice.stream(pcm, rate), asr.stream(pcm, rate))
        audio_end = max(voice_end, asr_end)
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
        snapshot = service.snapshot("utt_1", adapter.text())
        log("snapshot", snapshot=snapshot)

        if inject == "a":
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
        log("injection", items=injection)

        extra = {"tool_choice": "none"} if inject == "b" else {}
        reply, reply_pcm = await respond(voice, out_rate, audio_end, **extra)
        replies.append({"kind": "snapshot", **reply})
        log("reply", reply="snapshot", text=reply["text"])
        write_pcm16(out / f"{stem}_reply.wav", reply_pcm, out_rate)

        while (due := service.next_due()) is not None:
            await asyncio.sleep(max(0.0, due - clock()))
            for task_id in service.poll(clock()):
                log("service", task_id=task_id, event="completed")
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
        executor.shutdown()
        await voice.close()
        await asr.close()
    final_t = next(e["t"] for e in timeline if e["kind"] == "transcript_update" and e["is_final"])
    snapshot_t = next(e["t"] for e in timeline if e["kind"] == "snapshot")
    snapshot_reply = replies[0]
    return {
        "clip": wav.name, "inject": inject, "segmenter": segmenter_name, "status_only": status_only,
        "delay_s": delay_s, "audio_s": audio_s,
        "audio_end": audio_end,
        "metrics": {
            "final_transcript_after_audio_end_s": final_t - audio_end,
            "snapshot_ready_after_audio_end_s": snapshot_t - audio_end,
            "first_reply_audio_after_audio_end_s": snapshot_reply["first_audio_after_s"],
            "decider_live_calls": live_calls() - calls_before,
            "decision_points": sum(e["kind"] == "decision_point" for e in timeline),
            "segmenter_errors": sum(e["kind"] == "segmenter_error" for e in timeline),
            "coalesced_revisions": sum(len(e["revisions"]) for e in timeline if e["kind"] == "coalesced"),
        },
        "final_transcript": adapter.text(),
        "snapshot": snapshot,
        "injection": injection,
        "tool_turn": tool_turn,
        "replies": replies,
        "task_updates": task_updates,
        "tasks": segmenter.records(),
        "dispatch_log": segmenter.dispatch_log,
        "executions": {k: {**vars(v), "state": v.state.value} for k, v in service.executions.items()},
        "timeline": timeline,
        "events": {"voice": voice.events, "voice_sent": voice.sent, "asr": asr.events, "asr_sent": asr.sent},
    }


async def main_async(args):
    use_decider_cache(EVIDENCE / "decider-cache", DECIDER_BUDGET)
    out = args.out or EVIDENCE / f"inject-{args.inject}{'-status' if args.status_only else ''}"
    out.mkdir(parents=True, exist_ok=True)
    lfm_args = ("-c", str(args.lfm_ctx)) if args.lfm_ctx else ()
    with Server(args.port, out / "llama-server.log", lfm_args) as server, make_decider(args.decider, out) as decider:
        print(f"llama-server pid {server.proc.pid} healthy after {server.startup_s:.1f} s", flush=True)
        vram_mib = gpu_used_mib()
        if args.reuse:
            decider = ReusingClient(decider)
        for wav in args.clips:
            partial = {}
            try:
                record = await run_clip(wav, args.inject, args.status_only,
                                        f"ws://127.0.0.1:{args.port}/v1/realtime", decider, args.delay, out, partial,
                                        args.segmenter)
            except Exception as exc:
                record = {"clip": wav.name, "error": repr(exc), "metrics": {"error": repr(exc)}, **partial}
            record["server_startup_s"] = server.startup_s
            record["config"] = {"decider": args.decider, "reuse": args.reuse, "lfm_ctx": args.lfm_ctx,
                                "vram_mib_after_load": vram_mib}
            (out / f"{wav.stem}.json").write_text(json.dumps(record, indent=1) + "\n", encoding="utf-8")
            print(wav.stem, json.dumps(record["metrics"]), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("clips", nargs="+", type=Path)
    parser.add_argument("--inject", choices="abc", default="c")
    parser.add_argument("--status-only", action="store_true", help="with b, offer get_task_status and no delegate")
    parser.add_argument("--segmenter", choices=sorted(SEGMENTERS), default="hybrid")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--delay", type=float, default=4.0, help="seconds a mock task takes (default 4)")
    parser.add_argument("--port", type=int, default=8095)
    parser.add_argument("--decider", choices=["cloud", "d1"], default=default_decider())
    parser.add_argument("--reuse", action="store_true", help="ask only questions with no kept answer")
    parser.add_argument("--lfm-ctx", type=int, help="LFM3's total context; the model's 128k when left out")
    args = parser.parse_args()
    missing = [str(c) for c in args.clips if not c.is_file()]
    if missing:
        parser.error(f"no such clip: {', '.join(missing)}")
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
