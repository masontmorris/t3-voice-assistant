"""The slot check, one row per act: a line it passes, and lines it refuses with the reason it gives."""

from __future__ import annotations

import unittest

from voice.t3.acts import RULES, Kind, act, check, forbidden_claims, normalise, recites

T = "Add a clamp helper"
COMMAND = "cd ~/src/app && python -m pytest -q tests/test_parser.py 2>&1 | tail -40 && git status --short"
CUT = ("Should I delete the generated fixtures under the snapshots folder and regenerate them from the"
       "\N{HORIZONTAL ELLIPSIS}")
# kind: (slots, a line that passes, {a line that fails: the problem it must report})
CASES = {
    Kind.CONFIRM_LAUNCH: ({"project": "Voice sandbox"}, "Should I start that in Voice sandbox?",
                          {"I've started it in Voice sandbox.": "not a question",
                           "Should I start it in the server?": "project missing"}),
    Kind.ASK_WHICH: ({"of": "thread", "candidates": [T, "Fix the flaky ASR test"]},
                     f"Do you mean {T} or Fix the flaky ASR test?",
                     {f"Do you mean {T}?": "candidates missing: Fix the flaky ASR test",
                      f"It's {T} or Fix the flaky ASR test.": "not a question"}),
    Kind.NO_PROJECT: ({}, "I can't find any project to use.", {"": "empty"}),
    Kind.LAUNCH_PENDING: ({"project": "Voice sandbox"}, "I've asked T3 to start it.", {"": "empty"}),
    Kind.LAUNCHED: ({"project": "Voice sandbox"}, "It's started in Voice sandbox.",
                    {"It's started.": "project missing"}),
    Kind.LAUNCH_UNCONFIRMED: ({}, "T3 never confirmed the start, so check the app.",
                              {"**Check** the app.": "not plain speech",
                               "No need to think.</think> Check the app.": "not plain speech"}),
    Kind.NO_THREAD: ({"recent": 20}, "I can't find that in your 20 latest threads.",
                     {"One. Two. Three.": "too long"}),
    Kind.CONFIRM_THREAD: ({"thread": T}, f"Is that the {T} thread?",
                          {"Is that the clamp thread?": "thread missing", f"It's the {T} thread.": "not a question",
                           f'Is the thread "{T}" the one you mean?': "quotes around a name"}),
    Kind.CONFIRM_TELL: ({"thread": T, "message": "also add a test"},
                        f"Should I tell {T} to also add a test?",
                        {f"Should I tell {T} to add tests?": "message missing"}),
    Kind.CONFIRM_STOP: ({"thread": T}, f"Want me to stop {T}?",
                        {f"Stop {T}.": "not a question"}),
    Kind.CANCELLED: ({"op": "stop", "thread": T}, f"Okay, I'll leave {T} running.",
                     {}),
    Kind.THREAD_STATUS: ({"thread": T, "status": "working", "summary": "It added the helper."},
                         f"{T} is still working, and it has added the helper.",
                         {"It is still working.": "thread missing"}),
    Kind.TELL_PENDING: ({"thread": T}, f"I've asked T3 to pass that to {T}.",
                        {}),
    Kind.SENT_FOLLOWUP: ({"thread": T}, f"{T} has your message.", {"It has your message.": "thread missing"}),
    Kind.TELL_UNCONFIRMED: ({"thread": T}, f"T3 never confirmed that {T} got it.",
                            {"T3 never confirmed it.": "thread missing"}),
    Kind.HELD_FOLLOWUP: ({"thread": T}, f"{T} is busy, so I'll hold that until its run ends.",
                         {}),
    Kind.DROPPED_FOLLOWUP: ({"thread": T}, f"Okay, I won't give that to {T}.",
                            {}),
    Kind.STOPPING: ({"thread": T}, f"I'm stopping {T} now.", {}),
    Kind.NOTHING_RUNNING: ({"thread": T}, f"{T} isn't doing anything right now.",
                           {}),
    Kind.STOP_UNCONFIRMED: ({"thread": T}, f"I couldn't reach T3 to stop {T}, so use the app.",
                            {}),
    Kind.RUN_UPDATE: ({"thread": T, "status": "running"}, f"{T} is on it now.",
                      {}),
    Kind.REPLIED: ({"thread": T, "reply": "I added clamp."}, f"{T} says it added clamp.",
                   {f"{T} says it added clamp. What's next?": "unrequested question",
                    f"Anything else? {T} says it added clamp.": "unrequested question"}),
    Kind.NEEDS_YOU: ({"thread": T, "need": "command_approval", "prompt": COMMAND},
                     f"{T} needs your approval to run pytest and git status.",
                     {f"{T} wants to run {COMMAND}": "reads code",
                      f"{T} needs your approval to run python -m pytest -q.": "reads code",
                      f"{T} needs your approval to run the parser tests and then...": "not plain speech",
                      "It needs your approval to run pytest.": "thread missing"}),
    Kind.NEED_RESOLVED: ({"thread": T, "status": "resolved"}, f"The request from {T} was answered.",
                         {"That request was answered.": "thread missing"}),
    Kind.DONE: ({"thread": T, "status": "failed"}, f"{T} failed partway through.",
                {}),
    Kind.THREAD_DELETED: ({"thread": T}, f"{T} was deleted.", {"That thread was deleted.": "thread missing"}),
    Kind.LOST_TRACK: ({"thread": T}, f"I lost track of {T}, but it's still in the app.",
                      {"I lost track of it.": "thread missing"}),
    Kind.SAME_TITLE: ({"thread": T, "op": "stop"}, f"More than one thread is called {T}, so I can't tell which to stop.",
                      {"I can't tell which thread to stop.": "thread missing"}),
    Kind.THREAD_GONE: ({"thread": T}, f"I can't see {T} any more.", {"I can't see it.": "thread missing"}),
    Kind.WATCH_QUEUED: ({"thread": T, "following": 16}, f"I'll pick up {T} when one of my 16 threads ends.",
                        {"I'll pick it up soon.": "thread missing"}),
    Kind.UNREACHABLE: ({}, "I can't reach T3 right now.", {"   ": "empty"}),
    Kind.SEAM_DOWN: ({"reason": "access"}, "T3 stopped taking my access, so I've stopped following your threads.",
                     {"See https://example.invalid for why.": "not plain speech"}),
    Kind.LEGACY_UPDATE: ({"text": "Add a clamp helper finished."}, "Your clamp helper thread is done.",
                         {"See `the app`.": "not plain speech"}),
    Kind.CONFIRM_RESPOND: ({"thread": "Add clamp helper", "decision": "approve", "need": "command_approval",
                            "pending": "Run the formatter?"},
                           "Should I approve Add clamp helper running the formatter?",
                           {"Add clamp helper wants to run the formatter.": "not a question",
                            "Should I approve Add clamp helper running `fmt --all`?": "not plain speech"}),
    Kind.RESPONDED: ({"thread": "Add clamp helper", "decision": "approve", "need": "command_approval"},
                     "I approved the command for Add clamp helper.", {"I approved the command.": "missing"}),
    Kind.NOTHING_PENDING: ({"thread": "Add clamp helper"}, "Add clamp helper isn't waiting on any request.", {}),
    Kind.NEEDS_THE_APP: ({"thread": "Add clamp helper", "decision": "approve"},
                         "I can't say what Add clamp helper would be allowed to do, so approve that one in the app.",
                         {"Approve that one in the app.": "missing"}),
    Kind.ASK_QUESTION: ({"thread": "Add clamp helper", "question": "Which database should it use?",
                         "options": ["Postgres", "SQLite", "Keep the current store"]},
                        "Add clamp helper asks which database to use: Postgres, SQLite, or Keep the current store?",
                        {"Add clamp helper asks which database to use: Postgres or SQLite?":
                             "ask_question options missing: Keep the current store",
                         "Add clamp helper asks which database to use: Postgres, SQLite, or keep things as they are?":
                             "ask_question options missing: Keep the current store",
                         "Add clamp helper wants a database, Postgres, SQLite, or Keep the current store.":
                             "not a question",
                         "Which database should it use, Postgres, SQLite, or Keep the current store?":
                             "thread missing",
                         "Add clamp helper asks which database to use: SQLite, Postgres, or Keep the current store?":
                             "ask_question options out of order",
                         "Add clamp helper asks which database to use: Keep the current store, Postgres, or SQLite?":
                             "ask_question options out of order"}),
    Kind.CONFIRM_ANSWER: ({"thread": "Add clamp helper", "questions": ["Which database should it use?", "Name?"],
                           "answers": ["Postgres", "users"]},
                          "Should I answer Add clamp helper with Postgres and users?",
                          {"Should I answer Add clamp helper with Postgres?": "confirm_answer answers missing: users",
                           "I'll answer Add clamp helper with Postgres and users.": "not a question",
                           "Should I answer with Postgres and users?": "thread missing"}),
    Kind.ANSWERED: ({"thread": "Add clamp helper", "answers": ["Postgres"]},
                    "Add clamp helper has the answer Postgres.",
                    {"Add clamp helper has your answer.": "answers missing: Postgres",
                     "Add clamp helper has the answer Postgres. Anything else?": "unrequested question"}),
    Kind.ANSWER_IN_APP: ({"thread": "Add clamp helper"},
                         "Add clamp helper has a question you'll need to answer in the app.",
                         {"You'll need to answer that in the app.": "thread missing"}),
    Kind.UNANSWERED: ({}, "I couldn't give you an answer just now.",
                      {"Could you say that again?": "unrequested question"}),
    Kind.CORRECTION: ({"thread": "Add clamp helper", "status": "working"},
                      "Sorry, I got that wrong. Add clamp helper is still working.",
                      {"Add clamp helper is still working. Want me to check?": "unrequested question",
                       "Sorry, I got that wrong.": "missing"}),
    Kind.REFUSED: ({"op": "start", "code": "refused", "reason": "Model is off."},
                   "T3 wouldn't start it because the model is off.",
                   {}),
}


