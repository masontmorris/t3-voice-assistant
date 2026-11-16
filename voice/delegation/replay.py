"""Replay recorded hybrid runs with a Prewriter attached, from the decider cache only.

    python -m voice.delegation.replay scenarios [--author stand-in|commandcode] [--model ID] [--latency 0.5] --out FILE
    python -m voice.delegation.replay clips RUN_DIR [--author stand-in|commandcode] [--model ID] [--latency 0.5 1]
        --out DIR

The stand-in author returns a valid draft of the instruction, so it measures timing and staleness, not quality.
`--author commandcode` sends each draft request to Command Code through `CachedAuthor`, with the key file named by
VOICE_AUTHOR_KEY_FILE, and keeps every draft for review. Scenarios have no clock, so decision point i happens at
time i, every draft lands `--latency` after its request, and a latency below 1 means it lands before the next point.
A clip's decision points happen at the seconds they finished in the live run. A live clip draft lands after the
latency its own request measured, and a stand-in clip draft after each `--latency`. Clip output and the clip
author cache hold transcripts, so they live under $VOICE_HOME, outside the repo.
"""

import argparse
import heapq
import json
import statistics
from pathlib import Path

from dataclasses import asdict

from voice.delegation.author import CachedAuthor, author_request
from voice.delegation.draft import Prewriter, parse_draft, unwrap
from voice.segmentation.contract import TranscriptUpdate
from voice.segmentation.hybrid import HybridSegmenter
from voice.segmentation.run import OfflineClient

ROOT = Path(__file__).resolve().parents[1]
CACHE = Path(__file__).resolve().parent / "cache"
SCENARIO_DIRS = {"original": ROOT / "segmentation" / "scenarios", "heldout": ROOT / "segmentation" / "scenarios_heldout"}


def stand_in(source):
    return {"instruction": source.instruction, "context": "", "constraints": [],
            "success_criteria": ["The request above is done."], "open_questions": []}, None


