"""The one rule that lets a reply authorise an action (`reads_yes`), as a truth table, and the same for each kind of
confirmation through the reader: every answer it rests on missing or malformed, and every disagreement between them,
is not a yes."""

from __future__ import annotations

import itertools
import unittest

from voice.t3.acts import Kind, act
from voice.t3.verify import KEEPS, Decider, judge_reading, reads_yes

WHOLE = {"yes": 0.55, "question": 0.3, "no": 0.05, "other": 0.03, "unrelated": 0.07}
LABELS = tuple(WHOLE)


class Canned:
    def __init__(self, answers):
        self.answers = answers

    def call(self, route, body, label=""):
        return {"status": 200, "response": {"answers": self.answers}}


class Pure(unittest.TestCase):
    def test_the_truth_table(self):
        good = dict(label="yes", shares=WHOLE, agrees=0.8, asks=0.2, adds=0.1, kept=True, closes=0.1)
        self.assertTrue(reads_yes(**good))
        self.assertTrue(reads_yes(**{**good, "label": "question"}), "a yes the transcript marked as a question")
        for name, value in (
            ("label", None), ("label", "no"), ("label", "other"), ("label", "unrelated"), ("label", "maybe"),
            ("shares", None), ("shares", {**WHOLE, "no": 0.25, "yes": 0.35}), ("shares", {**WHOLE, "other": 0.25}),
            ("shares", {"yes": 0.2, "question": 0.2, "unrelated": 0.4, "no": 0.1, "other": 0.1}),
            ("shares", {}),
            ("agrees", None), ("agrees", 0.49), ("asks", None), ("asks", 0.7), ("asks", 0.95), ("adds", None),
            ("adds", 0.5), ("adds", 0.9), ("kept", False), ("closes", None), ("closes", 0.55), ("closes", 0.9)):
            with self.subTest(**{name: value}):
                self.assertFalse(reads_yes(**{**good, name: value}))

    def test_a_question_label_is_a_yes_only_with_a_real_share_of_yes(self):
        marked = {"yes": 0.36, "question": 0.5, "no": 0.04, "other": 0.04, "unrelated": 0.06}
        self.assertTrue(reads_yes("question", marked, 0.8, 0.2, 0.1, True, 0.1))
        for yes in (0.0, 0.1, 0.29):
            shares = {**marked, "yes": yes, "question": 0.86 - yes}
            with self.subTest(yes=yes):
                self.assertFalse(reads_yes("question", shares, 0.9, 0.1, 0.1, True, 0.1))
        self.assertTrue(reads_yes("question", {**marked, "yes": 0.3, "question": 0.56}, 0.9, 0.1, 0.1, True, 0.1))
        self.assertTrue(reads_yes("yes", {**marked, "yes": 0.55, "question": 0.31}, 0.9, 0.1, 0.1, True, 0.1),
                        "the floor is for the question label only")

    def test_every_combination_of_a_missing_answer_is_not_a_yes(self):
        names = ("label", "shares", "agrees", "asks", "adds", "closes")
        for gone in itertools.chain.from_iterable(itertools.combinations(names, n) for n in (1, 2, 3, 4, 5, 6)):
            args = dict(label="yes", shares=WHOLE, agrees=0.8, asks=0.2, adds=0.1, kept=True, closes=0.1)
            args.update({name: None for name in gone})
            with self.subTest(missing=gone):
                self.assertFalse(reads_yes(**args))

    def test_each_label_with_agreeing_questions_is_a_yes_only_as_yes_or_question(self):
        for label in LABELS:
            with self.subTest(label=label):
                self.assertEqual(reads_yes(label, WHOLE, 0.9, 0.1, 0.1, True, 0.1), label in ("yes", "question"))


def valid():
    return {"reading": {"choice": "yes", "probabilities": dict(WHOLE)},
            "agrees": {"type": "noul", "noul": 0.8}, "asks": {"type": "noul", "noul": 0.2},
            "adds": {"type": "noul", "noul": 0.1}, "closes": {"type": "noul", "noul": 0.1},
            "withdraws": {"type": "noul", "noul": 0.05},
            "target": {"choice": KEEPS, "probabilities": {KEEPS: 0.9, "option_1": 0.1}}}