CLIPPED = "So, uh, I want to get some work going on, uhm, a small project. A tool that con…"


class NamesInTheForm(unittest.TestCase):
    def test_a_name_said_whole_is_not_judged_on_its_own_punctuation(self):
        said = act(Kind.NEEDS_THE_APP, thread=CLIPPED, decision="approve")
        line = ("I can't tell what So, uh, I want to get some work going on. Uhm, a small project, a tool that con... "
                "would be allowed to do, so you'll need to approve that one in the app.")
        self.assertEqual(check(line, [said]), [])

    def test_the_line_around_a_name_is_still_judged(self):
        said = act(Kind.CONFIRM_THREAD, thread=CLIPPED)
        self.assertEqual(check(f"Do you mean the thread called {CLIPPED}?", [said]), [])
        self.assertIn("not plain speech", check(f"Do you mean the thread called {CLIPPED} or `not`?", [said]))
        self.assertIn("too long", check(f"Do you mean {CLIPPED}? Is it? Really? Sure.", [said]))
        self.assertIn(f"confirm_thread thread missing: {CLIPPED}",
                      check("Do you mean the thread called So, uh, I want to get some work going on?", [said]))

    def test_only_the_names_own_characters_are_masked(self):
        said = act(Kind.CONFIRM_THREAD, thread="Alpha Beta")
        self.assertIn("not plain speech", check("Do you mean the thread called Alpha `Beta?", [said]))
        self.assertIn("not plain speech", check("Do you mean the thread called Alpha ```Beta?", [said]))
        self.assertEqual(check("Do you mean the thread called Alpha Beta?", [said]), [])
        punctuated = act(Kind.CONFIRM_THREAD, thread="Alpha, then Beta. Gamma…")
        self.assertEqual(check("Do you mean the thread called Alpha, then Beta. Gamma...?", [punctuated]), [])
        self.assertIn("not plain speech", check("Do you mean the thread called Alpha, then `Beta. Gamma?", [punctuated]))

    def test_a_thread_with_no_title_is_named_by_its_project(self):
        said = act(Kind.NEEDS_YOU, begun_in="Voice sandbox", need="command_approval")
        self.assertEqual(check("The thread you started in Voice sandbox needs your approval to run a command.", [said]), [])
        self.assertIn("needs_you begun_in missing: Voice sandbox",
                      check("The thread you started needs your approval to run a command.", [said]))


