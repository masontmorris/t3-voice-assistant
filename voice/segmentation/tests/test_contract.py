import unittest

from voice.segmentation.contract import Kind, Readiness, TranscriptUpdate, decision_request, parse_decisions
from voice.segmentation.client import MODEL
from voice.segmentation.spans import clause_segments, extracted_segments, fine_pieces, merge_pieces
from voice.tools import registry
from voice.tools.registry import GENERAL, NONE, Tool, ToolChoice

TOOLS = registry.build(None, None, None).choices()


def body_for(text, final=True, delegates=None):
    return decision_request(TranscriptUpdate(1, text, final), clause_segments(text, final), [], delegates or {})


def good_answers(body, **overrides):
    answers = {}
    for name, q in body["questions"].items():
        if q["type"] == "noul":
            answers[name] = {"type": "noul", "noul": 0.1}
        elif q["type"] == "text":
            answers[name] = {"type": "text", "text": "Denver", "found": 0.9}
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


class ToolQuestion(unittest.TestCase):
    """The tool question is the registry's own table: every registered tool, the general worker, and none."""

    def body(self, text="What's the weather in Denver tomorrow?", **kw):
        return decision_request(TranscriptUpdate(1, text, True), clause_segments(text, True), [], {}, **kw)

    def test_every_registered_tool_appears_in_the_question_and_nothing_else(self):
        body = self.body(tools=TOOLS)
        criteria = set(body["questions"]["s1_tool"]["criteria"])
        tools = set(registry.build(None, None, None).tools)
        self.assertEqual(criteria, tools | {NONE, GENERAL})
        self.assertEqual(criteria - tools - {NONE, GENERAL}, set(), "nothing but the table is offered")

    def test_a_tool_added_to_the_registry_appears_with_no_other_edit(self):
        extra = Tool("calendar", "calendar lookup", "Reads the speaker's own calendar for a day.",
                     lambda instruction, earlier=None: {}, lambda arguments, deadline: ("", {}))
        runner = registry.Registry([*registry.build(None, None, None).tools.values(), extra])
        criteria = self.body(tools=runner.choices())["questions"]["s1_tool"]["criteria"]
        self.assertEqual(criteria["calendar"], "Reads the speaker's own calendar for a day.")

    def test_a_request_with_no_registry_asks_nothing_about_tools(self):
        self.assertNotIn("s1_tool", self.body()["questions"])

    def test_the_tool_answer_carries_its_margin_and_a_bad_one_leaves_the_segment_whole(self):
        body = self.body(tools=TOOLS)
        parsed = parse_decisions(good_answers(body, s1_tool={"type": "choice", "choice": "weather",
                                                            "confidence": 0.62}), body)
        self.assertEqual(parsed.segments["s1"].tool, ToolChoice("weather", 0.62))
        broken = parse_decisions(good_answers(body, s1_tool={"type": "choice", "choice": "dance"}), body)
        self.assertIsNone(broken.segments["s1"].tool, "the word rules take a task with no readable tool answer")
        self.assertEqual(set(broken.segments), {"s1"})
        self.assertEqual(broken.problems, ["s1_tool: type='choice' choice='dance'"])

    def test_the_read_place_and_day_ride_along_with_the_tool_answer(self):
        body = self.body(tools=TOOLS, arguments=True)
        response = good_answers(body, s1_place={"type": "text", "text": "Denver", "found": 0.99},
                                s1_when={"type": "choice", "choice": "tomorrow", "confidence": 0.9})
        parsed = parse_decisions(response, body)
        self.assertEqual(parsed.segments["s1"].tool.arguments, {"place": "Denver", "when": "tomorrow"})
        self.assertEqual(parsed.problems, [])

    def test_a_place_answer_that_is_not_a_name_leaves_the_argument_to_the_words(self):
        """The recorded answer from turn 020 of the 37-turn replay: the model copied the request's own state."""
        body = self.body(tools=TOOLS, arguments=True)
        echoed = 'Seattle right now? [1]",\n  "prior_resolved_tasks": [\n    {\n      "call": {\n        "arguments"'
        response = good_answers(body, s1_place={"type": "text", "text": echoed},
                                s1_when={"type": "choice", "choice": "today", "confidence": 0.9})
        parsed = parse_decisions(response, body)
        self.assertIsNone(parsed.segments["s1"].tool.arguments["place"])
        self.assertTrue(parsed.problems[0].startswith("s1_place: not a name"), parsed.problems)
        spelled = good_answers(body, s1_place={"type": "text", "text": "Coeur d'Alene"},
                               s1_when={"type": "choice", "choice": "today"})
        self.assertEqual(parse_decisions(spelled, body).segments["s1"].tool.arguments["place"], "Coeur d'Alene",
                         "a name with an apostrophe is a name")


if __name__ == "__main__":
    unittest.main()
