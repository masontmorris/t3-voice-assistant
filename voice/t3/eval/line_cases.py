"""Made-up acts for the line eval. Every title, project, summary and message here is invented, so the cases carry no
call content and can sit in the repo. They cover the acts the 2026-11-20 call failed on, each in several shapes:
a thread to confirm, which of several candidates, and a thread's status with a summary the line must pass on."""

from dataclasses import dataclass

from voice.t3.acts import NOTHING_NEW, Kind, act
from voice.t3.journal import launch_title
from voice.t3.speech import Launch

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
APP_THREAD_REFERENCES = {
    "Sort Seed Packets": "seed packets",
    "Could you start a thread to check the timestamps on imported tide rea…": "tide timestamps",
    'Make the "On Hold" badge readable': "badge",
    "The kiln log is missing its last page.": "kiln log",
}
# A thread started from a spoken request is titled with its first line, cut at 80 characters with an ellipsis, until T3
# names it. The requests are made up; each one is long, hesitant and holds more than one sentence.
PROJECT = "Pebble Garden"
clipped = launch_title


@dataclass(frozen=True)
class Scene:
    """What T3 and the journal hold when a case's act is worded: `thread` is the one the act is about, `launched` the
    threads the voice started, `titles` what T3 titles every listed thread now, `offered` the threads a
    which-thread question offers, and `on_call` whether `thread` is the one the call was opened on."""

    thread: str
    launched: tuple
    titles: dict
    offered: tuple = ()
    on_call: bool = False


@dataclass(frozen=True)
class Scened:
    act: object
    scene: Scene


def alone(request: str, thread: str = "t7") -> Scene:
    """One thread started from `request`, still titled with it, and no other."""
    return Scene(thread, (Launch(thread, request, PROJECT),), {thread: clipped(request)})


LAUNCH_REQUESTS = {
    "photo tool": "Um, I was hoping you could start a thread to get some work going on, um, a small tool that renames "
                  "holiday photos. Um, a small tool.",
    "pantry list": "So, uh, I want to get some work going on, uhm, a little script that sorts the pantry list. A script "
                   "that groups items by shelf.",
    "lantern config": "Hm, can you kick off a thread, uh, for tidying the lantern config files? Keep the formatting the "
                      "same as before please.",
}
LAUNCH_TITLES = {clipped(request): ref for ref, request in LAUNCH_REQUESTS.items()}
APP_THREAD_REFERENCES.update(LAUNCH_TITLES)
NEEDS_THE_APP = [act(Kind.NEEDS_THE_APP, thread=title, decision="approve") for title in APP_THREAD_REFERENCES]
LAUNCH_COMMANDS = ("printf 'ready' > dock-note.txt", "wc -l pantry.txt", "cp lantern.cfg lantern.bak")
LAUNCH_NEEDS = [act(Kind.NEEDS_YOU, thread=title, need="command_approval", prompt=command)
                for title, command in zip(LAUNCH_TITLES, LAUNCH_COMMANDS, strict=True)]