# One confirmation of each kind the reader serves, with the alternatives a launch can carry.
CONFIRMATIONS = {
    "launch": (act(Kind.CONFIRM_LAUNCH, project="Voice sandbox"), ()),
    "launch with one other project": (act(Kind.CONFIRM_LAUNCH, project="Voice sandbox"), ("Voice server",)),
    "launch with two other projects": (act(Kind.CONFIRM_LAUNCH, project="Voice sandbox"), ("Voice server", "Docs site")),
    "tell": (act(Kind.CONFIRM_TELL, thread="Add a clamp helper", message="also add a test"), ()),
    "stop": (act(Kind.CONFIRM_STOP, thread="Add a clamp helper"), ()),
    "thread": (act(Kind.CONFIRM_THREAD, thread="Add a clamp helper"), ()),
    "approve": (act(Kind.CONFIRM_RESPOND, thread="Add a clamp helper", decision="approve", need="command_approval",
                    summary="it wants to list the files"), ()),
    "deny": (act(Kind.CONFIRM_RESPOND, thread="Add a clamp helper", decision="deny", need="command_approval"), ()),
    "answer": (act(Kind.CONFIRM_ANSWER, thread="Add a clamp helper", questions=["Which?"], answers=["Postgres"]), ()),
}


def mutations():
    """Every way of breaking one answer, by name, as a function from the valid answers to the broken ones."""

    def edit(name, change):
        def apply(answers):
            change(answers)
            return answers

        return name, apply

    out = [
        edit("reading missing", lambda a: a.pop("reading")),
        edit("reading not a mapping", lambda a: a.update(reading="yes")),
        edit("reading without a choice", lambda a: a["reading"].pop("choice")),
        edit("reading choice not a label", lambda a: a["reading"].update(choice="maybe")),
        edit("reading shares missing", lambda a: a["reading"].pop("probabilities")),
        edit("reading shares empty", lambda a: a["reading"].update(probabilities={})),
        edit("reading shares partial", lambda a: a["reading"].update(probabilities={"yes": 0.9, "question": 0.1})),
        edit("reading shares sum low", lambda a: a["reading"].update(probabilities={k: v / 2 for k, v in WHOLE.items()})),
        edit("reading shares sum high", lambda a: a["reading"].update(probabilities={k: v * 2 for k, v in WHOLE.items()})),
        edit("reading share out of range", lambda a: a["reading"]["probabilities"].update(yes=1.5)),
        edit("reading share negative", lambda a: a["reading"]["probabilities"].update(no=-0.05, yes=0.65)),
        edit("reading share a string", lambda a: a["reading"]["probabilities"].update(yes="0.55")),
        edit("reading share a boolean", lambda a: a["reading"]["probabilities"].update(yes=True)),
        edit("reading share NaN", lambda a: a["reading"]["probabilities"].update(yes=float("nan"))),
        edit("reading shares with an unknown label", lambda a: a["reading"]["probabilities"].update(sure=0.0)),
    ]
    for name in ("agrees", "asks", "adds", "closes"):
        out += [
            edit(f"{name} missing", lambda a, n=name: a.pop(n)),
            edit(f"{name} not a mapping", lambda a, n=name: a.update({n: 0.9})),
            edit(f"{name} without a share", lambda a, n=name: a[n].pop("noul")),
            edit(f"{name} a string", lambda a, n=name: a[n].update(noul="0.9")),
            edit(f"{name} a boolean", lambda a, n=name: a[n].update(noul=True)),
            edit(f"{name} NaN", lambda a, n=name: a[n].update(noul=float("nan"))),
            edit(f"{name} negative", lambda a, n=name: a[n].update(noul=-0.1)),
            edit(f"{name} above one", lambda a, n=name: a[n].update(noul=1.1)),
        ]
    out += [
        edit("the choice says no", lambda a: a["reading"].update(
            choice="no", probabilities={**WHOLE, "no": 0.55, "yes": 0.05})),
        edit("the choice says other", lambda a: a["reading"].update(
            choice="other", probabilities={**WHOLE, "other": 0.55, "yes": 0.05})),
        edit("the choice says question with no share of yes", lambda a: a["reading"].update(
            choice="question", probabilities={"yes": 0.0, "question": 0.9, "no": 0.03, "other": 0.03, "unrelated": 0.04})),
        edit("the choice says question with too little yes", lambda a: a["reading"].update(
            choice="question", probabilities={"yes": 0.25, "question": 0.65, "no": 0.03, "other": 0.03, "unrelated": 0.04})),
        edit("the choice says unrelated", lambda a: a["reading"].update(
            choice="unrelated", probabilities={**WHOLE, "unrelated": 0.55, "yes": 0.05})),
        edit("a real share of no", lambda a: a["reading"]["probabilities"].update(no=0.3, yes=0.3)),
        edit("a real share of other", lambda a: a["reading"]["probabilities"].update(other=0.3, yes=0.28)),
        edit("the words do not say yes", lambda a: a["agrees"].update(noul=0.3)),
        edit("the words ask for something", lambda a: a["asks"].update(noul=0.9)),
        edit("the words add a remark about something else", lambda a: a["adds"].update(noul=0.9)),
        edit("the choice says yes and the words say no", lambda a: a["agrees"].update(noul=0.01)),
        edit("the words thank or close instead of answering", lambda a: a["closes"].update(noul=0.8)),
    ]
    return out


