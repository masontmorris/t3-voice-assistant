"""Does the decider call a long hesitant "start a thread" request ready to dispatch? Replays real final decisions.

    VOICE_ENV_FILE=<env file> python -m voice.t3.eval.dispatch_probe REHEARSAL_DIR [--draws 3]

Reads the final decisions call of each call's request turn from a rehearsal's records (the request bodies are in the
decider cache that run wrote under $VOICE_HOME/evidence/liquid/cache), asks them again to Liquid's d1.2 with each
variant of the readiness and tool questions, and prints how often a t3_start segment comes back dispatch_now. The
score of a variant is the share of draws in which at least one t3_start segment of the turn is dispatch_now, which is
what lets the turn's task run. It costs one d1.2 call per request per draw per variant, under the daily cap.
"""

import argparse
import json
from pathlib import Path

from voice.liquid_decider.client import CACHE, LiquidClient
from voice.segmentation.hybrid import T3_CORRECTS, T3_DEPENDS, T3_READY

OLD_START = "in a project they name. "
READY_T3 = T3_READY


DEPENDS_T3, CORRECTS_T3 = T3_DEPENDS, T3_CORRECTS


def variants(body, name):
    """`body` with the readiness, tool, dependency and correction questions changed as `name` says."""
    out = json.loads(json.dumps(body))
    for key, question in out["questions"].items():
        if name in ("depends", "both2") and key.endswith("_depends"):
            question["criteria"]["none"] += DEPENDS_T3
        if name in ("corrects", "both2") and key.endswith("_corrects"):
            question["criteria"]["none"] += CORRECTS_T3
        if name in ("ready", "both") and key.endswith("_ready"):
            question["criteria"]["dispatch_now"] += READY_T3
        if name in ("tool", "both") and key.endswith("_tool"):
            criteria = question["criteria"]
            if "t3_start" in criteria:
                criteria["t3_start"] = criteria["t3_start"].replace(OLD_START, "")
    return out


def request_bodies(directory):
    """(call, body) for the final decisions call of each rehearsal turn whose segments hold a t3_start tool."""
    found = []
    for path in sorted((directory / "server").glob("turn_*.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        if (record.get("live") or {}).get("call", {}).get("turn") != 3:
            continue
        finals = [e["decision"] for e in record.get("timeline", []) if e["kind"] == "final_decision"]
        if not finals:
            continue
        cached = Path(CACHE) / finals[0]["calls"][0]["file"]
        if cached.exists():
            found.append((path.name, json.loads(cached.read_text(encoding="utf-8"))["request"]))
    return found


def dispatched(response):
    """A t3_start segment that is dispatch_now, waits on no earlier segment, and no later one replaces or cancels it:
    what lets its task run."""
    answers = (response or {}).get("answers") or {}
    gone = {str(a.get("choice")).split("_", 1)[1] for key, a in answers.items()
            if key.endswith("_corrects") and str(a.get("choice")).startswith(("replace_", "cancel_"))}
    for key in answers:
        if not key.endswith("_ready"):
            continue
        sid = key[:-len("_ready")]
        if (answers.get(sid + "_tool", {}).get("choice") == "t3_start" and answers[key].get("choice") == "dispatch_now"
                and answers.get(sid + "_depends", {}).get("choice", "none") == "none" and sid not in gone):
            return True
    return False


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--draws", type=int, default=3)
    parser.add_argument("--variants", nargs="+", default=["as_is", "depends", "corrects", "both2"])
    args = parser.parse_args()
    client = LiquidClient(max_attempts=3)
    bodies = request_bodies(args.directory)
    print(f"{len(bodies)} request turns")
    for name in args.variants:
        hits = {}
        for turn, body in bodies:
            asked = variants(body, name)
            for _ in range(args.draws):
                record = client.call("decisions", asked, label="dispatch_probe", fresh=True, cache_record=False)
                hits.setdefault(turn, []).append(record["status"] == 200 and dispatched(record.get("response")))
        total = sum(sum(h) for h in hits.values())
        print(f"{name}: {total} of {sum(map(len, hits.values()))} draws dispatch a t3_start segment;",
              {turn: f"{sum(h)}/{len(h)}" for turn, h in hits.items()}, flush=True)


if __name__ == "__main__":
    main()