LAUNCH_STATUS = [
    act(Kind.THREAD_STATUS, thread=title, status="waiting_on_you", need="command_approval", pending=command,
        asked=asked, **extra)
    for (title, command), (asked, extra) in zip(zip(LAUNCH_TITLES, LAUNCH_COMMANDS, strict=True),
                                               (("is it approved yet", {}), ("is it approved now", {}),
                                                ("is it still waiting", {"since_last_asked": NOTHING_NEW})),
                                               strict=True)
]
LAUNCH_CONFIRM_THREAD = [act(Kind.CONFIRM_THREAD, thread=title) for title in list(LAUNCH_TITLES)[:2]]
NO_THREAD_OTHER = [act(Kind.NO_THREAD, other=True), act(Kind.NO_THREAD, recent=9), act(Kind.NO_THREAD)]
# Made-up pending commands, each with what a faithful description says it does and two lies about it: a label that
# names another action, and a purpose that says what the command is for when the request does not.
NEED_REQUESTS = (
    ("Check Dock Times", "printf 'ready' > dock-note.txt", "It wants to write a short note into a file.",
     "write a short note into a file", "run a cleanup script", "publish the dock schedule"),
    ("Review Print Sizes", "wc -l print-sizes.txt", "It wants to count the lines in a file.",
     "count the lines in a file", "run a migration script", "validate the print layout"),
    ("Sort Pantry List", "sort pantry.txt -o pantry.txt", "It wants to sort a file in place.",
     "sort a file in place", "delete old pantry entries", "back up the pantry list"),
    ("Tune Lantern Config", "cp lantern.cfg lantern.bak", "It wants to copy a config file to a backup.",
     "copy a config file to a backup", "reset the lantern settings", "send the lantern config to the server"),
    ("Trim Kite Logs", "ls logs | head", "It wants to list a few files in the logs folder.",
     "list a few files in the logs folder", "clear the kite logs", "compress the kite logs"),
    ("Move Tide Notes", "mv notes.txt archive/notes.txt", "It wants to move a notes file into the archive folder.",
     "move a notes file into the archive folder", "rename the tide notes", "delete the tide notes"),
)
NEEDS_WITH_COMMAND = [
    act(kind, thread=title, need="command_approval", **slots)
    for title, command, *_ in NEED_REQUESTS[:2]
    for kind, slots in (
        (Kind.NEEDS_YOU, {"prompt": command}),
        (Kind.THREAD_STATUS, {"status": "waiting_on_you", "pending": command, "asked": "is it still waiting"}),
    )
]


def need_lines(legacy: bool = False):
    """(shape, mode, act, line): faithful and invented claims about each pending command, said once from the act as a
    turn holds it with a description that passed the gate (`described`), and once with none (`kind_only`). `legacy` is
    the act as it was before a line was kept from the command's stored text: that text in `prompt`, and one mode."""
    for title, command, description, faithful, label, purpose in NEED_REQUESTS:
        if legacy:
            said = act(Kind.NEEDS_YOU, thread=title, need="command_approval", prompt=command)
            for shape, clause in (("faithful", faithful), ("invented_label", label), ("invented_purpose", purpose)):
                yield shape, "legacy", said, f"{title} is waiting for approval to {clause}."
            continue
        for mode, said, kind_clause in (
                ("described", act(Kind.NEEDS_YOU, thread=title, need="command_approval", request=description), faithful),
                ("kind_only", act(Kind.NEEDS_YOU, thread=title, need="command_approval"), "run a command")):
            for shape, clause in (("faithful", kind_clause), ("invented_label", label), ("invented_purpose", purpose)):
                yield shape, mode, said, f"{title} is waiting for approval to {clause}."


