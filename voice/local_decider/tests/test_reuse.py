import unittest

from voice.local_decider.reuse import ReusingClient
from voice.segmentation.contract import TranscriptUpdate
from voice.segmentation.tests.test_hybrid import FakeDecider, stream


def body(segments, open_sid=None, extra=()):
    questions = {}
    for sid, text in segments.items():
        for q in ("kind", "ready", "finished", "at"):
            questions[f"{sid}_{q}"] = {"type": "choice", "instructions": f"{q} of {text}", "criteria": {"a": "", "b": ""}}
    for name in extra:
        questions[name] = {"type": "choice", "instructions": name, "criteria": {"a": "", "b": ""}}
    return {"model": "m:free", "state": {"segments": segments, "still_being_spoken": open_sid}, "questions": questions}


class Recorder:
    def __init__(self):
        self.asked = []

    def call(self, route, body, label=""):
        self.asked.append(sorted(body["questions"]))
        answers = {n: {"type": "choice", "choice": "a", "probabilities": {"a": 0.9, "b": 0.1}, "confidence": 0.8}
                   for n in body["questions"]}
        return {"status": 200, "response": {"answers": answers, "usage": {"input_tokens": len(answers)}},
                "timing": {"ms": 1.0, "started_at": "t"}}


class Reuse(unittest.TestCase):
    def test_a_repeated_request_asks_only_the_break_questions_of_the_last_two_pieces(self):
        inner = Recorder()
        client = ReusingClient(inner)
        segments = {"s1": "find a recipe", "s2": "then make a list", "s3": "and check the weather"}
        client.call("decisions", body(segments), "u:r1")
        record = client.call("decisions", body(segments), "u:r2")
        self.assertEqual(inner.asked[-1], ["s2_at", "s2_finished", "s3_at", "s3_finished"])
        self.assertEqual(sorted(record["response"]["answers"]), sorted(body(segments)["questions"]))

    def test_a_piece_whose_text_changed_is_asked_again(self):
        inner = Recorder()
        client = ReusingClient(inner)
        client.call("decisions", body({"s1": "find a recipe", "s2": "then make"}), "u:r1")
        client.call("decisions", body({"s1": "find a recipe", "s2": "then make a list"}), "u:r2")
        self.assertEqual(inner.asked[-1], ["s1_at", "s1_finished", "s2_at", "s2_finished", "s2_kind", "s2_ready"])

    def test_a_piece_that_stops_being_spoken_is_asked_again(self):
        inner = Recorder()
        client = ReusingClient(inner)
        segments = {"s1": "find a recipe", "s2": "then make a list", "s3": "and call mom"}
        client.call("decisions", body(segments, open_sid="s1"), "u:r1")
        client.call("decisions", body(segments), "u:r2")
        self.assertEqual(inner.asked[-1], ["s1_at", "s1_finished", "s1_kind", "s1_ready",
                                           "s2_at", "s2_finished", "s3_at", "s3_finished"])

    def test_a_question_about_no_segment_is_always_asked(self):
        inner = Recorder()
        client = ReusingClient(inner)
        client.call("decisions", body({"s1": "a"}, extra=["d1_same_as"]), "u:r1")
        client.call("decisions", body({"s1": "a"}, extra=["d1_same_as"]), "u:r2")
        self.assertIn("d1_same_as", inner.asked[-1])

    def test_answers_are_not_shared_between_utterances(self):
        inner = Recorder()
        client = ReusingClient(inner)
        segments = {"s1": "find a recipe", "s2": "then make a list", "s3": "and call mom"}
        client.call("decisions", body(segments), "one:r1")
        client.call("decisions", body(segments), "two:r1")
        self.assertEqual(len(inner.asked[-1]), len(body(segments)["questions"]))

    def test_nothing_new_to_ask_makes_no_call(self):
        inner = Recorder()
        client = ReusingClient(inner)
        client.call("decisions", body({"s1": "a"}, extra=()), "u:r1")
        only_kind = {"model": "m:free", "state": {"segments": {"s1": "a", "s2": "b", "s3": "c"}},
                     "questions": {"s1_kind": body({"s1": "a"})["questions"]["s1_kind"]}}
        record = client.call("decisions", only_kind, "u:r2")
        self.assertEqual(len(inner.asked), 1)
        self.assertEqual(record["response"]["answers"]["s1_kind"]["choice"], "a")
        self.assertEqual(record["timing"]["ms"], 0.0)


class HybridWithReuse(unittest.TestCase):
    """With a decider whose answers depend only on the question and its piece, reuse changes no outcome."""

    TEXTS = ["find the recipe for lentil soup",
             "find the recipe for lentil soup then make a shopping",
             "find the recipe for lentil soup then make a shopping list",
             "find the recipe for lentil soup then make a shopping list and check the weather",
             "Find the recipe for lentil soup, then make a shopping list and check the weather."]

    def test_reuse_keeps_the_dispatches_and_asks_fewer_questions(self):
        plain, inner = FakeDecider(starters={"then"}), FakeDecider(starters={"then"})
        without = stream(self.TEXTS, plain)
        with_reuse = stream(self.TEXTS, ReusingClient(inner))
        self.assertEqual(without.dispatch_log, with_reuse.dispatch_log)
        self.assertEqual(without.records(), with_reuse.records())
        asked = lambda d: sum(len(b["questions"]) for b in d.bodies)
        self.assertLess(asked(inner), asked(plain))


if __name__ == "__main__":
    unittest.main()
