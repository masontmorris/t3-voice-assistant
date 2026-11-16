import unittest

from voice.segmentation.contract import TranscriptUpdate
from voice.segmentation.cursor import (CursorAnswer, CursorRunner, Pick, Verdict, commits, cursor_request,
                                       decision_for, judge, markers, words)
from voice.segmentation.tasks import Status


def answer(finished, at, margin=0.8, runner_up="none", side_effect=0.0, depends=None, corrects=None):
    return CursorAnswer(finished, Pick(at, margin, runner_up), side_effect, depends, corrects, {})


class Markers(unittest.TestCase):
    def gaps(self, text, cursor=0):
        ws = words(text)
        return [" ".join(w.text for w in ws[cursor:g]) for g in markers(text, ws, cursor)]

    def test_punctuation_and_connectives_mark_breaks_and_the_end_is_last(self):
        self.assertEqual(self.gaps("What's the weather in Denver, and look up the drive."),
                         ["what's the weather in denver", "what's the weather in denver and look up the drive"])

    def test_a_two_word_connective_is_one_marker(self):
        self.assertEqual(self.gaps("find the report and then summarize it"),
                         ["find the report", "find the report and then summarize it"])

    def test_markers_start_after_the_cursor(self):
        self.assertEqual(self.gaps("find the report and then summarize it", cursor=3), ["and then summarize it"])

    def test_a_long_stretch_without_markers_falls_back_to_every_word_gap(self):
        text = "one two three four five six seven eight nine ten eleven twelve thirteen"
        self.assertEqual(len(markers(text, words(text), 0)), 13)

    def test_nothing_after_the_cursor_has_no_markers(self):
        self.assertEqual(markers("find the report", words("find the report"), 3), [])


class Condition(unittest.TestCase):
    gaps = [3, 7, 9]

    def test_finished_and_a_marker_give_that_marker(self):
        self.assertEqual(judge(answer(0.9, "m2"), self.gaps, False), (Verdict.CANDIDATE, 7))

    def test_finished_without_a_marker_or_a_marker_without_finished_disagree(self):
        self.assertEqual(judge(answer(0.9, "none"), self.gaps, False), (Verdict.DISAGREE, None))
        self.assertEqual(judge(answer(0.2, "m1"), self.gaps, False), (Verdict.DISAGREE, None))

    def test_neither_finished_nor_located_is_nothing(self):
        self.assertEqual(judge(answer(0.2, "none"), self.gaps, False), (Verdict.NOTHING, None))

    def test_a_near_tie_on_a_partial_waits(self):
        self.assertEqual(judge(answer(0.9, "m2", margin=0.1, runner_up="m1"), self.gaps, False),
                         (Verdict.NEAR_TIE, None))

    def test_a_near_tie_on_the_final_transcript_takes_the_earlier_marker(self):
        self.assertEqual(judge(answer(0.9, "m2", margin=0.1, runner_up="m1"), self.gaps, True),
                         (Verdict.CANDIDATE, 3))

    def test_the_final_transcript_resolves_a_finished_request_with_no_marker_to_the_runner_up(self):
        self.assertEqual(judge(answer(0.9, "none", margin=0.5, runner_up="m3"), self.gaps, True),
                         (Verdict.CANDIDATE, 9))

    def test_the_final_transcript_never_commits_an_unfinished_request(self):
        self.assertEqual(judge(answer(0.2, "none", margin=0.1, runner_up="m3"), self.gaps, True),
                         (Verdict.NEAR_TIE, None))


class Stability(unittest.TestCase):
    words = ("find", "the", "report")

    def test_a_first_sighting_does_not_commit(self):
        self.assertFalse(commits(self.words, None, pause=False, side_effect=False, is_final=False))

    def test_holding_across_two_revisions_or_a_pause_commits(self):
        self.assertTrue(commits(self.words, self.words, pause=False, side_effect=False, is_final=False))
        self.assertTrue(commits(self.words, None, pause=True, side_effect=False, is_final=False))

    def test_a_changed_candidate_does_not_count_as_holding(self):
        self.assertFalse(commits(self.words, ("find", "the"), pause=False, side_effect=False, is_final=False))

    def test_a_side_effect_needs_both(self):
        self.assertFalse(commits(self.words, self.words, pause=False, side_effect=True, is_final=False))
        self.assertFalse(commits(self.words, None, pause=True, side_effect=True, is_final=False))
        self.assertTrue(commits(self.words, self.words, pause=True, side_effect=True, is_final=False))

    def test_the_final_transcript_commits(self):
        self.assertTrue(commits(self.words, None, pause=False, side_effect=True, is_final=True))


class Decision(unittest.TestCase):
    def test_a_near_tie_between_no_dependency_and_a_dependency_waits_on_it(self):
        d = decision_for(answer(0.9, "m1", depends=Pick("none", 0.15, "s1"), corrects=Pick("none", 0.9, "replace_s1")))
        self.assertEqual(d.depends_on, "s1")
        self.assertIsNone(d.corrects)

    def test_a_clear_no_dependency_stays_independent(self):
        d = decision_for(answer(0.9, "m1", depends=Pick("none", 0.6, "s1")))
        self.assertIsNone(d.depends_on)


