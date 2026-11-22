"""Made-up launch requests for the check of a started thread's first message (`local_summary.BRIEF_GATE`).

Each case is a request a caller might speak to start a thread, in one of four shapes: plain, hesitant with filler, with
a constraint, and with two tasks. Beside it are briefs that hand the new thread the work faithfully, and briefs that
must be refused:

- `adds` asks for something the request does not (an extra step, a test, a section);
- `drops` leaves out a constraint, a detail or the second task;
- `starts` still asks its reader to start a thread.

SEEN was read while the gate's wording and bars were chosen. HELD_OUT was written at the same time and only scored.
Every name is invented.
"""

from __future__ import annotations

SHAPES = ("plain", "hesitant", "constraint", "two_tasks")
KINDS = ("adds", "drops", "starts")


def case(name, shape, request, faithful, *, adds=(), drops=(), starts=()):
    return {"name": name, "shape": shape, "request": request, "faithful": list(faithful), "adds": list(adds),
            "drops": list(drops), "starts": list(starts)}


SEEN = [
    case("bakery_readme", "plain", "Hey, could you start a thread to write a short readme for the bakery menu project?",
         ["Write a short README for the bakery menu project.", "Write a short readme for the bakery menu project."],
         adds=["Write a short README for the bakery menu project, with install steps and a license section."],
         drops=["Write a README for the bakery menu project."],
         starts=["Start a new thread that writes a short README for the bakery menu project."]),
    case("lantern_toggle", "plain", "Can you start a thread to add a dark mode toggle to the lantern app settings?",
         ["Add a dark mode toggle to the lantern app's settings.",
          "In the lantern app, add a dark mode toggle to the settings."],
         adds=["Add a dark mode toggle to the lantern app's settings and write tests for it."],
         drops=["Add a dark mode toggle to the lantern app."],
         starts=["Open a new thread to add a dark mode toggle to the lantern app's settings."]),
    case("pantry_search", "hesitant",
         "Um, so, uh, could you start a thread for the, uh, pantry list app? Um, the search box doesn't find items "
         "with capital letters.",
         ["In the pantry list app, fix the search box so it finds items with capital letters.",
          "Fix the pantry list app's search box: it does not find items with capital letters."],
         adds=["Fix the pantry list app's search box so it finds items with capital letters, and add a clear button "
               "to it."],
         drops=["Look at the search box in the pantry list app."],
         starts=["Create a thread for the pantry list app to fix the search box so it finds items with capital "
                 "letters."]),
    case("kite_folders", "constraint",
         "Start a thread to rename the sprite folders in the kite game, but don't touch the sound folder.",
         ["Rename the sprite folders in the kite game. Do not touch the sound folder.",
          "In the kite game, rename the sprite folders and leave the sound folder alone."],
         adds=["Rename the sprite folders in the kite game and update the docs to match. Do not touch the sound "
               "folder."],
         drops=["Rename the sprite folders in the kite game."],
         starts=["Start a thread that renames the sprite folders in the kite game without touching the sound "
                 "folder."]),
    case("tide_dates", "two_tasks",
         "Can you start a thread that fixes the date format on the tide chart and then updates the changelog?",
         ["Fix the date format on the tide chart, then update the changelog.",
          "Fix how dates are formatted on the tide chart, and after that update the changelog."],
         adds=["Fix the date format on the tide chart, update the changelog, and bump the version number."],
         drops=["Fix the date format on the tide chart."],
         starts=["Start a thread that fixes the date format on the tide chart and then updates the changelog."]),
    case("loom_errors", "hesitant",
         "Okay, uh, I'd like a thread that, um, goes through the loom importer and, uh, makes the error messages "
         "clearer. Thanks.",
         ["Go through the loom importer and make its error messages clearer.",
          "Make the error messages in the loom importer clearer."],
         adds=["Go through the loom importer, make its error messages clearer and translate them into Spanish."],
         drops=["Go through the loom importer."],
         starts=["Start a new thread that goes through the loom importer and makes its error messages clearer."]),
]

HELD_OUT = [
    case("ferry_speed", "plain", "Start a thread to make the ferry timetable page load faster.",
         ["Make the ferry timetable page load faster."],
         adds=["Make the ferry timetable page load faster and add a loading spinner."],
         drops=["Look at the ferry timetable page."],
         starts=["Start a thread to make the ferry timetable page load faster."]),
    case("reef_pause", "hesitant",
         "Okay, um, can you, like, get a thread going on the reef card game? Uh, the score resets when you pause.",
         ["In the reef card game, fix the score resetting when the game is paused.",
          "Fix the reef card game: the score resets when you pause."],
         adds=["Fix the score resetting on pause in the reef card game, and add a pause menu."],
         drops=["Look into the reef card game's score."],
         starts=["Get a new thread going on the reef card game to fix the score resetting when you pause."]),
    case("weather_colors", "constraint",
         "Can you start a thread to update the chart colors in the weather widget, keeping the current blue for rain?",
         ["Update the chart colors in the weather widget, keeping the current blue for rain.",
          "Change the weather widget's chart colors, but keep rain in its current blue."],
         adds=["Update the chart colors in the weather widget, keeping the current blue for rain, and add a legend."],
         drops=["Update the chart colors in the weather widget."],
         starts=["Start a thread that updates the weather widget's chart colors and keeps the current blue for rain."]),
    case("recipe_search", "two_tasks",
         "Please start a thread to add a search bar to the recipe book and sort the recipes by name.",
         ["Add a search bar to the recipe book and sort the recipes by name.",
          "In the recipe book, add a search bar, and sort the recipes by name."],
         adds=["Add a search bar to the recipe book, sort the recipes by name, and add a favorites list."],
         drops=["Add a search bar to the recipe book."],
         starts=["Create a new thread that adds a search bar to the recipe book and sorts the recipes by name."]),
    case("harbour_logs", "constraint",
         "Uh, start a thread to, um, clean up the logging in the harbour map server, but, uh, keep the error logs as "
         "they are.",
         ["Clean up the logging in the harbour map server, but keep the error logs as they are.",
          "Tidy the harbour map server's logging and leave the error logs unchanged."],
         adds=["Clean up the logging in the harbour map server, keep the error logs as they are, and add log "
               "rotation."],
         drops=["Clean up the logging in the harbour map server."],
         starts=["Open a thread to clean up the harbour map server's logging, keeping the error logs as they are."]),
]

SPLITS = (("seen", SEEN), ("held_out", HELD_OUT))


def briefs(cases: list[dict]) -> list[dict]:
    """Every brief to score, as {case, shape, kind, text, faithful}: a faithful one passes, any other should not."""
    rows = []
    for c in cases:
        rows += [{"case": c["name"], "shape": c["shape"], "kind": "faithful", "text": t, "faithful": True}
                 for t in c["faithful"]]
        for kind in KINDS:
            rows += [{"case": c["name"], "shape": c["shape"], "kind": kind, "text": t, "faithful": False}
                     for t in c[kind]]
    return rows
