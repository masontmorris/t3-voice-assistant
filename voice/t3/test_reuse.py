"""Whether a summary written for other words answers the final question, on recorded decider answers: neither
decider reuses a summary that answers another question, and the reuses score as docs/voice/local-renderer.md
reports."""

from __future__ import annotations

import json
import unittest

from voice.local_decider.client import D1Client
from voice.t3.eval.record_reuse import CASES, FIXTURE, TITLE, body, key
from voice.t3.verify import Decider

RECORDED = json.loads(FIXTURE.read_text(encoding="utf-8"))


class Hosted:
    """The hosted decider, answering each request from the fixture, and only a request it recorded."""

    model = "hosted"

    def call(self, route, request, label=""):
        answer = RECORDED["answers"].get(key(self.model, request))
        if answer is None:
            raise KeyError("no recorded answer for this request: re-run voice.t3.eval.record_reuse")
        return {**answer, "timing": {"ms": 0.0, "started_at": ""}}


class Local(D1Client, Hosted):
    model = "local"

    def __init__(self):
        super().__init__("http://127.0.0.1:9")

    call = Hosted.call


def score(model):
    decider = Decider(Local() if model == "local" else Hosted())
    out = {False: [0, 0, 0], True: [0, 0, 0]}
    for asked, gist, question, answers, held in CASES:
        reused = decider.reuses(TITLE, gist, asked, question)
        out[held][0 if answers and reused else 1 if reused else 2] += answers or reused
    return {"seen": tuple(out[False]), "held": tuple(out[True])}


class ReuseTest(unittest.TestCase):
    def test_every_case_was_recorded_for_both_deciders(self):
        for model in ("local", "hosted"):
            self.assertEqual(len([r for r in RECORDED["rows"] if r["model"] == model]), len(CASES))
            for case in CASES:
                self.assertIn(key(model, body(case)), RECORDED["answers"])

    def test_neither_decider_reuses_a_summary_that_answers_another_question(self):
        # (right reuses, wrong reuses, missed reuses): 13 seen and 12 held-out cases answer, 8 and 9 do not.
        self.assertEqual(score("local"), {"seen": (10, 0, 3), "held": (10, 0, 2)})
        self.assertEqual(score("hosted"), {"seen": (12, 0, 1), "held": (12, 0, 0)})


if __name__ == "__main__":
    unittest.main()
