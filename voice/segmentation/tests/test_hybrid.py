import re
import unittest

from voice.segmentation.contract import TranscriptUpdate
from voice.segmentation.hybrid import HybridSegmenter, Proposal, hard_cuts
from voice.segmentation.cursor import words

SIDE_EFFECT_WORDS = {"email", "buy", "book", "text", "send", "post", "delete"}


def choice(pick, options, margin=0.8):
    probs = {o: 0.0 for o in options}
    probs[pick] = 0.5 + margin / 2
    rest = [o for o in options if o != pick]
    if rest:
        probs[rest[0]] = 0.5 - margin / 2
    return {"type": "choice", "choice": pick, "probabilities": probs, "confidence": margin}


class FakeDecider:
    """Answers like a careful decider. A request ends before a word in `starters`. Kinds come from the opening words."""

    def __init__(self, starters=(), ready_margin=0.8, depends=None, malformed=False):
        self.starters, self.ready_margin, self.depends, self.malformed = set(starters), ready_margin, depends or {}, \
            malformed
        self.bodies = []

    @staticmethod
    def _after(marked):
        """The first word after each marker [k] in new_speech, or None after the last."""
        parts = re.split(r" \[(\d+)\]", marked)
        return {int(parts[i]): (parts[i + 1].split() or [None])[0] for i in range(1, len(parts), 2)}

    def call(self, route, body, label=""):
        self.bodies.append(body)
        if self.malformed:
            return {"status": 200, "response": {"answers": {}}, "timing": {"ms": 1.0, "started_at": "t"}}
        state, answers = body["state"], {}
        after = self._after(state["new_speech"])
        segments = state["segments"]
        for name, q in body["questions"].items():
            sid, _, kind = name.partition("_")
            options = list(q["criteria"])
            text = segments.get(sid, "").lower()
            if kind == "kind":
                if text.startswith(("actually", "no wait", "i mean", "never mind")):
                    answers[name] = choice("correction", options)
                elif re.fullmatch(r"[\W]*(um|uh|so|okay)?[\W]*", text):
                    answers[name] = choice("chitchat", options)
                else:
                    answers[name] = choice("request", options)
            elif kind == "ready":
                answers[name] = choice("dispatch_now", options, self.ready_margin)
            elif kind == "side_effect":
                answers[name] = {"type": "noul", "noul": 0.9 if set(re.findall(r"[a-z]+", text)) & SIDE_EFFECT_WORDS
                                 else 0.1}
            elif kind == "depends":
                answers[name] = choice(self.depends.get(sid, "none"), options)
            elif kind == "corrects":
                earlier = [o.split("_", 1)[1] for o in options if o.startswith("replace_")]
                if text.startswith("never mind"):
                    answers[name] = choice(f"cancel_{earlier[-1]}", options)
                elif text.startswith(("actually", "no wait", "i mean")):
                    answers[name] = choice(f"replace_{earlier[-1]}", options)
                else:
                    answers[name] = choice("none", options)
            elif kind == "finished":
                answers[name] = {"type": "noul", "noul": 0.9}
            elif kind == "at":
                marks = sorted(int(o[1:]) for o in options if o != "none")
                pick = next((k for k in marks[:-1] if after.get(k) in self.starters), marks[-1])
                answers[name] = choice(f"m{pick}", options)
            elif kind == "same_as":
                wanted = set(re.findall(r"[a-z']+", q["instructions"].lower()))
                best = max((o for o in options if o != "new"),
                           key=lambda o: len(set(re.findall(r"[a-z']+", segments[o].lower())) & wanted))
                answers[name] = choice(best, options)
        return {"status": 200, "response": {"answers": answers}, "timing": {"ms": 1.0, "started_at": "t"}}


def stream(texts, decider, delegate_after=None):
    seg = HybridSegmenter(decider, "test")
    for i, text in enumerate(texts, 1):
        seg.update(TranscriptUpdate(i, text, i == len(texts)))
        if delegate_after and delegate_after[0] == i:
            seg.submit_delegate(delegate_after[1])
    return seg


def dispatched(seg):
    return {d["instruction"]: d for d in seg.dispatch_log}


