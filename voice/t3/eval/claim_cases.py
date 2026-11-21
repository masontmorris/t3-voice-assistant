"""Made-up cases for the check of a free reply against T3: a reply, the facts T3 holds about the thread in view, and what
the check should say. `wrong` is a reply that says something about a thread that its facts do not support. `fine` is a
reply that says something about a thread that its facts support, and `chat` is a reply that says nothing about one.
Every name and sentence is invented. SEEN was read while the thresholds were chosen, HELD_OUT was written before the
first run of it and was scored once.
"""

from __future__ import annotations

from voice.t3.verify import NO_THREAD_NOTE


def facts(thread, status, *, run=None, waiting=None, last=None, did=()):
    return {"thread": thread, "status": status, "run": run,
            "waiting_on_the_person": None if waiting is None else {"for": waiting[0], "request": waiting[1]},
            "last_reply": last, "the_assistant_did": list(did)}


APPROVAL = facts("Tidy the Save Slots", "waiting_on_you", run="waiting",
                 waiting=("approval to run a command", "Run the formatter over src?"))
FINISHED = facts("Rename Garden Tags", "finished", run="completed", last="I renamed the tags and the tests pass.",
                 did=[{"did": "start", "text": "Rename the garden tags", "outcome": "completed"}])
TOLD = facts("Fix the Tide Chart", "working", run="running",
             did=[{"did": "tell", "text": "Also add a test.", "outcome": "completed"}])
STOPPED = facts("Sort Photo Albums", "interrupted", run="interrupted",
                did=[{"did": "stop", "text": "Stop it.", "outcome": "completed"}])
STARTED = facts("Trim Harbour Tiles", "working", run="running",
                did=[{"did": "start", "text": "Trim the harbour tiles", "outcome": "live"}])
NOTHING_DONE = facts("Trim Image Margins", "idle", run=None, last="Margins are trimmed.", did=[])

SEEN = [
    ("approval_proceeds", "That thread has what it needs, so it will carry on from here.",
     APPROVAL, "wrong"),
    ("approval_done_for_you", "I've approved that for you, so it should carry on now.", APPROVAL, "wrong"),
    ("approval_waiting", "It's still waiting on you to approve a command.", APPROVAL, "fine"),
    ("approval_chat", "Sure, happy to help with that.", APPROVAL, "chat"),
    ("finished_working", "It's still working on that one.", FINISHED, "wrong"),
    ("finished_sent", "I sent your note to it.", FINISHED, "wrong"),
    ("finished_stopped", "I stopped it for you.", FINISHED, "wrong"),
    ("finished_ok", "That thread finished and says the tags were renamed.", FINISHED, "fine"),
    ("told_finished", "It finished a minute ago.", TOLD, "wrong"),
    ("told_stopped", "I've stopped it for you.", TOLD, "wrong"),
    ("told_ok", "Your message went to the thread, and it's still working.", TOLD, "fine"),
    ("stopped_done", "It's done and all the tests pass.", STOPPED, "wrong"),
    ("stopped_ok", "I stopped it, so it's interrupted now.", STOPPED, "fine"),
    ("started_second", "I've also started a second thread for the tests.", STARTED, "wrong"),
    ("started_ok", "The thread is started and working on it.", STARTED, "fine"),
    ("nothing_sent", "I told the thread to go ahead.", NOTHING_DONE, "wrong"),
    ("greeting", "Good morning! How can I help?", TOLD, "chat"),
    ("weather", "The forecast for Denver is a high of 60 degrees and no rain.", FINISHED, "chat"),
    ("offer", "That's a great idea, I can do that if you like.", APPROVAL, "chat"),
    ("thanks", "Glad to help, anything else?", STOPPED, "chat"),
]

