"""Run one recorded prototype clip end to end through the standing root, with no audio and no GPU.

    python -I voice/bot/seam_replay.py --checkout DIR --run RUN_DIR --clip STEM --out DIR [--bot URL] [--timeout S]

--checkout is a local checkout of a prototype branch (voice/stage2-hybrid). The clip's recorded transcript updates go
through that branch's hybrid segmenter, with decider answers from the prototype cache only (a cache miss is refused).
Its dispatches go to RootTaskService, so a read-only task travels root, PM, worker and back through the outbox, and a
side-effect task stays held. The record goes to --out, which must be outside the repo because it holds the transcript.
"""

import argparse
import json
import sys
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--checkout", type=Path, required=True)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--clip", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--bot", default="http://127.0.0.1:8787")
    parser.add_argument("--conversation")
    parser.add_argument("--timeout", type=float, default=300)
    args = parser.parse_args()
    sys.path[:0] = [str(args.checkout.resolve()), str(Path(__file__).resolve().parent)]

    from root_tasks import RootTaskService
    from voice.research.prototype.replay import updates
    from voice.research.prototype.report import VOICE_HOME
    from voice.prototype.segmenter import DECIDER_BUDGET, SEGMENTERS, use_decider_cache
    from voice.segmentation.run import OfflineClient

    use_decider_cache(VOICE_HOME / "evidence" / "prototype" / "decider-cache", DECIDER_BUDGET)
    record = json.loads((args.run / f"{args.clip}.json").read_text(encoding="utf-8"))
    segmenter = SEGMENTERS["hybrid"](OfflineClient(), args.clip)
    service = RootTaskService(segmenter, args.conversation or f"conv_{args.clip}", args.bot)
    t0 = time.monotonic()
    clock = lambda: round(time.monotonic() - t0, 3)
    timeline = []
    for update in updates(record, as_run=True):
        segmenter.update(update)
        for event in service.sync(clock()):
            timeline.append({"t": clock(), "kind": "service", **event})
    service.poll(clock())
    snapshot = service.snapshot("utt_1", record["final_transcript"])
    timeline.append({"t": clock(), "kind": "snapshot", "snapshot": snapshot})
    while service.next_due() is not None and clock() < args.timeout:
        for task_id in service.poll(clock(), wait_s=min(30.0, args.timeout - clock())):
            timeline.append({"t": clock(), "kind": "service", "task_id": task_id, "event": "completed"})
    task_updates = service.take_updates()
    timeline.append({"t": clock(), "kind": "task_updates", "updates": task_updates})
    bot_snapshot = service._call("GET", "/snapshot?" + __import__("urllib.parse").parse.urlencode(
        {"conversation": service.conversation_id, "utterance": "utt_2", "transcript": "", "prefix": service.conversation_id + ":"}))
    args.out.mkdir(parents=True, exist_ok=True)
    out = {"clip": args.clip, "conversation_id": service.conversation_id, "decider": "OfflineClient: cache hits only, misses refused",
           "tasks": segmenter.records(), "dispatch_log": segmenter.dispatch_log,
           "executions": {k: {**vars(v), "state": v.state.value} for k, v in service.executions.items()},
           "timeline": timeline, "bot_snapshot": bot_snapshot}
    (args.out / f"{args.clip}.json").write_text(json.dumps(out, indent=1) + "\n", encoding="utf-8")
    print("segmenter decider: cache only (OfflineClient refuses a miss, so this run makes no live call)")
    for task_id, e in service.executions.items():
        print(f"{task_id}: {e.state.value} after {e.finished_at if e.finished_at is not None else '-'} s")
    print(f"snapshot at end of utterance: {[(t['task_id'], t['status']) for t in snapshot['tasks']]}")
    print(f"task_update count {len(task_updates)}; bot snapshot tasks {[(t['task_id'], t['status']) for t in bot_snapshot['tasks']]}")


if __name__ == "__main__":
    main()
