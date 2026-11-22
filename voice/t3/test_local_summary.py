"""The local summariser's last checks: a gate answer that is not a probability drops the gist, and a gist is only what
the schema allows."""

import unittest

from voice.t3 import local_summary as LS


def gate(answer=None, *, status=200, response=None):
    body = response if response is not None else {"answers": {"unsupported": answer}}
    return {"status": status, "response": body}


class GateAnswerTest(unittest.TestCase):
    def test_only_a_probability_from_a_good_reply_counts(self):
        self.assertEqual(LS.unsupported(gate({"type": "noul", "noul": 0.04})), 0.04)
        malformed = {
            "false": gate({"type": "noul", "noul": False}),
            "true": gate({"type": "noul", "noul": True}),
            "a string": gate({"type": "noul", "noul": "0.01"}),
            "above 1": gate({"type": "noul", "noul": 1.5}),
            "below 0": gate({"type": "noul", "noul": -0.1}),
            "not a number": gate({"type": "noul", "noul": float("nan")}),
            "no noul": gate({"type": "noul"}),
            "no answer": gate(None),
            "answers not a mapping": gate(response={"answers": [0.01]}),
            "not 200": gate({"type": "noul", "noul": 0.01}, status=500),
            "body not JSON": gate(response={"unparsed": "<html>"}),
            "response not a mapping": gate(response=["0.01"]),
        }
        for name, record in malformed.items():
            self.assertIsNone(LS.unsupported(record), name)


class BriefGateTest(unittest.TestCase):
    def test_a_brief_passes_only_when_every_question_answers_under_its_bar(self):
        def answers(**scores):
            return {"status": 200, "response": {"answers": {k: {"type": "noul", "noul": v} for k, v in scores.items()}}}

        self.assertTrue(LS.brief_passes(answers(adds=0.02, drops=0.1, starts=0.05)))
        for name, record in {"it adds": answers(adds=0.4, drops=0.1, starts=0.05),
                             "it drops part of the work": answers(adds=0.02, drops=0.7, starts=0.05),
                             "it asks for a thread": answers(adds=0.02, drops=0.1, starts=0.9),
                             "a question unanswered": answers(adds=0.02, drops=0.1),
                             "a failed call": {"status": 500, "response": {}}}.items():
            self.assertFalse(LS.brief_passes(record), name)

    def test_the_gate_sees_the_request_and_the_brief_and_the_writer_sees_only_the_request(self):
        body = LS.brief_gate("can you start a thread to tidy the docs", "Tidy the docs.")
        self.assertEqual((body["state"]["request"], body["state"]["task"], set(body["questions"])),
                         ("can you start a thread to tidy the docs", "Tidy the docs.", {"adds", "drops", "starts"}))
        self.assertIn('"request": "can you start a thread to tidy the docs"', LS.brief_messages(
            "can you start a thread to tidy the docs")[-1]["content"])


class ReadGistTest(unittest.TestCase):
    def test_a_gist_is_only_what_the_schema_allows(self):
        self.assertEqual(LS.read_gist('{"gist": "It says all  12 tests pass.", "status": "finished"}'),
                         "It says all 12 tests pass.")
        refused = {
            "think tag": '{"gist": "<think>maybe</think>It says the tests pass."}',
            "markdown": '{"gist": "It says **all** tests pass."}',
            "code": '{"gist": "It ran `pytest` and the tests pass."}',
            "a line break": '{"gist": "It says the tests pass.\\nNext it opens a PR."}',
            "over the schema length": '{"gist": "%s"}' % ("It says the tests pass. " * 12),
            "not a string": '{"gist": 12}',
            "not JSON": "It says the tests pass.",
        }
        for name, text in refused.items():
            self.assertIsNone(LS.read_gist(text), name)


if __name__ == "__main__":
    unittest.main()
