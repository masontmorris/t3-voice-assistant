"""Pick the task gate's threshold on the tuning data: the original 11 scenarios and the 4 synthetic clips.

    python -m voice.research.segmentation.gate_tune [--clips RUN_DIR] [--out FILE]

It replays the hybrid's decision points on partial transcripts from cached answers, with no live call, and stops
before the final transcript, because the gated turn makes no call there. Every task the orchestrator dispatches
during speech is a gate candidate. Each candidate gets a label from its scenario's expectations, and a score under
each candidate gate form. RUN_DIR holds a prototype run over the synthetic clips, whose recorded transcript updates
are replayed as the run saw them. The printed table holds scores and labels only, never transcript text.

A dispatch is wrong when its instruction matches no expected task, the expected task ends corrected, superseded or
withdrawn, it must never dispatch, its instruction lacks a word the expectation requires, or its task dispatched
before.
"""

import argparse
import json
from pathlib import Path

from voice.segmentation.contract import TranscriptUpdate
from voice.segmentation.gate import YES_NO
from voice.segmentation.hybrid import HybridSegmenter
from voice.segmentation.run import SETS, OfflineClient
from voice.segmentation.score import CORRECTED, _matches

FORMS = {
    "all answers": None,
    "ready and side effect": ("ready", "side_effect"),
}
SYNTHETIC = ("single", "two_request", "correction", "long")


def scores(decision):
    out = {}
    for name, value in decision.confidence.items():
        if isinstance(value, (int, float)):
            out[name] = abs(2 * value - 1) if name in YES_NO else value
    return {form: min((v for k, v in out.items() if keep is None or k in keep), default=0.0)
            for form, keep in FORMS.items()}


def label(entry, expected, sent_before):
    exp = next((e for e in expected["tasks"] if _matches(e, {"instruction": entry["instruction"]})), None)
    if exp is None:
        return "wrong", "matches no expected task"
    if set(exp["status"]) <= CORRECTED:
        return "wrong", f"{exp['key']} ends corrected"
    if exp.get("never_dispatched"):
        return "wrong", f"{exp['key']} must never dispatch"
    missing = [w for w in exp.get("instruction_contains", []) if w not in entry["instruction"].lower()]
    if missing:
        return "wrong", f"{exp['key']} instruction lacks {missing}"
    if entry["task_id"] in sent_before:
        return "wrong", f"{exp['key']} dispatched twice"
    return "right", exp["key"]


def candidates(name, events, expected):
    """`events` are transcript updates and voice delegate instructions, in order."""
    seg = HybridSegmenter(OfflineClient(), name)
    rows, seen = [], 0
    for event in events:
        if isinstance(event, str):
            seg.submit_delegate(event)
        elif event.is_final:
            break
        else:
            seg.update(event)
        for entry in seg.dispatch_log[seen:]:
            decision = seg.orch.decided[entry["task_id"]][0]
            verdict, why = label(entry, expected, {e["task_id"] for e in seg.dispatch_log[:seen]})
            rows.append({"source": name, "revision": entry["revision"], "side_effect": decision.side_effect,
                         "label": verdict, "why": why, "scores": scores(decision)})
        seen = len(seg.dispatch_log)
    return rows


def scenario_rows():
    rows = []
    for path in sorted(SETS["original"].glob("*.json")):
        scenario = json.loads(path.read_text(encoding="utf-8"))
        events = [e["instruction"] if e["type"] == "delegate" else TranscriptUpdate(e["revision"], e["text"],
                                                                                    e["is_final"])
                  for e in scenario["events"]]
        rows += candidates(scenario["id"], events, scenario["expected"])
    return rows


def clip_rows(run_dir):
    from voice.research.prototype.replay import updates
    from voice.research.prototype.report import ROOT, VOICE_HOME
    from voice.prototype.segmenter import DECIDER_BUDGET, use_decider_cache
    use_decider_cache(VOICE_HOME / "evidence" / "prototype" / "decider-cache", DECIDER_BUDGET)
    expected = json.loads((ROOT / "expected.json").read_text(encoding="utf-8"))
    rows = []
    for stem in SYNTHETIC:
        record = json.loads((run_dir / f"{stem}.json").read_text(encoding="utf-8"))
        rows += candidates(stem, updates(record, as_run=True), expected[stem])
    return rows


def sweep(rows, form):
    """Per threshold: right candidates that run, wrong ones that run."""
    out = []
    for threshold in sorted({round(r["scores"][form], 2) for r in rows} | {0.0}):
        ran = [r for r in rows if r["scores"][form] >= threshold]
        out.append((threshold, sum(r["label"] == "right" for r in ran), sum(r["label"] == "wrong" for r in ran)))
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--clips", type=Path, help="a prototype run over the synthetic clips")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    rows = scenario_rows() + (clip_rows(args.clips) if args.clips else [])
    print("| Source | Revision | Side effect | Label | Why | " + " | ".join(FORMS) + " |")
    print("| --- | --- | --- | --- | --- | " + " | ".join("---" for _ in FORMS) + " |")
    for r in rows:
        why = r["why"] if r["label"] == "wrong" else "-"
        print(f"| {r['source']} | {r['revision']} | {'yes' if r['side_effect'] else 'no'} | {r['label']} | {why} | "
              + " | ".join(f"{r['scores'][f]:.2f}" for f in FORMS) + " |")
    for form in FORMS:
        print(f"\n{form}: threshold, right that run, wrong that run")
        for threshold, right, wrong in sweep(rows, form):
            print(f"  {threshold:.2f}  {right}  {wrong}")
    if args.out:
        args.out.write_text(json.dumps(rows, indent=1) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
