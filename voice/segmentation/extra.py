"""Paired test: does adding variant E's optional questions change latency or the other answers?

Takes every fourth decisions request that `stream-e` sent, in label order, up to 15, and sends it again with the
implicit and preemptive questions added. Writes results/extra-questions.json. Re-running replays from the cache.

    python -m voice.segmentation.extra            # live calls for uncached requests
    python -m voice.segmentation.extra --offline
"""

import argparse
import json

from voice.segmentation import client as api
from voice.segmentation.cursor import EXTRA_QUESTIONS
from voice.segmentation.run import RESULTS, OfflineClient

SAMPLE = 15
EVERY = 4


def sampled_files():
    labels = {}
    for path in sorted((RESULTS / "stream-e").glob("[0-9]*.json")):
        for point in json.loads(path.read_text(encoding="utf-8"))["decision_points"]:
            for call in point["calls"]:
                if "delegate" not in call["label"]:
                    labels.setdefault(call["label"], call["file"])
    return [labels[k] for k in sorted(labels)][::EVERY][:SAMPLE]


def compare(base, extra):
    a, b = base["response"]["answers"], extra["response"]["answers"]
    out = {}
    for name in base["request"]["questions"]:
        x, y = a.get(name, {}), b.get(name, {})
        if x.get("type") == "noul":
            out[name] = {"base": x.get("noul"), "extra": y.get("noul")}
        else:
            out[name] = {"base": x.get("choice"), "extra": y.get("choice"),
                         "base_margin": x.get("confidence"), "extra_margin": y.get("confidence")}
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()
    client = OfflineClient() if args.offline else api.Client()
    pairs = []
    for name in sampled_files():
        base = json.loads((api.CACHE / name).read_text(encoding="utf-8"))
        body = {**base["request"], "questions": {**base["request"]["questions"], **EXTRA_QUESTIONS}}
        extra = client.call("decisions", body, label=f"{base['label']}:extra")
        pairs.append({"label": base["label"], "base_file": name, "extra_file": api.cache_path("decisions", body).name,
                      "base_ms": round(base["timing"]["ms"], 1), "extra_ms": round(extra["timing"]["ms"], 1),
                      "base_questions": len(base["request"]["questions"]), "extra_questions": len(body["questions"]),
                      "answers": compare(base, extra) if extra["status"] == 200 else None,
                      "added": {k: extra["response"]["answers"].get(k, {}).get("noul")
                                for k in EXTRA_QUESTIONS} if extra["status"] == 200 else None})
    (RESULTS / "extra-questions.json").write_text(json.dumps(pairs, indent=2) + "\n", encoding="utf-8")
    print(f"{len(pairs)} pairs; ledger attempts {api.ledger_attempts()}")


if __name__ == "__main__":
    main()
