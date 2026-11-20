"""Made-up pick requests and replies written before the request wording in voice/t3/verify.py was tuned.

The replies were scored once, after the wording was chosen. The picks were scored once with an earlier "not listed"
and "unclear" wording, which then changed for a regression d1.2 showed on the tuned rows, and were scored again. Report
them as they come out: a change made for these rows makes them tuning cases.
"""

from voice.t3.acts import Kind, act
from voice.t3.verify import ELSEWHERE, Listed

LISTING = (  # newest first
    ("Add a greenhouse humidity alarm", "Greenhouse", "working"),
    ("Fix the ferry timetable parser", "Ferry app", "failed"),
    ("Write the field notes exporter", "Field notes", "finished"),
    ("Speed up the photo upload", "Field notes", "working"),
    ("Add dark tiles to the ferry map", "Ferry app", "finished"),
    ("Rename the soil sensor module", "Greenhouse", "idle"),
    ("Fix the ticket refund rounding", "Ferry app", "waiting_on_you"),
    ("Add offline mode to field notes", "Field notes", "interrupted"),
    ("Document the watering schedule API", "Greenhouse", "finished"),
    ("Translate the ferry app to Spanish", "Ferry app", "finished"),
    ("Clean up the greenhouse logs", "Greenhouse", "finished"),
    ("Add a seat picker", "Ferry app", "idle"),
)
THREADS = tuple(Listed(f"f{i}", title, project, activity) for i, (title, project, activity) in enumerate(LISTING, 1))
T = [title for title, _, _ in LISTING]

# (request, the thread last spoken about, what the pick must be, the title it means)
PICKS = [
    ("How's the humidity alarm for the greenhouse coming?", None, "sure", T[0]),
    ("Did parsing the ferry timetable get fixed?", None, "sure", T[1]),
    ("Is the exporter for field notes done?", None, "sure", T[2]),
    ("How fast is the photo uploading now?", None, "sure", T[3]),
    ("Did the ferry map get its dark tiles?", None, "sure", T[4]),
    ("What happened with renaming the soil sensor?", None, "sure", T[5]),
    ("Where are we on the refund rounding fix?", None, "sure", T[6]),
    ("Check on offline support for field notes.", None, "sure", T[7]),
    ("Is the watering schedule documentation finished?", None, "sure", T[8]),
    ("How's the Spanish translation of the ferry app?", None, "sure", T[9]),
    ("Did the greenhouse log cleanup go through?", None, "sure", T[10]),
    ("Tell me about the seat picker thread.", None, "sure", T[11]),
    ("How is it doing?", None, "asks", None),
    ("What about that one?", None, "asks", None),
    ("Any update on the thread?", None, "asks", None),
    ("Is that one finished?", None, "asks", None),
    ("And the other one, how's it going?", None, "asks", None),
    ("Check on it for me.", None, "asks", None),
    ("What's the status of the ferry thread?", None, "asks", None),
    ("How's the greenhouse one?", None, "asks", None),
    ("Is it finished yet?", T[3], "leans", T[3]),
    ("How's that one going?", T[1], "leans", T[1]),
    ("What did it say?", T[6], "leans", T[6]),
    ("Check on it again.", T[0], "leans", T[0]),
    ("Any news on that thread?", T[9], "leans", T[9]),
    ("How's the bird feeder camera thread?", None, "none", None),
    ("Did the payroll export finish?", None, "none", None),
    ("Check on the chess engine thread.", None, "none", None),
    ("What happened to the podcast player?", None, "none", None),
    ("Is the recipe scaler done?", None, "none", None),
    ("Tell me about the parking sensor work.", None, "none", None),
    ("How's the lighthouse webcam thread?", None, "none", None),
]

CONFIRM = act(Kind.CONFIRM_THREAD, thread=T[1])
CONFIRM_ALARM = act(Kind.CONFIRM_THREAD, thread=T[0])
STOP = act(Kind.CONFIRM_STOP, thread=T[3])
TELL = act(Kind.CONFIRM_TELL, thread=T[2], message="also export the photos")
WHICH = act(Kind.ASK_WHICH, of="thread", candidates=[T[1], T[4], T[9]])
WHICH_OPTIONS = (T[1], T[4], T[9])

# (the question the voice asked, the reply, the options sent with it, whether it must act, the target it must read)
REPLIES = [(CONFIRM, reply, (), True, None) for reply in (
    "Yes.", "Yeah, that one.", "Yep, that's it.", "That's the one.", "Correct.", "Mm-hmm.", "Right.",
    "Yes, that's right?", "Exactly.", "Yeah?", "Uh, yes.", "Yes, the timetable one.")]
REPLIES += [(CONFIRM_ALARM, reply, (), True, None) for reply in ("Yeah, the humidity alarm.", "Sure, that one.")]
REPLIES += [(STOP, reply, (), True, None) for reply in ("Yeah, stop it.", "Okay, do it?")]
REPLIES += [(TELL, reply, (), True, None) for reply in ("Yep, send that.", "Sounds good.")]
REPLIES += [(CONFIRM, reply, (), False, None) for reply in (
    "No.", "No, not that one.", "Nope, wrong thread.", "No, I meant the ferry map.", "The seat picker one, actually.",
    "Not the timetable, the refund one.", "Is that the one that failed?", "What's the weather tomorrow?",
    "Hang on.", "I don't know.", "Yes, but the other ferry thread.", "Hmm, maybe?")]
REPLIES += [(STOP, reply, (), False, None) for reply in ("No, let it finish.", "Stop the other one instead.")]
REPLIES += [(TELL, reply, (), False, None) for reply in ("Send it to the uploader thread instead.",
                                                         "Yes, but say videos, not photos.")]
REPLIES += [(WHICH, reply, WHICH_OPTIONS, False, target) for reply, target in (
    ("The map one.", T[4]), ("The middle one.", T[4]), ("Spanish.", T[9]), ("The first.", T[1]),
    ("The seat picker.", ELSEWHERE), ("None of them.", None), ("Never mind.", None))]