def simulate(steps, label, latency, author=stand_in, measured=False):
    """Run (time, event) steps through a hybrid and a Prewriter. Returns one row per dispatched task.

    `author` maps a DraftInput to (JSON answer, seconds it took or None). An answer lands `latency` after its
    request, or after the seconds it took when `measured` is set.
    """
    seg, pre = HybridSegmenter(OfflineClient(), label), Prewriter()
    pending, order, first_done, final_t = [], 0, {}, None
    rows = []

    def send(sources, now):
        nonlocal order
        for source in sources:
            order += 1
            content, seconds = author(source)
            delay = seconds if measured and seconds is not None else latency
            heapq.heappush(pending, (now + delay, order, source, content))

    def land_until(now):
        while pending and pending[0][0] <= now:
            _, _, source, content = heapq.heappop(pending)
            send(pre.land(source, content, seg.orch.tasks[source.task_id]), now)

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
            landing = next((t for t, _, s, _ in pending if s.task_id == task.task_id
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
            "raw_rejected": sum(r["reason"] == "rejected" for r in rows),
            "requests": requests, "requests_per_utterance": round(requests / utterances, 2) if utterances else None,
            "drafted_tasks": drafted, "redrafts": requests - drafted,
            "gap_done_to_dispatch": _spread(gaps), "wait_if_holding_for_draft": _spread(waits)}


def _spread(values):
    if not values:
        return None
    values = sorted(values)
    return {"n": len(values), "median": round(statistics.median(values), 3), "min": round(values[0], 3),
            "max": round(values[-1], 3)}


def live(author, label, landed):
    """An author function over a CachedAuthor that keeps each landed draft's record for the quality review."""
    def write(source):
        record = author.write(author_request(source), label=f"{label}:{source.task_id}:r{source.revision}")
        draft, problems = parse_draft(record["content"]) if record["status"] == 200 else (None, (record["error"],))
        landed.append({"label": record["label"], "instruction": source.instruction, "depends_on": source.depends_on,
                       "side_effect": source.side_effect, "status": record["status"], "ms": round(record["ms"], 1),
                       "served_model": record.get("served_model"), "finish_reason": record.get("finish_reason"),
                       "usage": record.get("usage"), "meta": record.get("meta"),
                       "draft": draft and asdict(draft), "parse_problems": list(problems),
                       "wrapped": unwrap(record["content"]) is not record["content"]})
        return record["content"], record["ms"] / 1000
    return write


def run_scenarios(latency, author=None, only=None):
    out, landed = {}, []
    for name, directory in SCENARIO_DIRS.items():
        rows, requests, drafted, per = [], 0, 0, {}
        paths = sorted(p for p in directory.glob("*.json") if not only or p.stem in only)
        for path in paths:
            scenario = json.loads(path.read_text(encoding="utf-8"))
            fn = stand_in if author is None else live(author, scenario["id"], landed)
            got, pre, _ = simulate(scenario_steps(scenario), scenario["id"], latency, fn)
            per[scenario["id"]] = {"rows": got, "requests": len(pre.requests)}
            rows += got
            requests += len(pre.requests)
            drafted += len({s.task_id for s in pre.requests})
        out[name] = {"summary": summarize(rows, len(paths), requests, drafted), "scenarios": per}
    if author is not None:
        out["drafts"] = landed
    return out


def run_clips(run_dir, latencies, author=None):
    from voice.prototype.report import VOICE_HOME
    from voice.prototype.segmenter import DECIDER_BUDGET, use_decider_cache
    use_decider_cache(VOICE_HOME / "evidence" / "prototype" / "decider-cache", DECIDER_BUDGET)
    records = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(run_dir.glob("*.json"))
               if p.name != "summary.json"]
    out, same, landed = {}, True, []
    for latency in (["measured"] if author is not None else latencies):
        rows, requests, drafted, per = [], 0, 0, {}
        for record in records:
            stem = Path(record["clip"]).stem
            fn = stand_in if author is None else live(author, stem, landed)
            got, pre, seg = simulate(clip_steps(record), stem, 0.0 if author else latency, fn, author is not None)
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
    if author is not None:
        out["drafts"] = landed
    return out


def clip_author(model):
    """A Command Code author for clips. Its cache holds transcripts, so it lives under $VOICE_HOME, and it gets
    what is left of the shared budget after the repo's scenario ledger."""
    from voice.delegation.author import BUDGET
    from voice.delegation.commandcode import CommandCodeTransport
    from voice.prototype.report import VOICE_HOME
    used = len(CachedAuthor(CommandCodeTransport(model), CACHE)._attempts())
    return CachedAuthor(CommandCodeTransport(model), VOICE_HOME / "evidence" / "prewrite" / "author-cache",
                        budget=BUDGET - used)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("what", choices=["scenarios", "clips"])
    parser.add_argument("run", type=Path, nargs="?")
    parser.add_argument("--latency", type=float, nargs="+", default=[0.5])
    parser.add_argument("--author", choices=["stand-in", "commandcode"], default="stand-in")
    parser.add_argument("--model", default=None, help="Command Code model id")
    parser.add_argument("--only", nargs="*", help="scenario ids to replay")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.what == "scenarios":
        author = None
        if args.author == "commandcode":
            from voice.delegation.commandcode import MODEL, CommandCodeTransport
            author = CachedAuthor(CommandCodeTransport(args.model or MODEL), CACHE)
        result = run_scenarios(args.latency[0], author, args.only)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
        summaries = {k: v["summary"] for k, v in result.items() if k != "drafts"}
    else:
        author = None
        if args.author == "commandcode":
            from voice.delegation.commandcode import MODEL
            author = clip_author(args.model or MODEL)
        result = run_clips(args.run, args.latency, author)
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / "prewrite-replay.json").write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
        summaries = {k: v["summary"] for k, v in result.items() if k != "drafts"}
    print(json.dumps(summaries, indent=1))


if __name__ == "__main__":
    main()