def respond(script):
    """A fake decisions client. `script` maps a lowercase new_speech prefix to (finished, at, margin, side effect).

    The longest matching prefix wins, and speech that matches no key is unfinished.
    """
    class Fake:
        bodies = []

        def call(self, route, body, label=""):
            assert body["model"].endswith(":free")
            Fake.bodies.append(body)
            speech = body["state"]["new_speech"].lower()
            if any(name.endswith("_same_as") for name in body["questions"]):
                answers = {name: {"type": "choice", "choice": script["delegate"], "confidence": 0.9,
                                  "probabilities": {script["delegate"]: 0.95, "new": 0.05}}
                           for name in body["questions"]}
            else:
                keys = [k for k in script if k != "delegate" and speech.startswith(k)]
                fin, at, margin, side = script[max(keys, key=len)] if keys else (0.1, "none", 0.8, 0.0)
                answers = {"finished": {"type": "noul", "noul": fin},
                           "at": {"type": "choice", "choice": at, "confidence": margin,
                                  "probabilities": {at: 0.5 + margin / 2, "none" if at != "none" else "m1":
                                                    0.5 - margin / 2}},
                           "side_effect": {"type": "noul", "noul": side}}
                for name in ("depends", "corrects"):
                    if name in body["questions"]:
                        answers[name] = {"type": "choice", "choice": "none", "confidence": 0.9,
                                         "probabilities": {"none": 0.95, "x": 0.05}}
            return {"status": 200, "response": {"answers": answers},
                    "timing": {"ms": 1.0, "started_at": "t", "connect_ms": None}}
    Fake.bodies = []
    return Fake()


def scenario(*events):
    return {"id": "t", "covers": "", "events": [
        {"type": "delegate", "instruction": e[1]} if e[0] == "delegate" else
        {"type": "transcript", "revision": i + 1, "text": e[0], "is_final": e[1]} for i, e in enumerate(events)],
        "expected": {"tasks": [], "clarification_needed": False}}


class Runner(unittest.TestCase):
    def test_a_request_dispatches_when_its_break_holds_and_the_next_call_sees_only_words_after_it(self):
        client = respond({"look up the weather in denver [1]": (0.9, "m1", 0.8, 0.0),
                          "and find": (0.2, "none", 0.8, 0.0)})
        result = CursorRunner(client).run(scenario(("look up the weather in denver", False),
                                                   ("look up the weather in denver and find", False),
                                                   ("look up the weather in denver and find a", False)))
        self.assertEqual([(d["instruction"], d["revision"]) for d in result["dispatch_log"]],
                         [("look up the weather in denver", 2)])
        self.assertTrue(client.bodies[-1]["state"]["new_speech"].startswith("and find a"))

    def test_a_near_tie_holds_and_resets_stability(self):
        client = respond({"look up the weather in denver [1]": (0.9, "m1", 0.8, 0.0),
                          "look up the weather in denver [1] and": (0.9, "m1", 0.1, 0.0)})
        result = CursorRunner(client).run(scenario(("look up the weather in denver", False),
                                                   ("look up the weather in denver and", False),
                                                   ("look up the weather in denver and find", False)))
        self.assertEqual(result["dispatch_log"], [])
        self.assertEqual([p["verdict"] for p in result["decision_points"]], ["candidate", "near_tie", "near_tie"])

    def test_a_side_effect_commits_after_a_hold_and_a_pause_but_dispatches_only_on_final(self):
        client = respond({"email dana that the meeting moved [1] and": (0.9, "m1", 0.8, 1.0),
                          "email dana that the meeting moved": (0.9, "m1", 0.8, 1.0)})
        text = "email dana that the meeting moved and"
        result = CursorRunner(client).run(scenario(("email dana that the meeting moved", False), (text, False),
                                                   (text, False), ("Email Dana that the meeting moved, and", True)))
        commits_at = [p["revision"] for p in result["decision_points"] if p.get("committed")]
        self.assertEqual(commits_at, [3])
        self.assertEqual([(d["revision"], d["is_final"]) for d in result["dispatch_log"]], [(4, True)])

    def test_a_delegate_for_speech_in_progress_links_to_the_segment_and_dispatches_once(self):
        client = respond({"delegate": "new_speech",
                          "look up tomorrow's weather in seattle [1]": (0.9, "m1", 0.8, 0.0),
                          "look up tomorrow's weather": (0.9, "m1", 0.8, 0.0)})
        result = CursorRunner(client).run(scenario(("look up tomorrow's weather in seattle", False),
                                                   ("delegate", "Look up tomorrow's weather in Seattle"),
                                                   ("look up tomorrow's weather in seattle and", False)))
        self.assertEqual(len(result["dispatch_log"]), 1)
        task = result["tasks"][0]
        self.assertEqual((task["status"], task["sources"]), (Status.DISPATCHED.value, ["orchestrator", "voice_delegate"]))

    def test_the_final_transcript_drains_every_finished_request(self):
        client = respond({"find the report [1] and then": (0.9, "m1", 0.8, 0.0),
                          "and then summarize it": (0.9, "m1", 0.8, 0.0)})
        result = CursorRunner(client).run(scenario(("Find the report and then summarize it.", True)))
        self.assertEqual([s["text"] for s in result["segments"]], ["Find the report", "and then summarize it."])

    def test_the_request_body_asks_about_corrections_only_once_a_segment_exists(self):
        ws = words("find the report")
        update = TranscriptUpdate(1, "find the report", False)
        first = cursor_request(update, ws, 0, markers(update.text, ws, 0), {})
        later = cursor_request(update, ws, 0, markers(update.text, ws, 0), {"s1": "look up the weather"})
        self.assertNotIn("corrects", first["questions"])
        self.assertEqual(set(later["questions"]["corrects"]["criteria"]), {"none", "replace_s1", "cancel_s1"})


if __name__ == "__main__":
    unittest.main()