class Confirmations(unittest.TestCase):
    def read(self, kind, answers):
        said, options = CONFIRMATIONS[kind]
        return Decider(Canned(answers)).reads(said, "Yeah?", options)

    def test_the_agreeing_answers_are_a_yes_for_every_kind(self):
        for kind in CONFIRMATIONS:
            with self.subTest(kind):
                self.assertTrue(self.read(kind, valid()).yes)

    def test_every_missing_malformed_or_disagreeing_answer_is_not_a_yes_for_every_kind(self):
        for kind in CONFIRMATIONS:
            for name, apply in mutations():
                with self.subTest(kind=kind, mutation=name):
                    self.assertFalse(self.read(kind, apply(valid())).yes)

    def test_a_target_answer_that_is_missing_or_does_not_say_keep_is_not_a_yes_when_alternatives_are_offered(self):
        def shares(**s):
            return {"choice": max(s, key=s.get), "probabilities": s}

        for kind in ("launch with one other project", "launch with two other projects"):
            for name, target in (("missing", None), ("not a mapping", "keeps"), ("empty", {}),
                                 ("no choice", {"probabilities": {KEEPS: 0.9}}),
                                 ("choice not offered", shares(maybe=0.9, keeps=0.1)),
                                 ("keeps too low", shares(keeps=0.55, option_1=0.45)),
                                 ("changes to one", shares(option_1=0.9, keeps=0.1)),
                                 ("changes without saying which", shares(elsewhere=0.9, keeps=0.1)),
                                 ("shares above one", shares(keeps=0.9, option_1=0.9))):
                answers = valid()
                if target is None:
                    answers.pop("target")
                else:
                    answers["target"] = target
                with self.subTest(kind=kind, target=name):
                    self.assertFalse(self.read(kind, answers).yes)

    def test_no_alternatives_means_no_target_answer_is_needed(self):
        answers = valid()
        answers.pop("target")
        for kind in ("launch", "tell", "stop", "thread", "approve", "deny", "answer"):
            with self.subTest(kind):
                self.assertTrue(self.read(kind, answers).yes)

    def test_a_yes_the_transcript_marked_as_a_question_is_a_yes_for_every_kind(self):
        marked = valid()
        marked["reading"] = {"choice": "question",
                             "probabilities": {"question": 0.45, "yes": 0.4, "unrelated": 0.05, "no": 0.05, "other": 0.05}}
        for kind in CONFIRMATIONS:
            with self.subTest(kind):
                self.assertTrue(self.read(kind, marked).yes)

    def test_judge_reading_never_raises_on_what_a_decider_could_send(self):
        for junk in (None, [], "yes", 3, {"reading": []}, {"reading": None}):
            self.assertFalse(judge_reading(junk).yes)


if __name__ == "__main__":
    unittest.main()
