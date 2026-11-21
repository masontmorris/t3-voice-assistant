"""Record the hosted decider's answers to the reply reader's questions on the made-up cases of
voice/t3/eval/reply_marks.py, for voice/t3/test_reply_marks.py to replay.

    VOICE_ENV_FILE=<env file> python -m voice.t3.eval.record_reply_marks [--draws N] [--splits seen fresh_seen ...]

Each case is sent as the live reader sends it (`reading_request`). A recording keeps the answers of the first draw;
more draws only print the counts, since a decider's answer near a bar varies from call to call. Splits that are not
asked for keep the rows already recorded, so a held-out split is recorded once, when its wording is final.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

from voice.liquid_decider.client import LiquidClient
from voice.t3.eval.reply_marks import FRESH_HELD_OUT, FRESH_SEEN, HELD_OUT, SEEN
from voice.t3.verify import judge_reading, reading_request

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "reply_marks.json"
SPLITS = {"seen": SEEN, "held_out": HELD_OUT, "fresh_seen": FRESH_SEEN, "fresh_held_out": FRESH_HELD_OUT}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--draws", type=int, default=1)
    parser.add_argument("--splits", nargs="+", choices=list(SPLITS), default=list(SPLITS))
    args = parser.parse_args()
    client = LiquidClient(max_attempts=3)
    kept = json.loads(FIXTURE.read_text(encoding="utf-8")) if FIXTURE.exists() else []
    rows, counts = [r for r in kept if r["split"] not in args.splits], defaultdict(lambda: [0, 0])
    for split in args.splits:
        for said, reply, group, must_act, takes_back in SPLITS[split]:
            for draw in range(args.draws):
                record = client.call("decisions", reading_request(said, reply), label="t3_reply_marks", fresh=True,
                                     cache_record=False)
                answers = (record.get("response") or {}).get("answers")
                if record["status"] != 200 or not isinstance(answers, dict):
                    raise RuntimeError(f"incomplete answer for {reply!r}: status {record['status']}")
                reading = judge_reading(answers)
                counts[split, group][0] += reading.yes if takes_back is None else reading.withdrawing
                counts[split, group][1] += 1
                if draw == 0:
                    rows.append({"split": split, "act": said.to_json(), "reply": reply, "group": group,
                                 "must_act": must_act, "takes_back": takes_back, "answers": answers})
    for (split, group), (hit, n) in sorted(counts.items()):
        print(f"{split:15} {group:20} {hit}/{n}")
    order = {name: i for i, name in enumerate(SPLITS)}
    rows.sort(key=lambda r: order[r["split"]])
    FIXTURE.write_bytes((json.dumps(rows, separators=(",", ":")) + "\n").encode())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
