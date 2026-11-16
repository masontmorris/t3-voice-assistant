"""Run scenario streams through a span option, the decider, and the orchestrator; write task records and scores.

    python -m voice.segmentation.run --option a --mode stream            # live calls for uncached requests
    python -m voice.segmentation.run --option a --mode stream --offline  # recorded responses only

--mode final runs only the final transcript of each scenario (the span-option comparison). --separate asks one
segment's questions per call instead of all in one call. --repeat N adds a `user` tag so the same request is sent again.
"""

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from voice.segmentation import client as api
from voice.segmentation.contract import Decisions, TranscriptUpdate, decision_request, parse_decisions
from voice.segmentation.score import score
from voice.segmentation.spans import (boundary_request, clause_segments, extracted_segments, extraction_request, fine_pieces,
                          merge_pieces, _segments)
from voice.segmentation.tasks import Orchestrator

ROOT = Path(__file__).resolve().parent
SCENARIOS = ROOT / "scenarios"
RESULTS = ROOT / "results"


class OfflineClient:
    def call(self, route, body, label=""):
        assert body["model"].endswith(":free")
        record = api.cached(route, body)
        if record is None:
            raise LookupError(f"no recorded response for {label} ({api.cache_path(route, body).name})")
        return record


class Runner:
    def __init__(self, option, client, separate=False, repeat=None):
        self.option, self.client, self.separate, self.repeat = option, client, separate, repeat

    def _call(self, route, body, label, calls):
        if self.repeat is not None:
            body = {**body, "user": f"repeat-{self.repeat}"}
        record = self.client.call(route, body, label=label)
        calls.append({"label": label, "route": route, "file": api.cache_path(route, body).name,
                      "status": record["status"], "ms": round(record["timing"]["ms"], 1),
                      "started_at": record["timing"]["started_at"]})
        return body, record

    def segments(self, update, label, calls):
        text, final = update.text, update.is_final
        if self.option == "a":
            return clause_segments(text, final), []
        if self.option == "b":
            pieces = fine_pieces(text)
            if len(pieces) <= 1:
                return _segments(text, pieces, final), []
            _, record = self._call("decisions", boundary_request(text, pieces), f"{label}:boundaries", calls)
            return merge_pieces(text, pieces, record["response"] if record["status"] == 200 else None, final)
        _, record = self._call("extractions", extraction_request(text), f"{label}:extract", calls)
        if record["status"] != 200:
            return [], [f"extraction http {record['status']}"]
        return extracted_segments(text, record["response"], final)

    def decide(self, orch, update, label):
        calls = []
        segments, problems = self.segments(update, label, calls)
        delegates = dict(orch.pending_delegates)
        decisions = Decisions({}, {}, [])
        if segments or delegates:
            tasks = [{"task": t.task_id, "instruction": t.instruction, "status": t.status.value,
                      "sources": sorted(t.sources)} for t in orch.tasks.values()]
            body = decision_request(update, segments, tasks, delegates)
            bodies = [body]
            if self.separate:
                bodies = [decision_request(update, segments, tasks, {}, only=s.sid) for s in segments]
                if delegates:
                    bodies.append({**decision_request(update, [], tasks, delegates), "state": body["state"],
                                   "questions": {k: v for k, v in body["questions"].items()
                                                 if k.endswith("_same_as")}})
            merged = Decisions({}, {}, [])
            for i, b in enumerate(bodies):
                sent, record = self._call("decisions", b, f"{label}:decide{i if self.separate else ''}", calls)
                if record["status"] != 200:
                    part = Decisions({}, {}, [f"decisions http {record['status']}"])
                else:
                    part = parse_decisions(record["response"], sent)
                merged = Decisions({**merged.segments, **part.segments}, {**merged.delegates, **part.delegates},
                                   merged.problems + part.problems)
            decisions = merged
        orch.apply(update, segments, decisions)
        return {"revision": update.revision, "is_final": update.is_final,
                "segments": [asdict(s) for s in segments], "calls": calls,
                "decisions": {sid: _jsonable(asdict(d)) for sid, d in decisions.segments.items()},
                "delegates": decisions.delegates, "problems": problems + decisions.problems}

    def run(self, scenario, mode):
        orch = Orchestrator()
        events = scenario["events"]
        if mode == "final":
            events = [e for e in events if e["type"] == "delegate"] + [events[-1]]
        points, last, signature, revisions = [], None, None, 0
        for event in events:
            if event["type"] == "delegate":
                orch.submit_delegate(event["instruction"])
                if last is not None:
                    points.append(self.decide(orch, last, f"{scenario['id']}:r{last.revision}:delegate"))
                continue
            revisions += 1
            last = TranscriptUpdate(event["revision"], event["text"], event["is_final"])
            cheap = clause_segments(last.text, last.is_final)
            current = (len(cheap), tuple(s.text for s in cheap if not s.open))
            if last.is_final or current != signature or orch.pending_delegates:
                points.append(self.decide(orch, last, f"{scenario['id']}:r{last.revision}"))
            signature = current
        snapshot = orch.snapshot("conv_stage2", f"utt_{scenario['id']}")
        records = orch.task_records()
        return {"scenario": scenario["id"], "covers": scenario["covers"], "option": self.option, "mode": mode,
                "revisions": revisions, "decision_points": points, "tasks": records,
                "dispatch_log": orch.dispatch_log, "snapshot": snapshot, "expected": scenario["expected"],
                "score": score(scenario["expected"], records, orch.dispatch_log, snapshot)}


def _jsonable(value):
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if hasattr(value, "value"):
        return value.value
    return value


def run_name(option, mode, separate, repeat):
    return f"{mode}-{option}" + ("-separate" if separate else "") + (f"-repeat{repeat}" if repeat else "")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--option", choices="abc", required=True)
    parser.add_argument("--mode", choices=["stream", "final"], required=True)
    parser.add_argument("--separate", action="store_true")
    parser.add_argument("--repeat", type=int)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--only")
    args = parser.parse_args()
    runner = Runner(args.option, OfflineClient() if args.offline else api.Client(), args.separate, args.repeat)
    out = RESULTS / run_name(args.option, args.mode, args.separate, args.repeat)
    out.mkdir(parents=True, exist_ok=True)
    summary = []
    for path in sorted(SCENARIOS.glob("*.json")):
        scenario = json.loads(path.read_text(encoding="utf-8"))
        if args.only and args.only not in scenario["id"]:
            continue
        result = runner.run(scenario, args.mode)
        (out / f"{scenario['id']}.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        s = result["score"]
        calls = [c for p in result["decision_points"] for c in p["calls"]]
        summary.append({"scenario": scenario["id"], "pass": s["pass"], "revisions": result["revisions"],
                        "decision_points": len(result["decision_points"]), "calls": len(calls),
                        "premature": s["premature_dispatches"], "duplicate": s["duplicate_dispatches"],
                        "missed_corrections": s["missed_corrections"],
                        "problems": sum(len(p["problems"]) for p in result["decision_points"]),
                        "failures": s["failures"]})
        print(f"{scenario['id']}: {'PASS' if s['pass'] else 'FAIL'} {s['failures']}")
    if not args.only:
        (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(f"{sum(r['pass'] for r in summary)}/{len(summary)} pass; ledger attempts {api.ledger_attempts()}")


if __name__ == "__main__":
    main()
