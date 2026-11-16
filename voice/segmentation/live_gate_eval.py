"""The live gate eval: every item through the end-of-turn path live mode runs, repeated, cached, scored.

One item is an utterance plus the call before it, as `eval/live_gate.jsonl` holds it. Each item runs the way
`run_clip` ends a live turn (`close_with_decider` in voice/prototype/run.py): the intent label decides whether the
decider is asked at all, then the hybrid segmenter makes its decision point on the final transcript, applies the
answers, and the orchestrator's gate settles each task. The turn ends with `close(settle=False)`.

Every decision call for one item-repeat is stored on disk under `EVIDENCE/calls/<item>.<repeat>.<n>.json` and reused
on the next run, so a scoring pass costs nothing and a rule can be replayed against recorded answers. `--fresh` sends
the same bodies again, which is how the repeats measure the model's spread on one body. Every response must report a
zero cost; a non-zero one stops the run.

The scoring pass is pure: it reads the traces and applies a gate rule to the recorded confidences. The rule that ships
in `voice/segmentation/gate.py` is scored by the same code, so the number the run prints is the number live mode gets.
"""

import argparse
import json
import os
import statistics
from pathlib import Path

from voice.prototype.segmenter import use_decider_cache
from voice.segmentation import client as api
from voice.segmentation.contract import Intent, TranscriptUpdate
from voice.segmentation.gate import PLAN_A, SCORED, scores
from voice.segmentation.hybrid import HybridSegmenter
from voice.segmentation.intent import utterance_intent

ROOT = Path(__file__).resolve().parent
EVAL = ROOT / "eval" / "live_gate.jsonl"
VOICE_HOME = Path(os.environ.get("VOICE_HOME", Path.home() / "voice"))
EVIDENCE = VOICE_HOME / "evidence" / "live-gate-eval"
REPEATS = 3
CALL_BUDGET = 400
ASKS_FOR_WORK = {"dispatch", "follow_up"}


class Recording:
    """One item-repeat's decider calls, cached by call order so a rerun repeats the same answers.

    With `fresh`, every body is sent again and saved over the previous answer, for a spread run.
    """

    def __init__(self, client, key, fresh=False):
        self.client, self.key, self.fresh, self.n = client, key, fresh, 0

    def call(self, route, body, label=""):
        assert route == "decisions", route
        assert body["model"].endswith(":free"), body["model"]
        self.n += 1
        path = EVIDENCE / "calls" / f"{self.key}.{self.n}.json"
        if path.exists() and not self.fresh:
            return json.loads(path.read_text(encoding="utf-8"))
        record = self.client.call(route, body, label=label, fresh=True)
        if record["status"] != 200:
            raise RuntimeError(f"{self.key} call {self.n}: status {record['status']}, nothing scored")
        response = record.get("response") or {}
        cost = (response.get("usage") or {}).get("cost")
        assert cost in (None, 0, 0.0), f"{self.key} call {self.n}: cost {cost}, stopping"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record, indent=2), encoding="utf-8")
        return record


def trace_of(item, repeat, segmenter, intent):
    """What one repeat did, in the terms a rule can replay: per task, the answers the gate scored.

    `last_reason` is the orchestrator's own record of the branch that settled the task, so a replayed rule can prove
    it is looking at the same tasks the shipped gate looked at.
    """
    decided = {}
    for point in segmenter.points:
        for segment in point["segments"]:
            decision = (point.get("decisions") or {}).get(segment["sid"])
            if decision is not None:
                decided[segment["text"]] = decision
    tasks = []
    for record in segmenter.records():
        decision = decided.get(record["segment_text"]) or {}
        reasons = [h.get("reason", "") for h in record["history"] if h["event"] == "status"]
        tasks.append({"instruction": record["instruction"], "status": record["status"],
                      "side_effect": record["side_effect"],
                      "gate_reached": bool(reasons) and reasons[-1].startswith("gate:"),
                      "last_reason": reasons[-1] if reasons else "",
                      "confidence": decision.get("confidence") or {},
                      "dispatched": any(d["task_id"] == record["task_id"] for d in segmenter.dispatch_log)})
    return {"id": item["id"], "split": item["split"], "category": item["category"], "expect": item["expect"],
            "repeat": repeat, "intent": intent.intent.value, "asked_decider": intent.intent == Intent.REQUEST,
            "tasks": tasks, "dispatched": bool(segmenter.dispatch_log)}


def run_item(client, item, repeat, fresh):
    """One live end of turn. Returns its trace."""
    segmenter = HybridSegmenter(Recording(client, f"{item['id']}.r{repeat}", fresh), f"{item['id']}.r{repeat}")
    segmenter.conversation_mode = True
    segmenter.prior_resolved_tasks = tuple(item["context"].get("resolved") or ())
    segmenter.orch.gate, segmenter.orch.side_effects_wait_for_final = PLAN_A, True
    update = TranscriptUpdate(1, item["utterance"], True)
    intent = utterance_intent(update.text)
    if intent.intent == Intent.REQUEST:
        segmenter.update(update)
    segmenter.close(update, settle=False)
    return trace_of(item, repeat, segmenter, intent)


