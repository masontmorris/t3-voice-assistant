"""The questions the probe asks, shared by the hosted d1.2 and the local d1-omni runs."""
import json

EOT = {"finished": {
    "type": "choice",
    "instructions": "The recording stops at the moment the speaker went quiet. Has the speaker finished their turn, or "
                    "are they pausing in the middle of a thought and about to say more?",
    "criteria": {
        "done": "The speaker has finished and expects the assistant to respond now: a complete request, question or "
                "statement that ends the way a finished sentence ends.",
        "continuing": "The speaker is pausing mid-thought: the sentence is unfinished, trails off, hesitates, or they "
                      "are collecting their thoughts and will say more."}}}

BC = {"floor": {
    "type": "choice",
    "instructions": "The assistant is speaking its reply aloud, and the recording is the first moments of the user "
                    "speaking over it. Is the user just acknowledging, or taking the floor?",
    "criteria": {
        "backchannel": "A short acknowledgement or filler that shows the user is listening and does not want the "
                       "assistant to stop: mm-hm, yeah, okay, right, uh-huh, I see, hmm, uhm.",
        "interrupt": "The user is taking the floor and the assistant should stop talking: a command such as stop or "
                     "wait, a question, or the start of a request, correction or statement."}}}

KIND_CRITERIA = {
    "request": "The speaker asks the assistant to do something or to find something out.",
    "description": "The speaker describes or mentions a task (their own, someone else's, past, or habitual) without "
                   "asking the assistant to do it.",
    "chitchat": "Small talk, filler, thanks, or a reaction, with no task in it.",
    "correction": "The speaker changes, replaces, or withdraws something they asked for in an earlier segment.",
    "topic_change": "Only a transition to a new subject, such as \"anyway\" or \"different thing\", with no task of its "
                    "own."}


def kind(text=None):
    """The segment-kind question. With `text` the instructions quote it, as the live decider does; without, it asks
    about the recording."""
    target = f"segment s1 (\"{text}\")" if text is not None else "the speaker's utterance in the recording"
    return {"s1_kind": {"type": "choice", "instructions": f"Classify {target}.", "criteria": KIND_CRITERIA}}


def p(answer, option):
    return answer["probabilities"][option]


def parse_p(rec, q, option):
    return json.loads(json.dumps(rec["response"]["answers"][q]))["probabilities"][option]
