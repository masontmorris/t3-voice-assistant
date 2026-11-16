"""Replay recorded hybrid runs with a Prewriter attached, from the decider cache only.

    python -m voice.delegation.replay scenarios [--latency 0.5] --out FILE
    python -m voice.delegation.replay clips RUN_DIR [--latency 0.5 1 2] --out DIR

The author is a stand-in that returns a valid draft of the instruction after a fixed latency, so the replay
measures timing and staleness, not draft quality. Scenarios have no clock, so decision point i happens at time i,
and a latency below 1 means a draft lands before the next point. Clip records carry the live run's timeline, so a
clip's decision points happen at the seconds they finished in that run. Clip output holds transcripts, so --out
must be outside the repo there. The printed table holds numbers only.
"""

import argparse
import heapq
import json
import statistics
from pathlib import Path

from voice.delegation.draft import Prewriter
from voice.segmentation.contract import TranscriptUpdate
from voice.segmentation.hybrid import HybridSegmenter
from voice.segmentation.run import OfflineClient

ROOT = Path(__file__).resolve().parents[1]
SCENARIO_DIRS = {"original": ROOT / "segmentation" / "scenarios", "heldout": ROOT / "segmentation" / "scenarios_heldout"}


def stand_in(source):
    return {"instruction": source.instruction, "context": "", "constraints": [],
            "success_criteria": ["The request above is done."], "open_questions": []}


def simulate(steps, label, latency):
    """Run (time, event) steps through a hybrid and a Prewriter. Returns one row per dispatched task."""
    seg, pre = HybridSegmenter(OfflineClient(), label), Prewriter()
    pending, order, first_done, final_t = [], 0, {}, None
    rows = []

    def send(sources, now):
        nonlocal order
        for source in sources:
            order += 1
            heapq.heappush(pending, (now + latency, order, source))

    def land_until(now):
        while pending and pending[0][0] <= now:
            _, _, source = heapq.heappop(pending)
            send(pre.land(source, stand_in(source), seg.orch.tasks[source.task_id]), now)

    for now, event in steps:
        land_until(now)
        before = len(seg.dispatch_log)
        if event["type"] == "delegate":
            point = seg.submit_delegate(event["instruction"])
        else:
            update = TranscriptUpdate(event["revision"], event["text"], event["is_final"])
            final_t = now if update.is_final else final_t
            point = seg.update(update)
        if point is not None and point["applied"]:
            send(pre.observe(point, seg.orch.tasks, seg.last.text), now)
            for task_id in pre.started:
                first_done.setdefault(task_id, now)
        for entry in seg.dispatch_log[before:]:
            task = seg.orch.tasks[entry["task_id"]]
            choice = pre.dispatch(task)
            landing = next((t for t, _, s in pending if s.task_id == task.task_id
                            and s.instruction == task.instruction), None)
            rows.append({"task_id": task.task_id, "used_draft": choice.used_draft, "reason": choice.reason,
                         "is_final": entry["is_final"], "dispatched_at": now, "first_done": first_done.get(task.task_id),
                         "wait_for_draft": 0.0 if choice.used_draft else round(
                             latency if landing is None else landing - now, 4),
                         "requests": sum(s.task_id == task.task_id for s in pre.requests)})
    for row in rows:
        row["final_at"] = final_t
    return rows, pre, seg


def scenario_steps(scenario):
    return list(enumerate(e for e in scenario["events"]))


def clip_steps(record):
    """Transcript updates the live run processed, each at the time its decision point finished, else when it came."""
    coalesced = {r for e in record["timeline"] if e["kind"] == "coalesced" for r in e["revisions"]}
    done = {e["revision"]: e["t"] for e in record["timeline"]
            if e["kind"] == "decision_point" and e["source"] == "update"}
    return [(done.get(e["revision"], e["t"]), {"type": "transcript", **{k: e[k] for k in ("revision", "text", "is_final")}})
            for e in record["timeline"] if e["kind"] == "transcript_update" and e["revision"] not in coalesced]


def summarize(rows, utterances, requests, drafted):
    used = [r for r in rows if r["used_draft"]]
    gaps = [r["dispatched_at"] - r["first_done"] for r in rows if r["first_done"] is not None]
    waits = [r["wait_for_draft"] for r in rows if r["wait_for_draft"] is not None]
    return {"utterances": utterances, "dispatched": len(rows), "with_draft": len(used),
            "raw_no_draft_started": sum(r["reason"] == "no draft started" for r in rows),
            "raw_still_drafting": sum(r["reason"] == "still drafting" for r in rows),
            "raw_stale": sum(r["reason"] == "stale" for r in rows),
            "requests": requests, "requests_per_utterance": round(requests / utterances, 2) if utterances else None,
            "drafted_tasks": drafted, "redrafts": requests - drafted,
            "gap_done_to_dispatch": _spread(gaps), "wait_if_holding_for_draft": _spread(waits)}


def _spread(values):
    if not values:
        return None
    values = sorted(values)
    return {"n": len(values), "median": round(statistics.median(values), 3), "min": round(values[0], 3),
            "max": round(values[-1], 3)}


def run_scenarios(latency):
    out = {}
    for name, directory in SCENARIO_DIRS.items():
        rows, requests, drafted, per = [], 0, 0, {}
        paths = sorted(p for p in directory.glob("*.json"))
        for path in paths:
            scenario = json.loads(path.read_text(encoding="utf-8"))
            got, pre, _ = simulate(scenario_steps(scenario), scenario["id"], latency)
            per[scenario["id"]] = {"rows": got, "requests": len(pre.requests)}
            rows += got
            requests += len(pre.requests)
            drafted += len({s.task_id for s in pre.requests})
        out[name] = {"summary": summarize(rows, len(paths), requests, drafted), "scenarios": per}
    return out


def run_clips(run_dir, latencies):
    from voice.prototype.report import VOICE_HOME
    from voice.prototype.segmenter import DECIDER_BUDGET, use_decider_cache
    use_decider_cache(VOICE_HOME / "evidence" / "prototype" / "decider-cache", DECIDER_BUDGET)
    records = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(run_dir.glob("*.json"))
               if p.name != "summary.json"]
    out, same = {}, True
    for latency in latencies:
        rows, requests, drafted, per = [], 0, 0, {}
        for record in records:
            stem = Path(record["clip"]).stem
            got, pre, seg = simulate(clip_steps(record), stem, latency)
            same &= seg.records() == record["tasks"] and seg.dispatch_log == record["dispatch_log"]
            audio_end = record["audio_end"]
            for row in got:
                row["after_audio_end"] = round(row["dispatched_at"] - audio_end, 4)
            per[stem] = {"rows": got, "requests": len(pre.requests)}
            rows += got
            requests += len(pre.requests)
            drafted += len({s.task_id for s in pre.requests})
        out[str(latency)] = {"summary": {**summarize(rows, len(records), requests, drafted), "same_as_run": same},
                             "clips": per}
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("what", choices=["scenarios", "clips"])
    parser.add_argument("run", type=Path, nargs="?")
    parser.add_argument("--latency", type=float, nargs="+", default=[0.5])
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.what == "scenarios":
        result = run_scenarios(args.latency[0])
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
        summaries = {k: v["summary"] for k, v in result.items()}
    else:
        result = run_clips(args.run, args.latency)
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / "prewrite-replay.json").write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
        summaries = {k: v["summary"] for k, v in result.items()}
    print(json.dumps(summaries, indent=1))


if __name__ == "__main__":
    main()