def weakest(task):
    """The gate's score for one task: its weakest scored answer, on the gate's own scale."""
    scaled = scores({name: value for name, value in task["confidence"].items() if name in SCORED})
    return min(scaled.values()) if scaled else None


def current_rule(task):
    """The gate as shipped: the weakest answer against the bar for that task."""
    score = weakest(task)
    return score is not None and score >= PLAN_A.bar(task["side_effect"])


def threshold_rule(threshold, side_effect_threshold):
    def rule(task):
        score = weakest(task)
        bar = side_effect_threshold if task["side_effect"] else threshold
        return score is not None and score >= bar
    return rule


def must_agree(threshold, side_effect_threshold):
    """A task that reached the gate passes only when its weakest answer clears the bar."""
    def rule(trace, task):
        return threshold_rule(threshold, side_effect_threshold)(task)
    return rule


def replayed(trace, rule):
    """Whether this trace's turn dispatched a task under `rule`, keeping the intent label it was run with."""
    if trace["intent"] != Intent.REQUEST.value:
        return False
    return any(task["gate_reached"] and rule(trace, task) for task in trace["tasks"])


def verify(traces):
    """A replay must reproduce what the shipped gate did, or the traces are not a faithful record."""
    wrong = [t["id"] for t in traces
             if replayed(t, lambda trace, task: current_rule(task)) != t["dispatched"]]
    assert not wrong, f"replay disagrees with the recorded dispatch on {wrong}"


def mean_of_readings(traces, readings=2):
    """Each item's answers averaged over `readings` samples of the same body, then scored as the gate scores.

    A re-query sends one body twice, so the samples differ only by the model's own spread. Averaging each answer's
    confidence before taking the weakest is one way to combine them; `min_of_readings` and `max_of_readings` are the
    conservative and permissive ones, and all three are here so the run can say which separates.
    """
    by_item = {}
    for trace in traces:
        by_item.setdefault(trace["id"], []).append(trace)
    out = {"mean": {}, "min": {}, "max": {}}
    for item, traces_of_item in by_item.items():
        traces_of_item.sort(key=lambda t: t["repeat"])
        picked = traces_of_item[:readings]
        merged = []
        for tasks in zip(*(t["tasks"] for t in picked)):
            if not any(task["gate_reached"] for task in tasks):
                continue
            keys = {key for task in tasks for key in task["confidence"]}
            answers = {key: [task["confidence"][key] for task in tasks
                             if isinstance(task["confidence"].get(key), (int, float))] for key in keys}
            for how, pick in (("mean", statistics.mean), ("min", min), ("max", max)):
                out[how].setdefault(item, []).append(
                    {"side_effect": tasks[0]["side_effect"],
                     "confidence": {key: pick(values) for key, values in answers.items() if values}})
    return out


def combined_rule(merged, threshold, side_effect_threshold, how):
    def rule(trace, task):
        combined = merged[how].get(trace["id"])
        return bool(combined) and any(threshold_rule(threshold, side_effect_threshold)(task) for task in combined)
    return rule


def sweep(traces, firsts):
    """Train accuracy at each threshold, then held-out at the one train picks. Train chooses; held-out only reports."""
    print(f"\nthreshold sweep, one reading per item (side-effect bar stays {PLAN_A.side_effect_threshold:.2f})")
    print(f"{'bar':>5}{'train ok':>10}{'train fd':>10}{'train fh':>10}{'held out ok':>13}{'held fd':>9}{'held fh':>8}")
    best = None
    for step in range(25, 96, 5):
        threshold = step / 100
        rule = lambda trace, task: threshold_rule(threshold, PLAN_A.side_effect_threshold)(task)  # noqa: E731
        rows = summarise(firsts, rule)
        train, held = aggregate(rows, "train"), aggregate(rows, "heldout")
        print(f"{threshold:>5.2f}{train['accuracy']:>10.0%}{train['false_dispatch']:>10}{train['false_hold']:>10}"
              f"{held['accuracy']:>13.0%}{held['false_dispatch']:>9}{held['false_hold']:>8}")
        if best is None or train["accuracy"] > best[1]:
            best = (threshold, train["accuracy"])
    print(f"train picks {best[0]:.2f} at {best[1]:.0%}")


def summarise(traces, rule):
    """Per split and category, how many turns matched their expected outcome, and both error rates."""
    rows = {}
    for trace in traces:
        expected = trace["expect"] in ASKS_FOR_WORK
        dispatched = replayed(trace, rule)
        row = rows.setdefault((trace["split"], trace["category"]),
                              {"n": 0, "ok": 0, "false_dispatch": 0, "false_hold": 0, "tasked_anyway": 0})
        row["n"] += 1
        row["ok"] += dispatched == expected
        if not expected and dispatched:
            row["false_dispatch"] += 1
        if not expected and trace["tasks"]:
            row["tasked_anyway"] += 1
        if expected and not dispatched:
            row["false_hold"] += 1
    return rows