class SlotRules(unittest.TestCase):
    def test_unanswered_forbids_every_action_except_confirmed_outcomes(self):
        forbidden = {"started", "sent", "stopped", "answered", "finished"}
        self.assertEqual(forbidden_claims([act(Kind.UNANSWERED)]), forbidden)
        self.assertEqual(forbidden_claims([act(Kind.UNANSWERED, outcomes=["started", "stop_requested"])]),
                         forbidden - {"started"})

    def test_every_act_has_a_row(self):
        self.assertEqual(set(CASES), set(Kind))

    def test_each_act_passes_its_line_and_refuses_the_rest(self):
        for kind, (slots, good, bad) in CASES.items():
            acts = [act(kind, **slots)]
            with self.subTest(kind=kind.value, line=good):
                self.assertEqual(check(good, acts), [])
            for line, problem in bad.items():
                with self.subTest(kind=kind.value, line=line):
                    found = check(line, acts)
                    self.assertTrue(any(problem in p for p in found), found)

    def test_names_survive_case_punctuation_and_person(self):
        acts = [act(Kind.CONFIRM_TELL, thread="Fix: the *flaky* test", message="check my PR")]
        self.assertEqual(check("Should I tell fix the flaky test to check my PR?", acts), [])
        self.assertIn("confirm_tell message missing: check my PR",
                      check("Should I tell fix the flaky test to check your PR?", acts),
                      "a person's own words are said as they were said, never turned into the voice's person")
        status = act(Kind.DONE, thread="Fix the parser?", status="finished")
        self.assertEqual(check("Fix the Parser? is finished.", [status]), [])
        self.assertEqual(normalise("It's done, isn't it?"), "it s done is not it")

    def test_a_status_another_act_states_is_not_forbidden(self):
        done_and_sent = [act(Kind.DONE, thread=T, status="completed"), act(Kind.SENT_FOLLOWUP, thread=T)]
        self.assertEqual(forbidden_claims(done_and_sent), {"working"})
        running = [act(Kind.RUN_UPDATE, thread=T, status="running"), act(Kind.HELD_FOLLOWUP, thread=T)]
        self.assertEqual(forbidden_claims(running), {"finished", "sent", "stopped"})

    def test_meaning_is_left_to_the_decider(self):
        self.assertEqual(check(f"I've stopped {T}, right?", [act(Kind.CONFIRM_STOP, thread=T)]), [],
                         "a status claim is the decider's question (voice/t3/test_verify.py), not a word list's")

    def test_a_slot_no_rule_names_is_refused_at_build_time(self):
        with self.assertRaises(ValueError):
            act(Kind.CONFIRM_STOP, thread=T, message="nope")
        self.assertTrue(all(set(rule.named) <= set(rule.slots) for rule in RULES.values()))

    def test_a_need_is_described_in_words_and_its_prompt_is_never_read_out(self):
        for need, prompt in (("command_approval", COMMAND), ("question", CUT), ("file_change", "Apply 3 edits?"),
                             ("link", "Open the sign-in link")):
            with self.subTest(need):
                needs_you = act(Kind.NEEDS_YOU, thread=T, need=need, prompt=prompt)
                self.assertEqual(check(f"{T} needs you in the app.", [needs_you]), [], "the prompt is not a slot")
        cut = act(Kind.NEEDS_YOU, thread=T, need="question", prompt=CUT)
        self.assertIn("not plain speech", check(f"{T} asks: {CUT}", [cut]), "a cut prompt is never read out")
        status = act(Kind.THREAD_STATUS, thread=T, status="waiting_on_you", need="command_approval", pending=COMMAND)
        self.assertIn("reads code", check(f"{T} is waiting for you to approve a command: {COMMAND}", [status]))

    def test_more_than_three_candidates_are_cut_to_three(self):
        said = act(Kind.ASK_WHICH, of="thread", candidates=["a", "b", "c", "d"])
        self.assertEqual(said.slots["candidates"], ["a", "b", "c"])


