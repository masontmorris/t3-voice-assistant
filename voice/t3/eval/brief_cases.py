"""Made-up launch requests for the check of a started thread's first message (`local_summary.BRIEF_GATE`).

Each case is a request a caller might speak to start a thread, in one of four shapes: plain, hesitant with filler, with
a constraint, and with two tasks. Beside it are briefs that hand the new thread the work faithfully, and briefs that
must be refused:

- `adds` asks for something the request does not (an extra step, a test, a section);
- `drops` leaves out a detail of the work or the second task;
- `starts` still asks its reader to start a thread;
- `target` changes or drops where the work is done (the project, app, screen, file or section);
- `negation` drops a prohibition ("don't touch", "keep ... as it is");
- `limit` drops a size or limit word ("short", "only", "under ten seconds").

SEEN was read while the gate's wording and bars were chosen. HELD_OUT was written at the same time and only scored.
The `target`, `negation` and `limit` kinds were added on 2026-11-22 after a review found the gate passing a brief that
dropped "settings" and one that dropped "short"; two of those briefs were `drops` before. Every name is invented.
"""

from __future__ import annotations

SHAPES = ("plain", "hesitant", "constraint", "two_tasks")
KINDS = ("adds", "drops", "starts", "target", "negation", "limit")


def case(name, shape, request, faithful, *, adds=(), drops=(), starts=(), target=(), negation=(), limit=()):
    return {"name": name, "shape": shape, "request": request, "faithful": list(faithful), "adds": list(adds),
            "drops": list(drops), "starts": list(starts), "target": list(target), "negation": list(negation),
            "limit": list(limit)}


SEEN = [
    case("bakery_readme", "plain", "Hey, could you start a thread to write a short readme for the bakery menu project?",
         ["Write a short README for the bakery menu project.", "Write a short readme for the bakery menu project."],
         adds=["Write a short README for the bakery menu project, with install steps and a license section."],
         limit=["Write a README for the bakery menu project."],
         target=["Write a short README for the bakery website project."],
         starts=["Start a new thread that writes a short README for the bakery menu project."]),
    case("lantern_toggle", "plain", "Can you start a thread to add a dark mode toggle to the lantern app settings?",
         ["Add a dark mode toggle to the lantern app's settings.",
          "In the lantern app, add a dark mode toggle to the settings."],
         adds=["Add a dark mode toggle to the lantern app's settings and write tests for it."],
         target=["Add a dark mode toggle to the lantern app.", "Add a dark mode toggle to the lantern app's main screen."],
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
         negation=["Rename the sprite folders in the kite game."],
         starts=["Start a thread that renames the sprite folders in the kite game without touching the sound "
                 "folder."]),
    case("tide_dates", "two_tasks",
         "Can you start a thread that fixes the date format on the tide chart and then updates the changelog?",
         ["Fix the date format on the tide chart, then update the changelog.",
          "Fix how dates are formatted on the tide chart, and after that update the changelog."],
         adds=["Fix the date format on the tide chart, update the changelog, and bump the version number."],
         drops=["Fix the date format on the tide chart."],
         target=["Fix the date format on the moon chart, then update the changelog."],
         starts=["Start a thread that fixes the date format on the tide chart and then updates the changelog."]),
    case("loom_errors", "hesitant",
         "Okay, uh, I'd like a thread that, um, goes through the loom importer and, uh, makes the error messages "
         "clearer. Thanks.",
         ["Go through the loom importer and make its error messages clearer.",
          "Make the error messages in the loom importer clearer."],
         adds=["Go through the loom importer, make its error messages clearer and translate them into Spanish."],
         drops=["Go through the loom importer."],
         starts=["Start a new thread that goes through the loom importer and makes its error messages clearer."]),
    case("pantry_welcome", "constraint",
         "Can you start a thread to shorten the pantry app's welcome text to one sentence?",
         ["Shorten the pantry app's welcome text to one sentence."],
         limit=["Shorten the pantry app's welcome text."],
         target=["Shorten the pantry app's help text to one sentence."]),
    case("lantern_icons", "constraint",
         "Start a thread to update the lantern app's icons, but don't touch the app logo.",
         ["Update the lantern app's icons, but do not touch the app logo."],
         negation=["Update the lantern app's icons.", "Update the lantern app's icons and the app logo."],
         target=["Update the lantern website's icons, but do not touch the app logo."]),
    case("loom_dates", "constraint",
         "Start a thread to fix only the date column in the loom importer's export, nothing else.",
         ["In the loom importer's export, fix only the date column and nothing else."],
         limit=["Fix the date column in the loom importer's export."],
         target=["Fix only the date column in the loom importer's settings page."]),
]

HELD_OUT = [
    case("ferry_speed", "plain", "Start a thread to make the ferry timetable page load faster.",
         ["Make the ferry timetable page load faster."],
         adds=["Make the ferry timetable page load faster and add a loading spinner."],
         drops=["Look at the ferry timetable page."],
         target=["Make the ferry booking page load faster."],
         starts=["Start a thread to make the ferry timetable page load faster."]),
    case("reef_pause", "hesitant",
         "Okay, um, can you, like, get a thread going on the reef card game? Uh, the score resets when you pause.",
         ["In the reef card game, fix the score resetting when the game is paused.",
          "Fix the reef card game: the score resets when you pause."],
         adds=["Fix the score resetting on pause in the reef card game, and add a pause menu."],
         drops=["Look into the reef card game's score."],
         starts=["Get a new thread going on the reef card game to fix the score resetting when you pause."]),
    case("forecast_colors", "constraint",
         "Can you start a thread to update the chart colors in the forecast widget, keeping the current blue for rain?",
         ["Update the chart colors in the forecast widget, keeping the current blue for rain.",
          "Change the forecast widget's chart colors, but keep rain in its current blue."],
         adds=["Update the chart colors in the forecast widget, keeping the current blue for rain, and add a legend."],
         negation=["Update the chart colors in the forecast widget."],
         target=["Update the chart colors in the forecast app's settings screen, keeping the current blue for rain."],
         starts=["Start a thread that updates the forecast widget's chart colors and keeps the current blue for rain."]),
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
         negation=["Clean up the logging in the harbour map server."],
         target=["Clean up the logging in the harbour map client, but keep the error logs as they are."],
         starts=["Open a thread to clean up the harbour map server's logging, keeping the error logs as they are."]),
    case("reef_intro", "constraint",
         "Could you start a thread to cut the reef card game's intro down to under ten seconds?",
         ["Cut the reef card game's intro down to under ten seconds."],
         limit=["Shorten the reef card game's intro."],
         target=["Cut the reef card game's credits down to under ten seconds."]),
    case("ferry_departures", "constraint",
         "Start a thread to show at most five departures on the ferry home screen.",
         ["Show at most five departures on the ferry home screen."],
         limit=["Show the departures on the ferry home screen."],
         target=["Show at most five departures on the ferry settings screen."]),
    case("recipe_css", "constraint",
         "Start a thread to tidy the recipe book's CSS, and please don't change any colors.",
         ["Tidy the recipe book's CSS without changing any colors."],
         negation=["Tidy the recipe book's CSS."],
         target=["Tidy the recipe book's HTML without changing any colors."]),
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