def aggregate(rows, split):
    picked = [row for (name, _), row in rows.items() if name == split]
    total = sum(row["n"] for row in picked)
    return {"n": total, "accuracy": sum(row["ok"] for row in picked) / total if total else 0.0,
            "false_dispatch": sum(row["false_dispatch"] for row in picked),
            "false_hold": sum(row["false_hold"] for row in picked),
            "tasked_anyway": sum(row["tasked_anyway"] for row in picked)}


def spread(traces, threshold):
    """Per item: the gate scores across repeats, their range, and whether they straddle the threshold."""
    by_item = {}
    for trace in traces:
        by_item.setdefault(trace["id"], []).append(trace)
    samples = {}
    for item, traces_of_item in by_item.items():
        values = [weakest(task) for trace in traces_of_item for task in trace["tasks"] if task["gate_reached"]]
        values = [value for value in values if value is not None]
        if values:
            samples[item] = values
    ranges = [max(values) - min(values) for values in samples.values() if len(values) > 1]
    straddles = sum(min(values) < threshold <= max(values) for values in samples.values() if len(values) > 1)
    return {"items": len(samples), "ranges": ranges, "straddles": straddles,
            "median": statistics.median(ranges) if ranges else 0.0,
            "mean": statistics.mean(ranges) if ranges else 0.0, "max": max(ranges) if ranges else 0.0}


def print_table(rows, label):
    print(f"\n{label}")
    print(f"{'split':8}{'category':22}{'n':>3}{'ok':>4}{'false-dispatch':>16}{'false-hold':>12}{'tasked':>7}")
    for (split, category), row in sorted(rows.items()):
        print(f"{split:8}{category:22}{row['n']:>3}{row['ok']:>4}{row['false_dispatch']:>16}"
              f"{row['false_hold']:>12}{row['tasked_anyway']:>7}")
    for split in ("train", "heldout"):
        total = aggregate(rows, split)
        print(f"{split:8}{'ALL':22}{total['n']:>3}{total['accuracy']:>6.0%}{total['false_dispatch']:>16}"
              f"{total['false_hold']:>12}{total['tasked_anyway']:>7}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--fresh", action="store_true", help="send every body again, for a spread run")
    parser.add_argument("--repeats", type=int, default=REPEATS)
    parser.add_argument("--budget", type=int, default=CALL_BUDGET)
    parser.add_argument("--only", default=None, help="one item id, for a smoke test")
    parser.add_argument("--traces", type=Path, default=EVIDENCE / "traces.jsonl")
    parser.add_argument("--score-only", action="store_true", help="score the saved traces, no call")
    parser.add_argument("--sweep", action="store_true", help="print the threshold sweep over the saved traces")
    args = parser.parse_args()
    items = [json.loads(line) for line in EVAL.read_text(encoding="utf-8").splitlines() if line.strip()]
    if args.only:
        items = [item for item in items if item["id"] == args.only]
    use_decider_cache(EVIDENCE / "ledger", args.budget)
    if not args.score_only:
        traces = []
        client = api.Client()
        for item in items:
            for repeat in range(1, args.repeats + 1):
                trace = run_item(client, item, repeat, args.fresh)
                traces.append(trace)
                print(f"{item['id']} r{repeat} intent={trace['intent']:9} dispatched={trace['dispatched']!s:5} "
                      f"scores={[round(weakest(t), 2) for t in trace['tasks'] if t['gate_reached']]} "
                      f"attempts={api.ledger_attempts()}", flush=True)
        args.traces.parent.mkdir(parents=True, exist_ok=True)
        args.traces.write_text("\n".join(json.dumps(t) for t in traces) + "\n", encoding="utf-8")
    traces = [json.loads(line) for line in args.traces.read_text(encoding="utf-8").splitlines() if line.strip()]
    verify(traces)
    firsts = [t for t in traces if t["repeat"] == 1]
    print_table(summarise(firsts, lambda trace, task: current_rule(task)), "SHIPPED GATE, one reading per item")
    effects = spread(traces, PLAN_A.threshold)
    print(f"\nscore spread on {effects['items']} items over {args.repeats} readings: median {effects['median']:.3f}, "
          f"mean {effects['mean']:.3f}, max {effects['max']:.3f}; {effects['straddles']} straddle "
          f"{PLAN_A.threshold:.2f}")
    readings = mean_of_readings(traces)
    for how in ("mean", "min", "max"):
        print_table(summarise(firsts, combined_rule(readings, PLAN_A.threshold, PLAN_A.side_effect_threshold, how)),
                    f"TWO READINGS, COMBINED BY {how.upper()}, one decision per item")
    if args.sweep:
        sweep(traces, firsts)


if __name__ == "__main__":
    main()
