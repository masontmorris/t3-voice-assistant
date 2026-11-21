"""Made-up replies to a pending question that the reply reader must tell apart: a yes the transcript marked as a
question, a question that starts with a yes, and a no that takes a request back against one that wants another thread.

`SEEN` was used to choose the wording of the reader's questions and their bars. Its non-committal replies were added
after "hmm, maybe?" read as a yes (0.66) on a held-out set that had been scored once, so they are tuning cases. `HELD_OUT` was written before any
wording ran on it and scored once, after the bars were set. Each case is (act the voice asked, reply, group, whether the
reply is an unconditional yes, whether a no takes the request back or None when it is not a no to a thread question).
"""

from voice.t3.acts import Kind, act

LAUNCH = act(Kind.CONFIRM_LAUNCH, project="Voice sandbox")
STOP = act(Kind.CONFIRM_STOP, thread="Add a clamp helper")
TELL = act(Kind.CONFIRM_TELL, thread="Add a clamp helper", message="also add a test")
THREAD = act(Kind.CONFIRM_THREAD, thread="Tidy the Save Slots")
YES_GROUPS = {"yes_marked": True, "hesitant_yes": True, "question_after_yes": False, "refusal": False,
              "off_topic": False, "condition": False, "non_committal": False}


TAKES_BACK = {"withdraw": True, "wants_other": False}


def cases(spec):
    rows = []
    for said, groups in spec:
        for group, replies in groups.items():
            for reply in replies:
                rows.append((said, reply, group, YES_GROUPS.get(group, False), TAKES_BACK.get(group)))
    return rows


SEEN = cases([
    (LAUNCH, {
        "yes_marked": ["Yeah?", "Sure?", "Okay?", "Yep?", "Yes?", "Alright?", "Mm-hmm?", "Go ahead?", "Sounds good?",
                       "Uh-huh?", "Yeah, okay?", "Please?", "Do it?"],
        "hesitant_yes": ["Um, yeah, I guess.", "I suppose so.", "Yeah, I think so.", "Uh, sure, why not.", "Hmm, okay.",
                         "Yeah, that's fine."],
        "question_after_yes": ["Yeah, but which project?", "Yes, but what will it do exactly?",
                               "Okay, how long will that take?", "Sure, but is it going to touch my files?",
                               "Yeah, which one is that?", "Yeah? What's it going to start?", "Okay but where?"],
        "refusal": ["No.", "Nah.", "Not yet.", "No, thanks.", "Don't.", "Never mind.", "Actually, no.", "Hmm, no."],
        "off_topic": ["What's the weather tomorrow?", "Hello?", "Can you hear me?", "What time is it?", "Thanks."],
        "condition": ["Yeah, if it's quick.", "Yes, but only after the tests pass.", "Sure, in the other project."]}),
    (STOP, {"yes_marked": ["Yeah?", "Okay?", "Sure?"], "question_after_yes": ["Yeah, but why?", "Okay, which one is that?"],
            "refusal": ["No.", "Not yet."]}),
    (TELL, {"yes_marked": ["Yeah?", "Yep?", "Go ahead?"], "question_after_yes": ["Yeah, but what does it say?"],
            "refusal": ["No.", "Nah."]}),
    (LAUNCH, {"non_committal": ["Hmm, maybe?", "I'm not sure.", "Maybe later.", "I don't know.", "Possibly?",
                                "Let me think.", "Perhaps?"]}),
    (THREAD, {"non_committal": ["Hmm, maybe?", "I'm not sure."]}),
    (THREAD, {"yes_marked": ["Yeah?", "Yep, that one?", "Right?"], "question_after_yes": ["Is that the one that failed?"],
              "refusal": ["No, not that one."],
              "withdraw": ["Never mind.", "Forget it.", "Drop it.", "Cancel that.", "Actually, skip it.", "Nothing, sorry.",
                           "Don't worry about it.", "Never mind that. What's two plus two?",
                           "Actually, never mind. Tell me about octopuses.", "No, forget it, it doesn't matter.",
                           "Let's not bother."],
              "wants_other": ["No.", "No, not that one.", "Nope, the other one.", "No, the jump timing one.",
                              "That's not it.", "Wrong one.", "No, I meant the menu one.", "No, a different thread."]}),
])

