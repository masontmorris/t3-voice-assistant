import unittest

from voice.segmentation.contract import Kind, Readiness, TranscriptUpdate, decision_request, parse_decisions
from voice.segmentation.client import MODEL
from voice.segmentation.spans import clause_segments, extracted_segments, fine_pieces, merge_pieces


def body_for(text, final=True, delegates=None):
    return decision_request(TranscriptUpdate(1, text, final), clause_segments(text, final), [], delegates or {})


def good_answers(body, **overrides):
    answers = {}
    for name, q in body["questions"].items():
        if q["type"] == "noul":
            answers[name] = {"type": "noul", "noul": 0.1}
        else:
            first = next(iter(q["criteria"]))
            answers[name] = {"type": "choice", "choice": first, "probabilities": {first: 1.0}, "confidence": 1.0}
    answers.update(overrides)
    return {"answers": answers}


class Splitter(unittest.TestCase):
    def test_splits_at_sentences_and_spoken_markers(self):
        cases = {
            "Find the report and then summarize it.": ["Find the report", "and then summarize it."],
            "Book a table for four, no wait, for six.": ["Book a table for four", "no wait, for six."],
            "Look up the score. Anyway, different thing. What's for dinner?":
                ["Look up the score.", "Anyway, different thing.", "What's for dinner?"],
        }
        for text, expected in cases.items():
            self.assertEqual([s.text for s in clause_segments(text, True)], expected, text)

    def test_drops_filler_only_pieces_and_marks_the_trailing_piece_open(self):
        segments = clause_segments("look up the weather in seattle and um", False)
        self.assertEqual([(s.text, s.open) for s in segments], [("look up the weather in seattle", True)])

    def test_spans_index_the_original_text(self):
        text = "Um, so, can you find the report, and email it?"
        for s in clause_segments(text, True):
            self.assertEqual(text[s.start:s.end], s.text)

    def test_boundary_answers_merge_pieces(self):
        text = "Um, can you, can you find the report, and email it?"
        pieces = fine_pieces(text)
        self.assertEqual([text[a:b] for a, b in pieces], ["can you", "can you find the report", "and email it?"])
        response = {"answers": {"b1": {"type": "noul", "noul": 0.1}, "b2": {"type": "noul", "noul": 0.9}}}
        segments, problems = merge_pieces(text, pieces, response, True)
        self.assertEqual([s.text for s in segments], ["can you, can you find the report", "and email it?"])
        self.assertEqual(problems, [])

    def test_extraction_spans_become_segments_and_bad_spans_fall_back_to_text(self):
        text = "find the report and email it to dana"
        response = {"answers": {"segments": {"type": "list", "value": ["find the report", "email it to dana"],
                                             "spans": [[0, 15], None]}}}
        segments, problems = extracted_segments(text, response, False)
        self.assertEqual([(s.text, s.open) for s in segments], [("find the report", False),
                                                                 ("email it to dana", True)])
        self.assertEqual(problems, ["segments[1]: no span, located by text"])


class Parsing(unittest.TestCase):
    def test_request_offers_only_earlier_segments_for_dependencies_and_corrections(self):
        body = body_for("Find the report and then summarize it.")
        self.assertEqual(body["model"], MODEL)
        self.assertNotIn("s1_depends", body["questions"])
        self.assertEqual(set(body["questions"]["s2_depends"]["criteria"]), {"none", "s1"})
        self.assertEqual(set(body["questions"]["s2_corrects"]["criteria"]), {"none", "replace_s1", "cancel_s1"})

    def test_well_formed_answers_parse_into_decisions(self):
        body = body_for("Find the report and then summarize it.")
        response = good_answers(body, s2_depends={"type": "choice", "choice": "s1"},
                                s2_side_effect={"type": "noul", "noul": 0.7})
        parsed = parse_decisions(response, body)
        self.assertEqual(parsed.problems, [])
        self.assertEqual(parsed.segments["s1"].kind, Kind.REQUEST)
        self.assertEqual(parsed.segments["s1"].readiness, Readiness.DISPATCH_NOW)
        self.assertEqual(parsed.segments["s2"].depends_on, "s1")
        self.assertTrue(parsed.segments["s2"].side_effect)

    def test_an_unoffered_choice_drops_only_that_segment(self):
        body = body_for("Find the report and then summarize it.")
        response = good_answers(body, s2_kind={"type": "choice", "choice": "dance"})
        parsed = parse_decisions(response, body)
        self.assertEqual(set(parsed.segments), {"s1"})
        self.assertEqual(len(parsed.problems), 1)

    def test_refusal_and_missing_answers_are_problems_not_exceptions(self):
        body = body_for("Find the report.")
        response = good_answers(body, s1_ready={"type": "refusal"})
        del response["answers"]["s1_side_effect"]
        parsed = parse_decisions(response, body)
        self.assertEqual(parsed.segments, {})
        self.assertEqual(len(parsed.problems), 2)
        self.assertEqual(parse_decisions({"error": "x"}, body).problems, ["no answers object"])

    def test_delegate_answers_name_a_segment_or_none(self):
        body = body_for("Find the report.", delegates={"d1": "Find the report"})
        self.assertEqual(set(body["questions"]["d1_same_as"]["criteria"]), {"new", "s1"})
        parsed = parse_decisions(good_answers(body, d1_same_as={"type": "choice", "choice": "s1"}), body)
        self.assertEqual(parsed.delegates, {"d1": "s1"})
        parsed = parse_decisions(good_answers(body, d1_same_as={"type": "choice", "choice": "new"}), body)
        self.assertEqual(parsed.delegates, {"d1": None})


if __name__ == "__main__":
    unittest.main()