class Recites(unittest.TestCase):
    """The call's reply is accepted only when it says the checked draft's words and no others."""

    LINE = "I couldn't find that, but I can start a thread in Voice sandbox."

    def test_the_same_words_pass_whatever_the_case_punctuation_markdown_or_contraction(self):
        for text in (self.LINE, self.LINE.lower(), "**" + self.LINE + "**", '"' + self.LINE + '"',
                     self.LINE.replace("couldn't", "could not"), self.LINE.replace(".", " ."),
                     self.LINE.replace("'", "’")):
            with self.subTest(text=text):
                self.assertTrue(recites(text, self.LINE))

    def test_anything_else_fails(self):
        for text in (f"Sure! {self.LINE}", f"Here's the draft: {self.LINE}", f"{self.LINE} Let's get this started!",
                     "I couldn't find that, but I can start a thread in", "", self.LINE.replace("Voice sandbox", "Voice server"),
                     "You couldn't find that, but you can start a thread in Voice sandbox."):
            with self.subTest(text=text):
                self.assertFalse(recites(text, self.LINE))

    def test_numbers_keep_their_separators_and_signs(self):
        for text, line in (("It found 1,500 files.", "It found 1.500 files."), ("It found 1500 files.", "It found 1,500 files."),
                           ("Pay $5.", "Pay 5."), ("It is 50 done.", "It is 50% done."), ("At 10:30.", "At 10.30.")):
            with self.subTest(text=text):
                self.assertFalse(recites(text, line))
        self.assertTrue(recites("It found 1,500 files.", "it found 1,500 files"))

    def test_a_word_is_judged_only_once_it_is_complete(self):
        """A reply cut after "don'" is on its way to "don't", and one cut after "1," to "1,500"."""
        for text, line in (("I don'", "I don't know."), ("I don", "I don't know."), ("I got 1,", "I got 1,500 files."),
                           ("I got 1", "I got 1,500 files."), ("I got 1,50", "I got 1,500 files.")):
            with self.subTest(text=text):
                self.assertTrue(recites(text, line, arriving=True))
        for text, line in (("I got 1.5 ", "I got 1,500 files."), ("I got 1,5 ", "I got 1,500 files."),
                           ("I got 2 ", "I got 1,500 files.")):
            with self.subTest(text=text):
                self.assertFalse(recites(text, line, arriving=True))
        self.assertTrue(recites("I do not know", "I don't know.", arriving=True), "a spelled-out contraction is the same")

    def test_who_does_what_is_never_flipped(self):
        self.assertNotEqual(normalise("I asked T3."), normalise("You asked T3."))
        self.assertFalse(recites("You asked T3 to stop it.", "I asked T3 to stop it."))

    def test_a_start_may_still_be_arriving(self):
        """A last word with no space after it may be half said, so it is not read until the next character: stopping on
        it would cut a "couldn" that is on its way to "couldn't"."""
        for text in ("", "I could", "I couldn't find", "I couldn't fi", "I couldn", "Here", "I couldn't find that, so"):
            with self.subTest(text=text):
                self.assertTrue(recites(text, self.LINE, arriving=True))
        for text in ("Here ", "I couldn't find that, so ", "I can ", "I couldn't find that, but I can start a thread in Voice sandbox. Ok "):
            with self.subTest(text=text):
                self.assertFalse(recites(text, self.LINE, arriving=True))


if __name__ == "__main__":
    unittest.main()
