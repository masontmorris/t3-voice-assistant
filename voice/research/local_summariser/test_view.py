import unittest

from voice.research.local_summariser.view import STEPS, Message, gist_of, messages, replies_view, thread_view
from voice.t3 import local_summary as LS
from voice.t3 import types as T
from voice.t3.eval.summary_eval import cut_at_sentence


def words(text: str) -> int:
    return len(text.split())


LONG_REPLY = " ".join(f"Step {n} of the export passed its check." for n in range(1, 126))
HISTORY = [Message("user", "Split the export into one file per month and open a PR."),
           Message("assistant", "Reading the exporter first."),
           Message("assistant", "Found the writer loop; it holds every month in memory before it writes."),
           Message("user", "Keep the CSV header in each file."),
           Message("assistant", LONG_REPLY)]
STATE = T.ThreadState("t", "p", "Example Split Export Files", T.ThreadActivity.FINISHED,
                      T.RunInfo("r", T.RunStatus.COMPLETED, None), None,
                      T.LastReply(cut_at_sentence(LONG_REPLY), "m", None, "r", ""), 1)


class ThreadViewTest(unittest.TestCase):
    def test_no_budget_is_the_seams_read(self):
        view = thread_view(STATE, HISTORY, None, words)
        self.assertEqual(view["messages"], [{"from": "thread", "text": cut_at_sentence(LONG_REPLY)}])

    def test_a_budget_keeps_the_first_request_then_the_newest_and_cuts_only_the_crossing_message(self):
        view = thread_view(STATE, HISTORY, 1_025, words)
        self.assertEqual(view["messages"], [{"from": "request", "text": HISTORY[0].text},
                                            {"from": "request", "text": HISTORY[3].text},
                                            {"from": "thread", "text": LONG_REPLY}])
        self.assertEqual(view["left_out"], 2)

        small = thread_view(STATE, HISTORY, 600, words)
        self.assertEqual(len(small["messages"]), 1)
        self.assertTrue(LONG_REPLY.startswith(small["messages"][0]["text"]))
        self.assertLessEqual(words(small["messages"][0]["text"]), 600)

    def test_the_replies_view_leaves_out_requests_and_puts_the_newest_earlier_reply_first(self):
        view = replies_view(STATE, HISTORY, 400, words)
        self.assertEqual(view["latest_reply"], cut_at_sentence(LONG_REPLY))
        self.assertEqual(view["earlier_replies"], [HISTORY[2].text, HISTORY[1].text])
        self.assertEqual(view["left_out"], 0)

    def test_the_gist_is_read_from_its_key_and_other_keys_are_ignored(self):
        step = STEPS["S12"]
        self.assertEqual(gist_of('{"gist": "It says the tests pass.", "status": "finished"}', step),
                         ("It says the tests pass.", None))
        self.assertIsNone(gist_of('{"gist": "It says the tests', step)[0])

    def test_the_shipped_local_prompt_is_the_one_measured_as_s11(self):
        last_view = thread_view(STATE, HISTORY, None, words)
        self.assertEqual(LS.messages(STATE, "Is it done?"), messages(STEPS["S11"], STATE, last_view, "Is it done?"))


if __name__ == "__main__":
    unittest.main()