HELD_OUT = cases([
    (LAUNCH, {
        "yes_marked": ["Yeah, go ahead?", "Okay, sure?", "Yup?", "Yes, please?", "Sure thing?", "Absolutely?", "Fine?",
                       "Go on?", "Mhm?", "Okay then?", "Yeah, do that?", "Let's do it?"],
        "hesitant_yes": ["Er, I think that works.", "Yeah... okay, sure.", "Well, why not.", "Okay, I guess.",
                         "Hmm, yeah, go for it.", "I suppose that's fine."],
        "question_after_yes": ["Yeah, but what does that actually do?", "Okay, and which project is it going into?",
                               "Sure, how many files will it change?", "Yes, though, should it be in the docs project?",
                               "Yeah, what name will it get?", "Okay, wait, is that the one from earlier?",
                               "Sure, remind me what it's for?", "Yep, but does that cost anything?"],
        "refusal": ["No way.", "Not now.", "Let's not.", "No, forget it.", "I changed my mind.", "Skip it.", "Hold off.",
                    "Cancel that."],
        "off_topic": ["How's the weather?", "What did you just say?", "Hold on, someone's at the door.", "Thanks, bye.",
                      "Are you there?"],
        "condition": ["Yes, once the build is green.", "Sure, but in the server project.",
                      "Yeah, as long as it's quick."]}),
    (STOP, {"yes_marked": ["Yep?", "Go ahead?"], "question_after_yes": ["Yeah, but is it still running?"],
            "refusal": ["Leave it."]}),
    (TELL, {"yes_marked": ["Sure?", "Okay, send it?"], "question_after_yes": ["Yes, but can I change the wording?"],
            "refusal": ["Don't send it."]}),
    (LAUNCH, {"non_committal": ["Um, maybe not?", "Not sure, maybe.", "Hmm, I'll think about it.", "Could be?",
                                "We'll see.", "Maybe, maybe not.", "That depends.", "I'm undecided."]}),
    (STOP, {"non_committal": ["Hmm, maybe?", "I'm not sure."]}),
    (THREAD, {"yes_marked": ["Yeah, that one?", "Mm-hmm?"], "question_after_yes": ["Which thread is that again?"],
              "refusal": ["No, the other one."],
              "withdraw": ["Never mind, it's not important.", "Scratch that.", "Leave it.", "Forget I asked.",
                           "Oh, never mind, I'll look myself.", "Okay, never mind. What time is it?", "Skip that."],
              "wants_other": ["No, not the save slots one.", "Not that one, the audio one.", "No, the other thread.",
                              "That's the wrong one.", "No, I meant the one about shaders.", "Nah, a different one."]}),
])


# Shapes a review found uncovered: the reply echoes the question, agrees to something else than what is pending, reverses
# itself, or is cut off by end-of-turn detection mid-word or before a condition. `FRESH_SEEN` was written together with
# `FRESH_HELD_OUT` and is the only one a wording or bar may be chosen on; `FRESH_HELD_OUT` was written before either was
# run and is scored once, on the final wording.
APPROVE = act(Kind.CONFIRM_RESPOND, thread="Add a clamp helper", decision="approve", need="command_approval",
              summary="it wants to list the files in the folder")
DENY = act(Kind.CONFIRM_RESPOND, thread="Add a clamp helper", decision="deny", need="command_approval")
ANSWER = act(Kind.CONFIRM_ANSWER, thread="Add a clamp helper", questions=["Which database?"], answers=["Postgres"])
SHAPES = {"echo": False, "other_question": False, "reversal_to_no": False, "reversal_to_yes": True,
          "truncated_yes": True, "truncated_condition": False, "truncated_refusal": False, "cut_word": False}
YES_GROUPS.update(SHAPES)