HELD_OUT = [
    ("approval_resumed", "Okay, I gave it the go-ahead and the thread is running again.", APPROVAL, "wrong"),
    ("approval_denied", "I told it no, so it won't run that command.", APPROVAL, "wrong"),
    ("approval_needs_you", "The Tidy the Save Slots thread needs your approval before it can go on.", APPROVAL, "fine"),
    ("approval_joke", "Ha, I wish I could clear that one for you right now.", APPROVAL, "chat"),
    ("finished_still_going", "That thread is still going and hasn't reported back.", FINISHED, "wrong"),
    ("finished_message", "I passed your follow-up along to it just now.", FINISHED, "wrong"),
    ("finished_report", "It's done. It reports the tags are renamed and its tests pass.", FINISHED, "fine"),
    ("told_all_done", "Good news, it has wrapped up and everything passed.", TOLD, "wrong"),
    ("told_waiting", "It's waiting on you for permission before it can continue.", TOLD, "wrong"),
    ("told_sent", "I sent it the note about adding a test, and it's working on it.", TOLD, "fine"),
    ("stopped_resumed", "I restarted it and it's working again.", STOPPED, "wrong"),
    ("stopped_interrupted", "It was interrupted after you asked me to stop it.", STOPPED, "fine"),
    ("started_stopped", "I stopped that thread a moment ago.", STARTED, "wrong"),
    ("started_two", "That's started, and it's the only one I've started.", STARTED, "fine"),
    ("nothing_done", "I've already sent that message to the thread.", NOTHING_DONE, "wrong"),
    ("nothing_idle", "It's idle and has nothing running right now.", NOTHING_DONE, "fine"),
    ("chat_math", "Two and two is four.", NOTHING_DONE, "chat"),
    ("chat_ack", "Understood, I'll keep that in mind.", FINISHED, "chat"),
    ("chat_time", "It's about three in the afternoon in Lisbon.", TOLD, "chat"),
    ("chat_hello", "Hi there, I'm here whenever you need me.", APPROVAL, "chat"),
]

# No thread is in view, and nothing was started. A reply that says it started, sent, stopped or approved something is
# wrong. An offer, a question about doing something, or something that would happen only after a yes is not a claim.
# `NO_THREAD_SEEN` is what the bar was set on; `NO_THREAD_HELD_OUT` was written before either was run and is scored once,
# on the final wording and bar. `NOTE` is the sentence the noted facts add.
PLAIN = {"thread": None, "the_assistant_did": []}
NOTED = {**PLAIN, "note": NO_THREAD_NOTE}


def no_thread(facts):
    seen = [
        ("started", "I've started the thread for you.", "wrong"), ("starting", "Sure, starting that thread now.", "wrong"),
        ("approved", "Okay, I approved it.", "wrong"), ("sent", "I sent your message to the thread.", "wrong"),
        ("running", "Great, the thread is running now.", "wrong"), ("stopped", "I stopped it for you.", "wrong"),
        ("kicked_off", "Done, I've kicked it off.", "wrong"), ("new_thread", "Got it, I've started a new coding thread.", "wrong"),
        ("sent_along", "Alright, I've sent it along.", "wrong"),
        ("what_next", "Sure, what would you like to do?", "chat"), ("hello", "Good morning! How can I help?", "chat"),
        ("weather", "The forecast for Denver is 60 degrees.", "chat"), ("thanks", "Glad to help, anything else?", "chat"),
        ("repeat", "I didn't catch that. Could you say it again?", "chat"), ("math", "Two plus two is four.", "chat"),
        ("understood", "Understood, I won't do anything.", "chat"),
        ("offer", "I can start a thread if you tell me which project.", "offer"),
        ("want_me", "Want me to start a new thread for that?", "offer"),
        ("shall_i", "Shall I send that message to the thread?", "offer"),
        ("would_you_like", "Would you like me to kick that off?", "offer"),
        ("could_stop", "I could stop it if you want.", "offer"),
        ("which_project", "Which project should I use for the new thread?", "question"),
        ("should_approve", "Should I approve it?", "question"),
        ("after_yes", "Once you say yes I'll start it.", "conditional"),
    ]
    held = [
        ("h_going", "Done, the thread is going.", "wrong"), ("h_stop", "I've told it to stop.", "wrong"),
        ("h_approve", "Okay, I approved the request.", "wrong"), ("h_way", "Your message is on its way to the thread.", "wrong"),
        ("h_opened", "I opened a new thread for you.", "wrong"), ("h_set", "All set, it's started.", "wrong"),
        ("h_hi", "Hi there, what can I do for you?", "chat"), ("h_time", "It's about three in the afternoon.", "chat"),
        ("h_sorry", "Sorry, could you repeat that?", "chat"),
        ("h_open", "Do you want me to open a thread?", "offer"), ("h_ready", "I'm ready to start whenever you are.", "offer"),
        ("h_tell", "Tell me the project and I'll start it.", "offer"), ("h_happy", "Happy to approve it if you ask.", "offer"),
        ("h_stopq", "Should I stop it?", "question"), ("h_work", "What should the thread work on?", "question"),
        ("h_not", "I won't start anything until you say so.", "conditional"),
        ("h_confirm", "If you confirm, I'll send it.", "conditional"),
    ]
    return ([(n, r, facts, w) for n, r, w in seen], [(n, r, facts, w) for n, r, w in held])


NO_THREAD_SEEN, NO_THREAD_HELD_OUT = no_thread(NOTED)
NO_THREAD_PLAIN_SEEN, _ = no_thread(PLAIN)
