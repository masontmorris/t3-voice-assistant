"""The task gate: whether a task the orchestrator is about to dispatch may run now, from the decider's own answers.

The gate asks the decider nothing new. It scores the answers that say whether the task can start: whether it is ready
(`ready`) and whether it acts outside the assistant (`side_effect`). Each carries a confidence: the margin between the
top two choices for `ready`, or the distance of the yes-no `side_effect` answer from 0.5, doubled. The gate's score is
the weaker of the two. A task runs when the score reaches the threshold, and a side effect needs the stricter one.
Anything else waits, and at the end of the utterance it goes to the voice model.

`kind` is left out on purpose. It is a five-way choice, so a tie between two kinds reads as 0.375 while a tie between
two of `ready`'s three choices reads as 0.5; the numbers are not on one scale. Measured on the live gate eval's 70
items, the `kind` margin also ranked the two cases backwards: a plain lookup the model called `request` twice in three
readings scored 0.369 and was held, while a description of someone else's task scored 0.558 and ran. A non-request kind
still creates no task in the orchestrator, so the gate does not need the margin to refuse one.
"""

from dataclasses import dataclass

YES_NO = ("side_effect", "finished")
SCORED = ("ready", "side_effect")


def scores(confidence):
    """Each answer's confidence on the gate's own scale: a yes-no answer's distance from 0.5, doubled; else as given."""
    return {name: abs(2 * value - 1) if name in YES_NO else value
            for name, value in confidence.items() if isinstance(value, (int, float))}


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

    def bar(self, side_effect):
        return self.side_effect_threshold if side_effect else self.threshold

    def __call__(self, decision):
        scaled = scores({name: value for name, value in decision.confidence.items() if name in SCORED})
        weakest = min(scaled, key=scaled.get) if scaled else "no answer"
        score = scaled.get(weakest, 0.0)
        threshold = self.bar(decision.side_effect)
        return Verdict(score >= threshold, score, weakest, threshold)


# Frozen on the tuning data (the original 11 scenarios and the 4 synthetic clips) before any run on Mason's clips.
# python -m voice.segmentation.gate_tune: of 10 tasks dispatched during speech, the one wrong one scored 0.35 and the
# lowest right one 0.41. No side effect dispatched during speech there, so the side-effect bar is a judgment.
PLAN_A = Gate(threshold=0.40, side_effect_threshold=0.80)