FRESH_SEEN = cases([
    (LAUNCH, {
        "echo": ["Start a new coding thread in Voice sandbox?", "A new coding thread in Voice sandbox?",
                 "Should you start a new thread?", "Start it in Voice sandbox, you mean?"],
        "other_question": ["Yeah, I'm doing fine.", "Yes, I can hear you.", "Sure, it's nice weather.",
                           "Yeah, that's what he said."],
        "reversal_to_no": ["Yeah... actually wait.", "Yes, no, hold on.", "Sure, actually never mind.", "Okay, wait, no."],
        "reversal_to_yes": ["No, wait, yes, go ahead.", "Actually, yes, do it.", "Hmm, no... okay, yes."],
        "truncated_yes": ["Yeah, go", "Sure, do", "Yes, please", "Okay, go on"],
        "truncated_condition": ["Yeah, but", "Yes, and", "Sure, if", "Okay, only", "Yes, unless"],
        "truncated_refusal": ["No, don't sta", "Not ye", "No, plea", "Don't"],
        "cut_word": ["Ye", "Oka", "Su"]}),
    (STOP, {"echo": ["Stop the clamp helper?"], "other_question": ["Yes, it's running fine."],
            "truncated_condition": ["Yes, but"], "truncated_refusal": ["No, leave"], "truncated_yes": ["Yes, stop"]}),
    (TELL, {"echo": ["Send also add a test to it?"], "reversal_to_no": ["Yeah, send it... no wait."],
            "truncated_yes": ["Yes, send"], "truncated_condition": ["Yes, but"]}),
    (APPROVE, {"echo": ["Approve the request?"], "other_question": ["Yes, it's running a command."],
               "truncated_condition": ["Yes, but only"], "truncated_yes": ["Yes, approve"],
               "reversal_to_no": ["Yeah... actually no."], "truncated_refusal": ["No, don't appr"]}),
    (DENY, {"echo": ["Deny the request?"], "truncated_yes": ["Yes, deny"], "truncated_condition": ["Yes, but"],
            "reversal_to_no": ["Yes, deny it... no, wait."]}),
    (ANSWER, {"echo": ["Give it the answer Postgres?"], "truncated_yes": ["Yes, send"],
              "truncated_condition": ["Yes, but"], "other_question": ["Yeah, I like Postgres."]}),
    (THREAD, {"echo": ["The clamp helper thread?", "Add a clamp helper?"], "other_question": ["Yes, it's a good thread."],
              "reversal_to_no": ["Yeah... no, wait."], "truncated_condition": ["Yes, but"],
              "truncated_refusal": ["No, not tha"], "truncated_yes": ["Yes, that one"]}),
])

FRESH_HELD_OUT = cases([
    (LAUNCH, {
        "echo": ["Open a coding thread in the sandbox?", "Voice sandbox, a new thread?", "You want to start it now?",
                 "A thread, in Voice sandbox, new?"],
        "other_question": ["Yes, my day was good.", "Sure, the music is loud.", "Yeah, it's raining here.",
                           "Okay, I see the screen."],
        "reversal_to_no": ["Yes, wait, actually don't.", "Okay... hmm, no, stop.", "Sure, wait, hold off.",
                           "Yeah, go... no, cancel."],
        "reversal_to_yes": ["Hmm, no. Well, yes, fine.", "Wait, yes, start it.", "No, I mean yes, go ahead."],
        "truncated_yes": ["Yeah, start", "Sure, go", "Okay, yes", "Yes, go"],
        "truncated_condition": ["Yes, but only", "Sure, as long", "Okay, provided", "Yeah, except", "Yes, though"],
        "truncated_refusal": ["No, not", "Nope, don't", "Please don't sta", "No, hold"],
        "cut_word": ["Yea", "Sur", "Ok"]}),
    (STOP, {"echo": ["Stop it, you say?"], "other_question": ["Yes, the build is green."],
            "truncated_condition": ["Sure, once"], "truncated_refusal": ["No, let it"], "truncated_yes": ["Yeah, stop"]}),
    (TELL, {"echo": ["Tell it to add a test?"], "reversal_to_no": ["Yes, send it, actually no."],
            "truncated_yes": ["Sure, send"], "truncated_condition": ["Yeah, and"]}),
    (APPROVE, {"echo": ["Let it list the files?"], "other_question": ["Sure, the files are in the folder."],
               "truncated_condition": ["Okay, as long"], "truncated_yes": ["Yeah, allow"],
               "reversal_to_no": ["Yes, no, wait, deny."], "truncated_refusal": ["No, don't all"]}),
    (DENY, {"echo": ["Say no to it?"], "truncated_yes": ["Yeah, say no"], "truncated_condition": ["Yes, but"],
            "reversal_to_no": ["Yeah, deny... actually allow it."]}),
    (ANSWER, {"echo": ["Postgres, you mean?"], "truncated_yes": ["Sure, give"], "truncated_condition": ["Yeah, but"],
              "other_question": ["Yes, Postgres is fast."]}),
    (THREAD, {"echo": ["That clamp thread?"], "other_question": ["Yeah, threads are useful."],
              "reversal_to_no": ["Yes, hmm, no."], "truncated_condition": ["Yeah, though"],
              "truncated_refusal": ["No, that's"], "truncated_yes": ["Yes, that one"]}),
])
