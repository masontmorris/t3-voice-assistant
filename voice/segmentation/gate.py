"""The task gate: whether a task the orchestrator is about to dispatch may run now, from the decider's own answers.

The gate asks the decider nothing new. Every decision point already answers, for each segment, what kind it is,
whether it is ready, whether it has a side effect, what it depends on and corrects, and, for a piece whose end is in
doubt, whether it is finished and where it ends. Each answer carries a confidence: the margin between the top two
choices, or the distance of a yes-no answer from 0.5, doubled. The gate's score is the lowest of them. A task runs
when the score reaches the threshold, and a side effect needs the stricter one. Anything else waits, and at the end
of the utterance it goes to the voice model.
"""

from dataclasses import dataclass

YES_NO = ("side_effect", "finished")


@dataclass(frozen=True)
class Verdict:
    passed: bool
    score: float
    weakest: str
    threshold: float

    def reason(self):
        verb = "reached" if self.passed else "fell short of"
        return f"gate: {self.weakest} confidence {self.score:.2f} {verb} {self.threshold:.2f}"


@dataclass(frozen=True)
class Gate:
    threshold: float
    side_effect_threshold: float

    def __call__(self, decision):
        scores = {name: abs(2 * value - 1) if name in YES_NO else value
                  for name, value in decision.confidence.items() if isinstance(value, (int, float))}
        weakest = min(scores, key=scores.get) if scores else "no answer"
        score = scores.get(weakest, 0.0)
        threshold = self.side_effect_threshold if decision.side_effect else self.threshold
        return Verdict(score >= threshold, score, weakest, threshold)


# Frozen on the tuning data (the original 11 scenarios and the 4 synthetic clips) before any run on Mason's clips.
# python -m voice.segmentation.gate_tune: of 10 tasks dispatched during speech, the one wrong one scored 0.35 and the
# lowest right one 0.41. No side effect dispatched during speech there, so the side-effect bar is a judgment.
PLAN_A = Gate(threshold=0.40, side_effect_threshold=0.80)
