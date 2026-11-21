"""Made-up acts for the line eval. Every title, project, summary and message here is invented, so the cases carry no
call content and can sit in the repo. They cover the acts the 2026-11-20 call failed on, each in several shapes:
a thread to confirm, which of several candidates, and a thread's status with a summary the line must pass on."""

from voice.t3.acts import NOTHING_NEW, Kind, act

CONFIRM_THREAD = [
    act(Kind.CONFIRM_THREAD, thread="Tidy the tide table export"),
    act(Kind.CONFIRM_THREAD, thread="Move the kiln log to its own page"),
    act(Kind.CONFIRM_THREAD, thread="Lantern app: fix the dimmer slider"),
    act(Kind.CONFIRM_THREAD, thread="Rename the orchard sync job"),
]
ASK_WHICH = [
    act(Kind.ASK_WHICH, of="thread", candidates=["Trim the harbour map tiles", "Trim the harbour map labels",
                                                   "Add a harbour map legend"]),
    act(Kind.ASK_WHICH, of="thread", candidates=["Speed up the loom importer", "Fix the loom importer crash"]),
    act(Kind.ASK_WHICH, of="thread", candidates=["Sort the seed catalogue", "Sort the seed packets by colour",
                                                   "Show seed packets in a grid"]),
    act(Kind.ASK_WHICH, of="project", candidates=["Moss Notes", "Kite Planner"]),
    act(Kind.ASK_WHICH, of="project", candidates=["Pebble Garden", "Ferry Times", "Quilt Maker"]),
]
SUMMARY = [
    act(Kind.THREAD_STATUS, thread="Tidy the tide table export", status="cancelled",
        summary="It says it began reading the tide tables before it was stopped.", asked="how is it going"),
    act(Kind.THREAD_STATUS, thread="Move the kiln log to its own page", status="completed",
        summary="It reports that the kiln log now has a page and the links work.", asked="is it done"),
    act(Kind.THREAD_STATUS, thread="Lantern app: fix the dimmer slider", status="working",
        summary="It says the slider jumps because of a rounding step and it is rewriting that step.",
        asked="what is it doing"),
    act(Kind.THREAD_STATUS, thread="Rename the orchard sync job", status="interrupted",
        summary="Its last reply says two of the five renames were done.", asked="why did it stop"),
    act(Kind.THREAD_STATUS, thread="Trim the harbour map tiles", status="failed",
        summary="It reports that the build failed on a missing font file.", asked="did it work"),
    act(Kind.THREAD_STATUS, thread="Speed up the loom importer", status="working",
        summary="It says it cut the import time in half on the sample file.", asked="tell me more",
        since_last_asked=NOTHING_NEW),
    act(Kind.THREAD_STATUS, thread="Sort the seed catalogue", status="completed",
        summary="It says it sorted the catalogue by name and added a test for the order.", asked="walk me through it"),
    act(Kind.THREAD_STATUS, thread="Show seed packets in a grid", status="idle",
        summary="It says it is waiting for the layout choice before it goes on.", asked="what is the status"),
]
STATUS_WITHOUT_SUMMARY = [
    act(Kind.THREAD_STATUS, thread="Move the kiln log to its own page", status="working",
        asked="is it done"),
    act(Kind.THREAD_STATUS, thread="Could you start a thread to change how missing samples are han…",
        status="waiting_on_you", need="command_approval", asked="is it approved yet"),
    act(Kind.NEEDS_YOU, thread="Could you start a thread to change how missing samples are han…",
        need="command_approval"),
]
DONE = [
    act(Kind.DONE, thread="Add a harbour map legend", status="completed",
        summary="It reports that the legend shows five symbols."),
    act(Kind.DONE, thread="Fix the loom importer crash", status="failed",
        summary="It says the crash came from an empty thread list and it could not fix it."),
]
ASK_QUESTION = [
    act(Kind.ASK_QUESTION, thread="Tidy the tide table export", question="Which file format should the export use?",
        options=["CSV", "JSON", "Keep the current format"]),
    act(Kind.ASK_QUESTION, thread="Move the kiln log to its own page", question="Should the log page need a sign-in?",
        options=["Public", "Require sign-in"]),
    act(Kind.ASK_QUESTION, thread="Lantern app: fix the dimmer slider", question="What should the slider step be?",
        options=["One percent", "Five percent", "Ten percent", "Smooth"], own_words=True),
    act(Kind.ASK_QUESTION, thread="Rename the orchard sync job", question="What should the job be called?",
        options=[], own_words=True),
    act(Kind.ASK_QUESTION, thread="Sort the seed catalogue", question="Which sort order do you want first?",
        options=["By name", "By colour"], number=1, count=2),
    act(Kind.ASK_QUESTION, thread="Sort the seed catalogue", question="Should empty packets be shown?",
        options=["Show them", "Hide them"], number=2, count=2),
    act(Kind.ASK_QUESTION, thread="Trim the harbour map tiles", question="Which tile size should it use?",
        options=["Small", "Large"], restated="unheard"),
    act(Kind.ASK_QUESTION, thread="Speed up the loom importer", question="Which parser should it use?",
        options=["The fast one", "The strict one"], restated="changed"),
]
CONFIRM_ANSWER = [
    act(Kind.CONFIRM_ANSWER, thread="Tidy the tide table export", questions=["Which file format should the export use?"],
        answers=["CSV"]),
    act(Kind.CONFIRM_ANSWER, thread="Lantern app: fix the dimmer slider", questions=["What should the slider step be?"],
        answers=["Call it half a percent"]),
    act(Kind.CONFIRM_ANSWER, thread="Sort the seed catalogue",
        questions=["Which sort order do you want first?", "Should empty packets be shown?"],
        answers=["By colour", "Hide them"]),
    act(Kind.CONFIRM_ANSWER, thread="Move the kiln log to its own page", questions=["Should the log page need a sign-in?"],
        answers=["Require sign-in"]),
]
ANSWERED = [
    act(Kind.ANSWERED, thread="Tidy the tide table export", answers=["CSV"]),
    act(Kind.ANSWERED, thread="Sort the seed catalogue", answers=["By colour", "Hide them"]),
    act(Kind.ANSWERED, thread="Rename the orchard sync job", answers=["Call it orchard nightly"]),
]
ANSWER_IN_APP = [
    act(Kind.ANSWER_IN_APP, thread="Trim the harbour map tiles"),
    act(Kind.ANSWER_IN_APP, thread="Speed up the loom importer"),
]
OTHER = [
    act(Kind.CONFIRM_LAUNCH, project="Moss Notes"),
    act(Kind.CONFIRM_LAUNCH, project="Kite Planner"),
    act(Kind.CONFIRM_TELL, thread="Tidy the tide table export", message="keep the date column first"),
    act(Kind.CONFIRM_TELL, thread="Rename the orchard sync job", message="skip the archive folder"),
    act(Kind.CONFIRM_STOP, thread="Lantern app: fix the dimmer slider"),
    act(Kind.CONFIRM_STOP, thread="Sort the seed catalogue"),
    act(Kind.LAUNCH_PENDING, project="Pebble Garden"),
    act(Kind.CANCELLED, op="stop"),
    act(Kind.NO_THREAD, recent=12),
    act(Kind.REPLIED, thread="Speed up the loom importer", status="running", reply="I found 7 slow steps."),
]

GROUPS = {"confirm_thread": CONFIRM_THREAD, "ask_which": ASK_WHICH, "thread_status_summary": SUMMARY,
          "status_without_summary": STATUS_WITHOUT_SUMMARY,
          "done_summary": DONE, "other": OTHER, "ask_question": ASK_QUESTION, "confirm_answer": CONFIRM_ANSWER,
          "answered": ANSWERED, "answer_in_app": ANSWER_IN_APP}


def cases() -> list[tuple[str, tuple]]:
    """(group, acts) pairs: one act each."""
    return [(group, (a,)) for group, acts in GROUPS.items() for a in acts]


def turns_for(acts) -> list[dict]:
    """What the person just said, for the acts that carry what they asked."""
    asked = next((a.slots.get("asked") for a in acts if a.slots.get("asked")), None)
    return [{"user": f"Tell me, {asked}?"}] if asked else []
