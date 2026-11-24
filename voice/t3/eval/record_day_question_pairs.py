"""Record the hosted decider's reading of thread follow-ups under the base day question and the shipped one, so
voice/t3/test_followups.py can say whether the longer day question moves a decision it does not ask about.

    VOICE_ENV_FILE=<env file> python -m voice.t3.eval.record_day_question_pairs [--samples 8]

The day question grew options (`in_2_days` to `in_15_days` and `farther`) and a `continues` question was added beside
it. This asks the same thread follow-ups of both bodies: `base` is the question set without those, and `shipped` is
the code as it stands. Every sample is a fresh call, so a body is asked `--samples` times and the spread is the model's
own. The fixture keeps each answer under a hash of its request and the sample's index.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
from pathlib import Path

from voice.segmentation import contract
from voice.tools import lookups
from voice.t3.eval.record_followups import CASES, key, decide

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "day_question_pairs.json"
SAMPLES = 8
ARMS = ("base", "shipped")
# The follow-ups the shipped fixture's test looks at: the one the longer question was suspected of moving, and two
# of the same kind that it reads the same under either.
UTTERANCES = ("Please tell it to stop touching the config.", "Tell it to also add tests.", "Stop it.",
              "Ask it to log the load times too.")


@contextlib.contextmanager
def arm(name):
    """The question set the arm asks: `base` has the day question as it was before counted days and `continues`."""
    if name == "shipped":
        yield
        return
    original = contract.segment_questions
    counted = set(lookups.COUNTED_DAYS) | {lookups.FARTHER}

    def base(*args, **kwargs):
        questions = original(*args, **kwargs)
        for question_name in [n for n in questions if n.endswith("_continues")]:
            del questions[question_name]
        for question_name, question in questions.items():
            if question_name.endswith("_when"):
                question["criteria"] = {k: v for k, v in question["criteria"].items() if k not in counted}
        return questions

    contract.segment_questions = base
    try:
        yield
    finally:
        contract.segment_questions = original


def cases():
    return [case for case in CASES if case[0] in UTTERANCES]


class Recording:
    def __init__(self, client, answers, sample):
        self.client, self.answers, self.sample = client, answers, sample

    def call(self, route, body, label=""):
        record = self.client.call(route, body, label=label, fresh=True)
        if record["status"] == 200:
            self.answers.setdefault(key(body), {})[str(self.sample)] = record["response"]
        return record

    def __getattr__(self, name):
        return getattr(self.client, name)


class Replay:
    def __init__(self, answers, sample):
        self.answers, self.sample = answers, sample

    def call(self, route, body, label="", **_):
        answer = self.answers.get(key(body), {}).get(str(self.sample))
        if answer is None:
            raise KeyError("no recorded answer for this request: re-run voice.t3.eval.record_day_question_pairs")
        return {"status": 200, "response": answer, "timing": {"ms": 0.0, "started_at": ""}}


def main() -> int:
    from voice.local_decider.client import decider as make_decider
    from voice.prototype.segmenter import use_decider_cache

    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=int, default=SAMPLES)
    args = parser.parse_args()
    use_decider_cache(Path(os.environ.get("VOICE_HOME", Path.home() / "voice")) / "evals" / "day-question-pairs", 400)
    answers, rows = {}, []
    with make_decider("liquid") as client:
        for utterance, recent, earlier, want, held in cases():
            for name in ARMS:
                with arm(name):
                    for sample in range(args.samples):
                        routed = decide(Recording(client, answers, sample), utterance, recent, earlier, "after")
                        rows.append({"arm": name, "utterance": utterance, "want": want, "sample": sample,
                                     "routed": routed})
                        print(json.dumps(rows[-1]), flush=True)
    FIXTURE.write_bytes((json.dumps({"rows": rows, "answers": answers}, indent=1) + "\n").encode("utf-8"))
    for utterance, _, _, want, _ in cases():
        for name in ARMS:
            hit = sum(row["routed"] == [want] for row in rows if row["arm"] == name and row["utterance"] == utterance)
            print(f"{utterance!r} {name}: {want} in {hit} of {args.samples}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
