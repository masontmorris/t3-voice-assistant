"""Record fictional local-kind holdout answers. Hosted recording is an explicit, billed PM action.

Run local with a running leased server and VOICE_D1_URL set:
    python -m voice.t3.eval.record_local_kind
To record hosted answers separately:
    python -m voice.t3.eval.record_local_kind liquid
"""
import json
import sys
from collections import Counter
from pathlib import Path

from voice.local_decider.client import decider
from voice.t3.eval.record_followups import decide, passes

HERE = Path(__file__).resolve().parent
CASES = HERE / "local_kind_holdout.jsonl"


class AnswersOnly:
    def __init__(self, client):
        self.client = client
        self.calls = []

    def __getattr__(self, name):
        return getattr(self.client, name)

    def call(self, route, body, label=""):
        result = self.client.call(route, body, label=label)
        if result["status"] != 200:
            raise RuntimeError(f"decider status {result['status']}")
        self.calls.append(result["response"]["answers"])
        return result


def main():
    name = sys.argv[1] if len(sys.argv) > 1 else "d1"
    if name not in ("d1", "liquid"):
        raise ValueError("choose d1 or liquid")
    cases = [json.loads(line) for line in CASES.read_text(encoding="utf-8").splitlines() if line]
    rows = []
    with decider(name) as client:
        for case in cases:
            recorder = AnswersOnly(client)
            routed = decide(recorder, case["utterance"], "Lantern task", None, "after")
            rows.append({"id": case["id"], "want": case["tool"], "routed": routed,
                         "pass": passes(case["tool"], routed), "answers": recorder.calls})
    out = HERE / ("local_kind_local_answers.json" if name == "d1" else "local_kind_hosted_answers.json")
    out.write_text(json.dumps(rows, indent=1) + "\n", encoding="utf-8")
    totals = Counter((row["want"], row["pass"]) for row in rows)
    for label in dict.fromkeys(row["want"] for row in rows):
        print(label, totals[label, True], '/', totals[label, True] + totals[label, False])
    print("failed:", ', '.join(row['id'] for row in rows if not row['pass']))


if __name__ == "__main__":
    main()