DONE = [
    act(Kind.DONE, thread="Add a harbour map legend", status="completed",
        summary="It reports that the legend shows five symbols."),
    act(Kind.DONE, thread="Fix the loom importer crash", status="failed",
        summary="It says the crash came from an empty thread list and it could not fix it."),
]
# A run that ended by being stopped: its status is a participle that needs "was" (the repeat call said "X cancelled and
# the thread's last reply said ...").
DONE_STOPPED = [
    act(Kind.DONE, thread="Sort Seed Packets", status="cancelled",
        summary="Its last reply said it was grouping the packets by season."),
    act(Kind.DONE, thread="Make the \"On Hold\" badge readable", status="interrupted",
        summary="It says it had darkened the badge text and was checking the contrast."),
    act(Kind.DONE, thread="Sort Seed Packets", status="rolled_back",
        summary="It reports that the new sort order was undone."),
]
# A request that must go to the app with no checked description: the line says what kind of thing it asks for.
NEEDS_THE_APP_NEED = [
    act(Kind.NEEDS_THE_APP, thread="Sort Seed Packets", decision="approve", need="command_approval"),
    act(Kind.NEEDS_THE_APP, thread='Make the "On Hold" badge readable', decision="approve", need="file_change"),
    act(Kind.NEEDS_THE_APP, thread="The kiln log is missing its last page.", decision="approve",
        need="command_approval"),
]
# An approval asked about a thread the call started, which has no title, worded from the `kind_only_begun` examples:
# they open on "Should I approve it", which the call's voice says first (voice/t3/eval/relay_turn_eval.py).
CONFIRM_RESPOND_BEGUN = [
    act(Kind.CONFIRM_RESPOND, begun_in="Moss Notes", begun_for="sorting the seed packets by season",
        decision="approve", need="command_approval"),
    act(Kind.CONFIRM_RESPOND, begun_in="Tide Tables", begun_for="exporting the tide times as a table",
        decision="approve", need="file_change"),
    act(Kind.CONFIRM_RESPOND, begun_in="Kiln Log", begun_for="giving each firing its own page",
        decision="approve", need="command_approval"),
    act(Kind.CONFIRM_RESPOND, begun_in="Fern Atlas", begun_for="a search box for the atlas", decision="approve",
        need="file_read"),
    act(Kind.CONFIRM_RESPOND, begun_in="Brook Survey", decision="approve", need="command_approval"),
    act(Kind.CONFIRM_RESPOND, begun_in="Harbour Maps", decision="approve", need="file_change"),
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

# Written after the groups above had been run and tuned on, and scored once.
HELD_OUT_REQUESTS = {
    "beekeeping log": ("Okay, so, um, could you maybe get a thread going for, uh, the beekeeping log? Um, it should list "
                       "hive visits by date.", "grep -c todo notes.md"),
    "sailing newsletter": ("Right, uhm, I'd like a thread that cleans up, uh, the sailing club newsletter template. "
                           "Mainly the footer.", "rm -f scratch.tmp"),
}
# Shapes the four acts above do not take: a launched thread beside an ordinary one in its project, two launched threads
# in one project, and a launched thread T3 has named since the act was made. Written after the groups above had been run
# and tuned on, and scored once.
TIDE = ("Hmm, so, uh, could you start a thread for the tide chart widget? Um, the labels overlap on small screens.")
FERRY = "Right, um, I'd like a thread on, uh, the ferry timetable import. Um, it chokes on blank rows."
KITE = "Okay, so, uhm, can you get something going on the kite log viewer? Um, it forgets the last filter."
HARBOUR = "Tidy the harbour map"
WAITING = dict(need="command_approval", status="waiting_on_you")
_mixed = Scene("t7", (Launch("t7", TIDE, PROJECT),), {"t7": clipped(TIDE), "t9": HARBOUR})
_two = {"launched": (Launch("t7", TIDE, PROJECT), Launch("t8", FERRY, PROJECT)),
        "titles": {"t7": clipped(TIDE), "t8": clipped(FERRY)}}
HELD_OUT_SHAPES = [
    Scened(act(Kind.NEEDS_YOU, thread=clipped(TIDE), need="command_approval", prompt="ls tide-charts | head"), _mixed),
    Scened(act(Kind.THREAD_STATUS, thread=HARBOUR, asked="is it approved", pending="rm -f harbour.tmp", **WAITING),
           Scene("t9", _mixed.launched, _mixed.titles)),
    Scened(act(Kind.NEEDS_YOU, thread=clipped(TIDE), need="command_approval", prompt="ls tide-charts | head"),
           Scene("t7", **_two)),
    Scened(act(Kind.THREAD_STATUS, thread=clipped(FERRY), asked="is it approved", pending="wc -l ferry.csv", **WAITING),
           Scene("t8", **_two)),
    Scened(act(Kind.CONFIRM_THREAD, thread=clipped(TIDE)), Scene("t7", **_two)),
    Scened(act(Kind.NEEDS_THE_APP, thread=clipped(FERRY), decision="approve"), Scene("t8", **_two)),
    Scened(act(Kind.ASK_WHICH, of="thread", candidates=[clipped(TIDE), clipped(FERRY)]),
           Scene("t7", **_two, offered=("t7", "t8"))),
    Scened(act(Kind.NEEDS_YOU, thread=clipped(KITE), need="command_approval", prompt="cat kite.log | tail"),
           Scene("t7", (Launch("t7", KITE, PROJECT),), {"t7": "Fix the kite log viewer"})),
    Scened(act(Kind.CONFIRM_THREAD, thread=clipped(KITE)),
           Scene("t7", (Launch("t7", KITE, PROJECT),), {"t7": "Fix the kite log viewer"})),
]
APP_THREAD_REFERENCES.update({clipped(FERRY): "ferry timetable", clipped(TIDE): "tide chart"})
# The thread a call from the app was opened on, one of two launched threads in a project that T3 has not yet named:
# the line calls it this thread. Written with the change that makes it so, and scored once.
_on_call = Scene("t7", **_two, on_call=True)
CALL_THREAD = [
    Scened(act(Kind.NEEDS_YOU, thread=clipped(TIDE), need="command_approval", prompt="ls tide-charts | head"), _on_call),
    Scened(act(Kind.THREAD_STATUS, thread=clipped(TIDE), asked="is it approved", pending="ls tide-charts | head",
               **WAITING), _on_call),
    Scened(act(Kind.DONE, thread=clipped(TIDE), status="completed",
               summary="It reports that the labels no longer overlap."), _on_call),
    Scened(act(Kind.REPLIED, thread=clipped(TIDE), status="running", reply="I moved the labels below the chart."),
           _on_call),
    Scened(act(Kind.NEEDS_THE_APP, thread=clipped(TIDE), decision="approve"), _on_call),
    Scened(act(Kind.CONFIRM_THREAD, thread=clipped(TIDE)), _on_call),
]

# What each request was started for, a faithful account of it and two that say something the request does not.
ACCOUNT_CASES = (
    (TIDE, "the tide chart widget labels overlapping on small screens",
     ("the tide chart widget crashing on startup", "adding a new tide chart for the harbour")),
    (FERRY, "the ferry timetable import failing on blank rows", ("deleting the ferry timetable", "the ferry fares page")),
    (KITE, "the kite log viewer forgetting its last filter", ("the kite log viewer being too slow", "kite sales totals")),
    (LAUNCH_REQUESTS["pantry list"], "sorting the pantry list and grouping items by shelf",
     ("deleting the old pantry list", "the pantry delivery schedule")),
    (LAUNCH_REQUESTS["photo tool"], "a small tool that renames holiday photos",
     ("uploading holiday photos to a server", "resizing holiday photos")),
)

HELD_OUT = [
    said
    for ref, (request, command) in HELD_OUT_REQUESTS.items()
    for title in [clipped(request)]
    for said in (act(Kind.NEEDS_YOU, thread=title, need="command_approval", prompt=command),
                 act(Kind.THREAD_STATUS, thread=title, status="waiting_on_you", need="command_approval",
                     pending=command, asked="is it approved"),
                 act(Kind.NEEDS_THE_APP, thread=title, decision="approve"),
                 act(Kind.CONFIRM_THREAD, thread=title))
]
APP_THREAD_REFERENCES.update({clipped(request): ref for ref, (request, _) in HELD_OUT_REQUESTS.items()})
LAUNCH_TITLES.update({clipped(request): ref for ref, (request, _) in HELD_OUT_REQUESTS.items()})

# The second question of a request held behind another one of the same breath. Each is worded beside its twin, the same
# act without `another`, which is what the first question says: the caller can tell the two apart only when they differ.
ANOTHER_REQUEST = [
    act(Kind.CONFIRM_LAUNCH, project="Pebble Garden", another=True, about="speeding up the shader compile step"),
    act(Kind.CONFIRM_LAUNCH, project="Ferry Times", another=True, about="cleaning up the save slot writer"),
    act(Kind.CONFIRM_LAUNCH, project="Quilt Maker", another=True, about="a photo mode for the gallery"),
    act(Kind.CONFIRM_LAUNCH, project="Moss Notes", another=True),
    act(Kind.CONFIRM_LAUNCH, project="Kite Planner", another=True),
    act(Kind.CONFIRM_TELL, thread="Tidy the tide table export", message="keep the date column first", another=True),
    act(Kind.CONFIRM_TELL, thread="Rename the orchard sync job", message="skip the archive folder", another=True),
    act(Kind.CONFIRM_STOP, thread="Rename the orchard sync job", another=True),
    act(Kind.CONFIRM_STOP, thread="Speed up the loom importer", another=True),
    act(Kind.CONFIRM_THREAD, thread="Lantern app: fix the dimmer slider", another=True),
    act(Kind.CONFIRM_THREAD, thread="Sort the seed catalogue", another=True),
    act(Kind.ASK_WHICH, of="project", candidates=["Moss Notes", "Kite Planner"], another=True),
    act(Kind.ASK_WHICH, of="thread", candidates=["Trim the harbour map tiles", "Trim the harbour map labels"],
        another=True),
]

GROUPS = {"confirm_thread": CONFIRM_THREAD, "ask_which": ASK_WHICH, "thread_status_summary": SUMMARY,
          "status_without_summary": STATUS_WITHOUT_SUMMARY,
          "done_summary": DONE, "done_stopped": DONE_STOPPED, "needs_the_app_need": NEEDS_THE_APP_NEED, "other": OTHER, "ask_question": ASK_QUESTION, "confirm_answer": CONFIRM_ANSWER,
          "confirm_respond_begun": CONFIRM_RESPOND_BEGUN,
          "answered": ANSWERED, "answer_in_app": ANSWER_IN_APP, "needs_the_app": NEEDS_THE_APP,
          "needs_with_command": NEEDS_WITH_COMMAND, "launch_needs_you": LAUNCH_NEEDS,
          "launch_status_waiting": LAUNCH_STATUS, "launch_confirm_thread": LAUNCH_CONFIRM_THREAD,
          "no_thread_other": NO_THREAD_OTHER, "held_out_launch": HELD_OUT, "held_out_shapes": HELD_OUT_SHAPES,
          "call_thread": CALL_THREAD, "another_request": ANOTHER_REQUEST}


REQUESTS_BY_TITLE = {clipped(request): request for request in
                     (*LAUNCH_REQUESTS.values(), *(request for request, _ in HELD_OUT_REQUESTS.values()))}


def cases() -> list[tuple[str, tuple, Scene | None]]:
    """(group, acts, scene) triples: one act each. An act titled with a launch request that has no scene of its own is
    the one launched thread T3 has not yet named."""
    out = []
    for group, acts in GROUPS.items():
        for a in acts:
            scene = a.scene if isinstance(a, Scened) else None
            a = a.act if isinstance(a, Scened) else a
            request = REQUESTS_BY_TITLE.get(a.slots.get("thread"))
            out.append((group, (a,), scene or (alone(request) if request else None)))
    return out


def turns_for(acts) -> list[dict]:
    """What the person just said, for the acts that carry what they asked."""
    if acts[0].kind is Kind.NEEDS_THE_APP:
        title = acts[0].slots["thread"]
        ref = APP_THREAD_REFERENCES[title]
        return [{"user": f"Is the {ref} thread waiting on me?",
                 "assistant": f"{title} is waiting for approval."},
                {"user": f"Please approve the request on the {ref} thread."}]
    asked = next((a.slots.get("asked") for a in acts if a.slots.get("asked")), None)
    return [{"user": f"Tell me, {asked}?"}] if asked else []