class CorrectionOnlyDecider(FakeDecider):
    """Answers `correction` for every segment's kind, whatever the words, as the decider did on a measured item."""

    def call(self, route, body, label=""):
        record = super().call(route, body, label)
        for name, question in body["questions"].items():
            if name.endswith("_kind"):
                record["response"]["answers"][name] = choice("correction", list(question["criteria"]))
        return record


class Breaks(unittest.TestCase):
    def test_hard_cuts_are_the_baseline_splitters_cuts_plus_i_mean(self):
        text = "Find the capital of Austria, I mean Australia. Then check the weather and also the tide"
        ws = words(text)
        cut_words = sorted(ws[g].text for g in hard_cuts(text, ws))
        self.assertEqual(cut_words, ["and", "i", "then"])

    def test_a_filler_only_piece_joins_the_piece_after_it(self):
        text = "find the weather and um and what time is it"
        pieces = Proposal(text, words(text), 0, set()).pieces
        self.assertEqual([" ".join(w.text for w in words(text)[p.w0:p.w1]) for p in pieces],
                         ["find the weather", "and um and what time is it"])


class Hybrid(unittest.TestCase):
    def test_a_correction_naming_nothing_inside_a_request_is_the_request_it_leads_into(self):
        seg = stream(["That was wrong, now find the sales report for week 45."], CorrectionOnlyDecider())
        self.assertEqual(list(dispatched(seg)), ["That was wrong, now find the sales report for week 45."])

    def test_a_bare_then_splits_two_requests_and_the_first_dispatches_before_the_final(self):
        seg = stream(["find the recipe for lentil soup",
                      "find the recipe for lentil soup then make a shopping",
                      "find the recipe for lentil soup then make a shopping list",
                      "Find the recipe for lentil soup, then make a shopping list."], FakeDecider(starters={"then"}))
        sent = dispatched(seg)
        self.assertEqual(sorted(sent), ["find the recipe for lentil soup", "then make a shopping list."])
        self.assertFalse(sent["find the recipe for lentil soup"]["is_final"])
        self.assertTrue(sent["then make a shopping list."]["is_final"])

    def test_a_repeated_partial_is_a_pause_and_commits_the_request_being_spoken(self):
        seg = stream(["find the recipe for lentil soup then make a shopping list",
                      "find the recipe for lentil soup then make a shopping list",
                      "find the recipe for lentil soup then make a shopping list",
                      "Find the recipe for lentil soup, then make a shopping list."], FakeDecider(starters={"then"}))
        self.assertEqual([(d["instruction"], d["is_final"]) for d in seg.dispatch_log],
                         [("find the recipe for lentil soup", False), ("then make a shopping list", False)])

    def test_a_break_must_hold_on_a_second_decision_point_before_its_request_dispatches(self):
        decider = FakeDecider(starters={"then"})
        seg = HybridSegmenter(decider, "test")
        seg.update(TranscriptUpdate(1, "find the recipe for lentil soup then make", False))
        self.assertEqual(seg.dispatch_log, [])
        seg.update(TranscriptUpdate(2, "find the recipe for lentil soup then make a list", False))
        self.assertEqual([d["instruction"] for d in seg.dispatch_log], ["find the recipe for lentil soup"])

    def test_a_correction_after_a_hard_marker_supersedes_the_held_request_before_it_dispatches(self):
        seg = stream(["look up the height of mount elbert",
                      "look up the height of mount elbert actually mount massive",
                      "Look up the height of Mount Elbert, actually Mount Massive."], FakeDecider())
        tasks = {t["task_id"]: t for t in seg.records()}
        self.assertEqual(tasks["task_1"]["status"], "superseded")
        self.assertEqual(tasks["task_2"]["revises"], "task_1")
        self.assertEqual([d["task_id"] for d in seg.dispatch_log], ["task_2"])
        self.assertIn("massive", seg.dispatch_log[0]["instruction"].lower())

    def test_a_side_effect_request_dispatches_only_on_the_final_transcript(self):
        seg = stream(["email dana that the meeting moved and then check the room",
                      "email dana that the meeting moved and then check the room is free",
                      "email dana that the meeting moved and then check the room is free",
                      "Email Dana that the meeting moved, and then check the room is free."], FakeDecider())
        email = next(d for d in seg.dispatch_log if "Dana" in d["instruction"])
        self.assertTrue(email["is_final"])
        self.assertEqual(len(seg.dispatch_log), 2)

    def test_a_cancellation_is_read_before_the_request_it_cancels_can_dispatch(self):
        seg = stream(["look up the tide at rye and find the ferry timetable",
                      "look up the tide at rye and find the ferry timetable never mind the second one",
                      "Look up the tide at Rye and find the ferry timetable. Never mind the second one."],
                     FakeDecider())
        ferry = next(t for t in seg.records() if "ferry" in t["instruction"])
        self.assertEqual(ferry["status"], "withdrawn")
        self.assertEqual([d["instruction"] for d in seg.dispatch_log], ["look up the tide at rye"])

    def test_one_request_with_no_marker_costs_one_call_on_the_final_transcript(self):
        decider = FakeDecider()
        seg = stream(["email dana", "email dana the q3", "email dana the q3 numbers", "Email Dana the Q3 numbers."],
                     decider)
        self.assertEqual(len(decider.bodies), 1)
        self.assertTrue(decider.bodies[0]["state"]["transcript_is_final"])
        self.assertEqual([d["instruction"] for d in seg.dispatch_log], ["Email Dana the Q3 numbers."])

    def test_a_near_tie_on_readiness_holds_dispatch_until_the_final_transcript(self):
        seg = stream(["find the weather and then the tide", "find the weather and then the tide times",
                      "find the weather and then the tide times today", "Find the weather and then the tide times."],
                     FakeDecider(ready_margin=0.1))
        self.assertTrue(all(d["is_final"] for d in seg.dispatch_log))
        self.assertEqual(len(seg.dispatch_log), 2)

    def test_a_final_transcript_that_shows_a_new_break_is_read_again_before_anything_dispatches(self):
        decider = FakeDecider(starters={"check"})
        seg = stream(["find the moonrise time in perth", "find the moonrise time in perth check the train fare",
                      "Find the moonrise time in Perth, check the train fare."], decider)
        self.assertEqual(len(decider.bodies), 2)
        self.assertEqual([d["instruction"] for d in seg.dispatch_log],
                         ["Find the moonrise time in Perth,", "check the train fare."])
        self.assertTrue(all(d["is_final"] for d in seg.dispatch_log))

    def test_a_follow_up_is_told_that_the_earlier_resolved_task_supplies_its_missing_words(self):
        earlier = ({"ref": "turn_002:task_1", "instruction": "what is the weather in Denver tomorrow?",
                    "side_effect": False, "call": {"tool": "weather",
                                                    "arguments": {"place": "Denver", "when": "tomorrow"}}},)
        decider = FakeDecider()
        seg = HybridSegmenter(decider, "test")
        seg.conversation_mode, seg.prior_resolved_tasks = True, earlier
        seg.update(TranscriptUpdate(1, "And what about Saturday?", True))
        body = decider.bodies[-1]
        self.assertEqual(body["state"]["prior_resolved_tasks"], list(earlier))
        self.assertIn("follow-up", body["questions"]["s1_kind"]["criteria"]["request"])
        self.assertIn("earlier resolved task", body["questions"]["s1_ready"]["criteria"]["dispatch_now"])

    def test_a_waiting_approval_changes_the_kind_question_only_while_one_waits_and_never_the_state(self):
        tools = {"weather": "w", "t3_ask": "a", "none": "n"}
        asked = []
        for waiting in ((), [{"thread": "Add clamp helper", "asks_for": "approval to run a command"}]):
            decider = FakeDecider()
            seg = HybridSegmenter(decider, "test", tools=tools)
            seg.conversation_mode, seg.pending_requests = True, waiting
            seg.update(TranscriptUpdate(1, "I approve.", True))
            asked.append(decider.bodies[-1])
        self.assertNotIn("pending_requests", asked[1]["state"])
        self.assertEqual(asked[0]["state"], asked[1]["state"], "the shared state is what it always was")
        self.assertNotIn("approve", asked[0]["questions"]["s1_kind"]["criteria"]["request"])
        self.assertIn("approve", asked[1]["questions"]["s1_kind"]["criteria"]["request"])

    def test_live_side_effect_criteria_do_not_change_recorded_decision_contract(self):
        live = FakeDecider()
        segmenter = HybridSegmenter(live, "live")
        segmenter.conversation_mode = True
        segmenter.update(TranscriptUpdate(1, "Search my email.", True))
        self.assertIn("Reading or searching", live.bodies[-1]["questions"]["s1_side_effect"]["criteria"]["false"])
        baseline = FakeDecider()
        HybridSegmenter(baseline, "baseline").update(TranscriptUpdate(1, "Search my email.", True))
        self.assertNotIn("Reading or searching", baseline.bodies[-1]["questions"]["s1_side_effect"]["criteria"]["false"])

    def test_the_t3_sentence_is_in_the_kind_question_only_when_the_t3_tools_are_offered(self):
        criteria = {}
        for name, tools in (("off", {"weather": "Weather.", "none": "No tool."}),
                            ("on", {"t3_ask": "Report a thread.", "weather": "Weather.", "none": "No tool."})):
            decider = FakeDecider()
            segmenter = HybridSegmenter(decider, name, tools=tools)
            segmenter.conversation_mode = True
            segmenter.update(TranscriptUpdate(1, "How's the PR thread going?", True))
            criteria[name] = decider.bodies[-1]["questions"]["s1_kind"]["criteria"]
        self.assertNotIn("coding thread", criteria["off"]["request"])
        self.assertIn("coding thread", criteria["on"]["request"])
        self.assertIn("not chitchat", criteria["on"]["chitchat"])

    def test_a_thread_request_with_no_project_is_ready_only_when_the_t3_tools_are_offered(self):
        ready = {}
        for name, tools in (("off", {"weather": "Weather.", "none": "No tool."}),
                            ("on", {"t3_start": "Start a thread.", "weather": "Weather.", "none": "No tool."})):
            decider = FakeDecider()
            segmenter = HybridSegmenter(decider, name, tools=tools)
            segmenter.conversation_mode = True
            segmenter.update(TranscriptUpdate(1, "Could you start a thread for the importer logging?", True))
            ready[name] = decider.bodies[-1]["questions"]["s1_ready"]["criteria"]
        self.assertNotIn("names no project", ready["off"]["dispatch_now"])
        self.assertIn("names no project", ready["on"]["dispatch_now"])
        self.assertNotIn("names no project", ready["on"]["clarify"])

    def test_the_fragments_of_one_thread_request_do_not_wait_on_or_replace_each_other_only_with_the_t3_tools(self):
        criteria = {}
        for name, tools in (("off", {"weather": "Weather.", "none": "No tool."}),
                            ("on", {"t3_start": "Start a thread.", "weather": "Weather.", "none": "No tool."})):
            decider = FakeDecider()
            segmenter = HybridSegmenter(decider, name, tools=tools)
            segmenter.conversation_mode = True
            segmenter.update(TranscriptUpdate(1, "I need the importer cleaned up. And could you start a thread for it?",
                                              True))
            criteria[name] = decider.bodies[-1]["questions"]
        for name, suffix in (("off", "_depends"), ("on", "_depends")):
            question = next(q for key, q in criteria[name].items() if key.endswith(suffix))
            self.assertEqual("only when it needs something" in question["criteria"]["none"], name == "on")
        question = next(q for key, q in criteria["on"].items() if key.endswith("_corrects"))
        self.assertIn("do not change or withdraw", question["criteria"]["none"])

    def test_a_voice_delegate_links_to_its_segment_and_dispatches_once(self):
        seg = stream(["look up tomorrow's weather in seattle", "look up tomorrow's weather in seattle and um",
                      "look up tomorrow's weather in seattle and um what time does the ferry leave",
                      "Look up tomorrow's weather in Seattle, and, um, what time does the ferry leave?"],
                     FakeDecider(), delegate_after=(2, "Look up tomorrow's weather in Seattle"))
        weather = next(t for t in seg.records() if "weather" in t["instruction"])
        self.assertEqual(weather["sources"], ["orchestrator", "voice_delegate"])
        self.assertEqual(sum(d["task_id"] == weather["task_id"] for d in seg.dispatch_log), 1)
        self.assertEqual(len(seg.dispatch_log), 2)

    def test_malformed_answers_dispatch_nothing_and_are_reported(self):
        seg = stream(["find the weather and the tide", "Find the weather and the tide."], FakeDecider(malformed=True))
        self.assertEqual(seg.dispatch_log, [])
        self.assertTrue(seg.points[-1]["problems"])


if __name__ == "__main__":
    unittest.main()
