"""What the product does with a reply, end to end through the task service: each recorded reader answer replayed as the
answer to a real question of its kind, and the action looked for where it happens (a launch, a message or a stop in the
journal, a respond or answer call on the seam, a status read for a pick). `reads_yes` alone is not this: a reply it
accepts must also be acted on, and one it refuses must not be.

    python -m voice.live.tests.test_answer_path      # prints the table the PR quotes
"""

from __future__ import annotations

import collections
import json
import unittest
from pathlib import Path

import voice.live.tests.test_t3_tasks as T3T
from voice.live.t3_tasks import ANSWER, APPROVE, ASK, DENY, STOP, TELL
from voice.t3 import fakes as F
from voice.t3.acts import Act
from voice.t3.acts import Kind as S
from voice.t3.verify import judge_reading

FIXTURES = Path(__file__).resolve().parents[2] / "t3" / "fixtures"


def rows():
    """Every recorded reply to a confirmation, as (source, act, reply, options, must_act, answers)."""
    out = []
    for r in json.loads((FIXTURES / "reply_marks.json").read_text(encoding="utf-8")):
        out.append((r["split"], Act.from_json(r["act"]), r["reply"], (), r["must_act"], r["answers"]))
    for r in json.loads((FIXTURES / "reply_reads.json").read_text(encoding="utf-8"))["rows"]:
        act = Act.from_json(r["act"])
        if act.kind is S.ASK_WHICH:
            continue
        answers = {"reading": {"choice": r["choice"], "probabilities": r["p"]},
                   **{k: {"type": "noul", "noul": v} for k, v in r["noul"].items()}}
        if r.get("target_answer"):
            answers["target"] = r["target_answer"]
        out.append(("reads", act, r["reply"], tuple(r["options"]), r["must_act"], answers))
    return out


def key(act: Act) -> str:
    return act.kind.value + "/" + str(act.slots.get("decision"))


class Replay(F.Reader):
    """The decider as the recording had it: the reading recorded for this kind of question and this reply."""

    def __init__(self, recorded):
        super().__init__({})
        self.recorded = recorded

    def reads(self, said, reply, options=()):
        answers = self.recorded[(key(said), reply, tuple(options))]
        return judge_reading(answers, tuple(options))


def listing_of(*pairs):
    return T3T.listing(*pairs)


def acted(act: Act, reply: str, options: tuple, recorded: dict) -> bool | None:
    """Whether the product acted on `reply` as the answer to the confirmation `act`, or None when the confirmation
    could not be put (its kind is not one the service asks in this setup)."""
    decider = Replay(recorded)
    kind = act.kind
    if kind is S.CONFIRM_LAUNCH:
        projects = (("p1", "Voice sandbox", ("sandbox", "voice sandbox")),
                    *((f"p{i}", name, (name.lower(),)) for i, name in enumerate(options, 2)))
        seam = F.FakeSeam(projects=projects)
        t3 = T3T.tasks_over(unittest.TestCase(), seam, decider=decider)
        update = t3.start("k1", "Add a clamp helper in the sandbox project", 0)
        if [a.kind for a in update.acts] != [S.CONFIRM_LAUNCH]:
            return None
        t3.spoke(update)
        t3.answer(reply, 1)
        return bool(seam.ops("launch"))
    if kind in (S.CONFIRM_TELL, S.CONFIRM_STOP, S.CONFIRM_THREAD):
        seam = F.FakeSeam(read=F.state(thread="t3", title="Add a clamp helper", as_of=150))
        seam.listing = listing_of(("t3", "Add a clamp helper"), ("t0", "Fix the menu blur"))
        t3 = T3T.tasks_over(unittest.TestCase(), seam, decider=decider)
        op, words = {S.CONFIRM_TELL: (TELL, "Tell the clamp helper thread to also add a test."),
                     S.CONFIRM_STOP: (STOP, "Stop the clamp helper thread."),
                     S.CONFIRM_THREAD: (ASK, "How is the clamp helper thread going?")}[kind]
        update = t3.about("k1", op, words, 0)
        if [a.kind for a in update.acts] != [kind]:
            return None
        t3.spoke(update)
        done = t3.answer(reply, 1)
        if kind is S.CONFIRM_THREAD:
            return done is not None and done.acts[0].kind is S.THREAD_STATUS
        return bool(t3.follow.journal.tasks)
    if kind is S.CONFIRM_RESPOND:
        case = T3T.Respond()
        seam = case.seam()
        t3 = T3T.tasks_over(unittest.TestCase(), seam, decider=decider)
        decision = act.slots["decision"]
        update = t3.about("k1", APPROVE if decision == "approve" else DENY,
                          "I approve." if decision == "approve" else "No, do not allow that.", 0)
        if update.status != "needs_clarification":
            return None
        t3.spoke(update)
        try:
            t3.answer(reply, 1)
        except Exception:
            pass
        return bool(seam.ops("respond"))
    if kind is S.CONFIRM_ANSWER:
        case = T3T.Answer()
        seam = case.seam()
        reader = F.Reader(T3T.READINGS, choosing={"Postgres.": 0})
        t3 = T3T.tasks_over(unittest.TestCase(), seam, decider=reader)
        update = t3.about("k1", ANSWER, "Postgres.", 0)
        if update.status != "needs_clarification":
            return None
        t3.spoke(update)
        t3.decider = decider
        try:
            t3.answer(reply, 1)
        except Exception:
            pass
        return bool(seam.ops("answer_question"))
    return None


def table():
    recorded = {(key(act), reply, options): answers for _, act, reply, options, _, answers in rows()}
    counts = collections.defaultdict(collections.Counter)
    skipped = 0
    for source, act, reply, options, must_act, _ in rows():
        taken = acted(act, reply, options, recorded)
        if taken is None:
            skipped += 1
            continue
        group = counts[key(act).replace("/None", "")]
        group["yes rows" if must_act else "not-yes rows"] += 1
        group["yes taken" if must_act else "not-yes taken"] += bool(taken)
    return counts, skipped


class AnswerPath(unittest.TestCase):
    def test_a_reply_that_is_not_a_yes_never_acts_but_the_known_few_and_a_marked_yes_does(self):
        counts, skipped = table()
        self.assertEqual(skipped, 0, "every recorded confirmation could be put to the service")
        taken = {kind: c["not-yes taken"] for kind, c in counts.items()}
        # The cut-off words and the replies that agree and go on, the rows `KNOWN_TAKEN` names in
        # voice/t3/test_reply_marks.py; one of the second kind answers an approval.
        self.assertEqual({kind: n for kind, n in taken.items() if n}, {"confirm_launch": 6, "confirm_respond/approve": 1})
        self.assertGreaterEqual(counts["confirm_launch"]["yes taken"], 66)
        # A rising yes is taken about 3 times in 4 over 5 draws (voice/t3/test_reply_marks.py). This recording read
        # "Yeah?" to a message and "Okay?" to a stop as questions with yes under the 0.3 floor; each is asked once more.
        for kind, missed in (("confirm_tell", 1), ("confirm_stop", 1)):
            self.assertEqual(counts[kind]["yes taken"], counts[kind]["yes rows"] - missed, kind)


if __name__ == "__main__":
    counts, skipped = table()
    for kind in sorted(counts):
        c = counts[kind]
        print(f"{kind:24} real yeses taken {c['yes taken']:3} of {c['yes rows']:3} | not yes taken {c['not-yes taken']:2} of "
              f"{c['not-yes rows']:3}")
    print("skipped", skipped)
