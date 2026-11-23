"""Record the hosted decider's answers for the line-check fixture: the reviewer's three escapes, the status claims
code's word lists used to refuse, and one right line for each, so voice/t3/test_verify.py replays real answers
with no live call.

    VOICE_ENV_FILE=<env file> python -m voice.t3.eval.record_verify
    VOICE_ENV_FILE=<env file> python -m voice.t3.eval.record_verify --attribution-probe

Run it again after a change to the questions in voice/t3/verify.py. Each new line goes to Liquid's d1.2.
The attribution probe records the optional question separately because its answers were too noisy to gate speech.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from voice.liquid_decider.client import LiquidClient
from voice.t3.acts import NOTHING_NEW, Kind, act
from voice.t3.verify import request

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "verify_lines.json"
ATTRIBUTION_FIXTURE = FIXTURE.with_name("verify_attribution.json")
ATTRIBUTION_QUESTION = ("Does the spoken line claim work or results without an attribution like 'it says', "
                        "'it reported', or 'according to the thread'? Ignore the thread's run status and any "
                        "request for the person to act.")

CASES = [
    ("a stop confirm", act(Kind.CONFIRM_STOP, thread="Alpha"), "Alpha is complete?", "Want me to stop Alpha?"),
    ("a working status", act(Kind.THREAD_STATUS, thread="Alpha", status="working"), "Alpha has failed.",
     "Alpha is still working on it."),
    ("Alpha's run status", act(Kind.RUN_UPDATE, thread="Alpha", status="running"),
     "Beta is running. Alpha is unrelated.", "Alpha is running now."),
]
T = "Add a clamp helper"
# The status claims code's word lists refused before the decider replaced them, each beside its act's right line.
CLAIMS = [
    (act(Kind.CONFIRM_LAUNCH, project="Voice sandbox"), "It started in Voice sandbox, want more?",
     "Should I start that in Voice sandbox?"),
    (act(Kind.CONFIRM_TELL, thread=T, message="also add a test"),
     f"I sent {T} the note to also add a test, okay?", f"Should I tell {T} to also add a test?"),
    (act(Kind.CONFIRM_STOP, thread=T), f"I've stopped {T}, right?", f"Want me to stop {T}?"),
    (act(Kind.CANCELLED, op="stop", thread=T), f"Okay, {T} is stopped.", f"Okay, I'll leave {T} running."),
    (act(Kind.THREAD_STATUS, thread=T, status="working", summary="It added the helper."), f"{T} has finished.",
     f"{T} is still working and says it added the helper."),
    (act(Kind.THREAD_STATUS, thread=T, status="working", summary="It added the helper."), f"{T} was stopped.",
     None),
    (act(Kind.TELL_PENDING, thread=T), f"I passed that on to {T}.", f"I've asked T3 to pass that to {T}."),
    (act(Kind.HELD_FOLLOWUP, thread=T), f"{T} has finished, so I sent it.",
     f"{T} is busy, so I'll hold that until its run ends."),
    (act(Kind.DROPPED_FOLLOWUP, thread=T), f"I've passed that on to {T}.", f"Okay, I won't give that to {T}."),
    (act(Kind.STOPPING, thread=T), f"{T} has stopped.", f"I'm stopping {T} now."),
    (act(Kind.NOTHING_RUNNING, thread=T), f"I stopped {T}.", f"{T} isn't doing anything right now."),
    (act(Kind.NOTHING_RUNNING, thread=T), f"{T} is still running.", None),
    (act(Kind.STOP_UNCONFIRMED, thread=T), f"{T} was stopped.", f"I couldn't reach T3 to stop {T}, so use the app."),
    (act(Kind.RUN_UPDATE, thread=T, status="running"), f"{T} is done.", f"{T} is on it now."),
    (act(Kind.REPLIED, thread=T, status="running", reply="I added clamp."), f"{T} finished adding clamp.",
     f"{T} says it added clamp."),
    (act(Kind.DONE, thread=T, status="failed"), f"{T} is still working.", f"{T} failed partway through."),
    (act(Kind.REFUSED, op="start", code="refused", reason="Model is off."), "T3 started it.",
     "T3 wouldn't start it because the model is off."),
]
CASES += [(f"claim {said.kind.value}", said, wrong, right) for said, wrong, right in CLAIMS]
SMOKE = "Add a Clamp Helper"
# Right lines the decider refused in the Replay smoke run (2026-11-18), each beside a wrong line for its act.
CASES += [
    ("smoke replied", act(Kind.REPLIED, thread=SMOKE, status="running",
                          reply="I added clamp next to sum. All 12 tests pass."),
     f"{SMOKE} has finished: it added clamp next to sum.", f"{SMOKE} says it added clamp next to sum and the tests pass."),
    # Refused twice in the fourth smoke run: an interrupted run has ended, but it did not finish.
    ("smoke done interrupted", act(Kind.DONE, thread="Add a Sum Helper", status="interrupted",
                                   summary="The thread has no reply yet and is interrupted, with nothing pending or "
                                           "waiting on."),
     "Add a Sum Helper completed, and the tests pass.",
     "Add a Sum Helper ended as interrupted, with no reply yet and nothing pending or waiting on."),
    # The PM's two replied cases (2026-11-18), recorded TAKES times each, since one d1.2 answer can drift.
    ("pm replied finished work", act(Kind.REPLIED, thread=SMOKE, status="running",
                                     reply="I added clamp next to sum. All 12 tests pass."),
     None, f"{SMOKE} says it added clamp and the tests pass."),
    ("pm replied says done", act(Kind.REPLIED, thread=SMOKE, status="running",
                                 reply="I added clamp next to sum. All 12 tests pass."),
     f"{SMOKE} is done: it added clamp, and the tests pass.", None),
    ("smoke sent_followup", act(Kind.SENT_FOLLOWUP, thread=SMOKE), f"Your message is still waiting for {SMOKE}.",
     f"Your message reached {SMOKE}."),
    # A request for more detail right after a status line (hosted call, 2026-11-19): the line said again must still
    # pass on what the thread reported.
    ("said again", act(Kind.THREAD_STATUS, thread=T, status="cancelled", summary="It reported setting up the project.",
                       since_last_asked=NOTHING_NEW),
     f"There's nothing new from {T} since I last told you. It was cancelled.",
     f"There's nothing new from {T} since I last told you. It was cancelled after it reported setting up the project."),
]

# The summary question must pass a faithful paraphrase of the thread's report and refuse a line that leaves the report
# out, as the 2026-11-20 call's did ("... as mentioned in the summary."). Each case pairs a wrong line with a
# paraphrase that says the same thing in other words.
PARAPHRASES = [
    ("paraphrase failed build", act(Kind.THREAD_STATUS, thread="Trim the harbour map tiles", status="failed",
                                    summary="It reports that the build failed on a missing font file.", asked="did it work"),
     "The Trim the harbour map tiles thread failed, as mentioned in the summary.",
     "The Trim the harbour map tiles thread failed, and it says the build stopped because a font file was missing."),
    ("paraphrase halved import", act(Kind.THREAD_STATUS, thread="Speed up the loom importer", status="working",
                                     summary="It says it cut the import time in half on the sample file."),
     "Speed up the loom importer is still working and has made good progress.",
     "Speed up the loom importer is still working, and it says importing the sample file now takes half the time."),
    ("paraphrase two of five", act(Kind.THREAD_STATUS, thread="Rename the orchard sync job", status="interrupted",
                                   summary="Its last reply says two of the five renames were done."),
     "Rename the orchard sync job was interrupted, and it says all five renames were done.",
     "Rename the orchard sync job was interrupted before it finished, and it says it got through two of the five renames."),
    ("paraphrase slider", act(Kind.THREAD_STATUS, thread="Lantern app: fix the dimmer slider", status="working",
                              summary="It says the slider jumps because of a rounding step and it is rewriting that step."),
     "Lantern app: fix the dimmer slider is still working, and it says the slider now works.",
     "Lantern app: fix the dimmer slider is still working. It says a rounding step makes the slider jump, and it is rewriting that step."),
    ("paraphrase waiting", act(Kind.THREAD_STATUS, thread="Show seed packets in a grid", status="idle",
                               summary="It says it is waiting for the layout choice before it goes on."),
     "Show seed packets in a grid is idle.",
     "Show seed packets in a grid is idle, and it says it is waiting on your layout choice before it continues."),
    ("paraphrase kiln page", act(Kind.THREAD_STATUS, thread="Move the kiln log to its own page", status="completed",
                                 summary="It reports that the kiln log now has a page and the links work."),
     "Move the kiln log to its own page finished, and it says it will also add a chart.",
     "Move the kiln log to its own page finished, and it says the kiln log has its own page now, with working links."),
    ("paraphrase done legend", act(Kind.DONE, thread="Add a harbour map legend", status="completed",
                                   summary="It reports that the legend shows five symbols."),
     "Add a harbour map legend finished, as mentioned in the summary.",
     "Add a harbour map legend finished, and it says the legend has five symbols on it."),
]
CASES += PARAPHRASES

# The line said in place of a free reply that offered a taken-back pick again (`cancelled`, op `ask`): it must not name
# or offer either thread, say another action was dropped, or say the thread itself stopped. The pair is the whole-call
# scenario's own made-up pair.
DROPPED = act(Kind.CANCELLED, op="ask")
CASES += [
    ("dropped pick names a thread", DROPPED, "Okay, I'll leave Add a Volume Slider to the Mixer.",
     "Okay, I'll leave that one."),
    ("dropped pick names the other thread", DROPPED, "Okay, I won't look into Add a Volume Slider to Settings.",
     "No problem, I'll drop that question."),
    ("dropped pick offers the choices", DROPPED, "Okay. Did you mean the mixer one or the settings one?",
     "Okay, I won't look into it then."),
    ("dropped pick offers them after leaving it", DROPPED,
     "Okay, I'll leave it, but it's either the mixer one or the settings one.", None),
    ("dropped pick drops a message", DROPPED, "Okay, I won't send that message.", None),
    ("dropped pick drops a stop", DROPPED, "Okay, I won't stop the thread.", None),
    ("dropped pick drops a launch", DROPPED, "Okay, I won't start a new thread.", None),
    ("dropped pick cancels the thread", DROPPED, "Okay, I've cancelled that thread.", None),
    ("dropped pick stops the thread", DROPPED, "Okay, that thread is stopped now.", None),
]

ATTRIBUTION_CASES = [
    ("historical bare status", act(Kind.THREAD_STATUS, thread=T, status="working", summary="It added the helper."),
     f"{T} is still working, and it has added the helper.", None),
    ("historical bare reply", act(Kind.REPLIED, thread=SMOKE, status="running",
                                  reply="I added clamp next to sum. All 12 tests pass."),
     "It added clamp next to sum and the tests pass.", None),
    ("pm replied finished work", act(Kind.REPLIED, thread=SMOKE, status="running",
                                     reply="I added clamp next to sum. All 12 tests pass."),
     "It added clamp, and the tests pass.", None),
]

WORK = [
    ("The clamp helper", "added clamp", "says it added clamp"),
    ("Parser cleanup", "removed the unused parser", "reports removing the unused parser"),
    ("Cache tests", "fixed the flaky test", "says the flaky test is fixed"),
    ("Queue metrics", "found a duplicate counter", "reported finding a duplicate counter"),
    ("Export check", "confirmed the export works", "says it confirmed the export works"),
]
for thread, work, attributed in WORK:
    for said, start in (
        (act(Kind.REPLIED, thread=thread, status="running", reply=f"I {work}."), f"{thread} "),
        (act(Kind.THREAD_STATUS, thread=thread, status="working", summary=f"It {attributed}."),
         f"{thread} is still working, and it "),
        (act(Kind.DONE, thread=thread, status="finished", summary=f"It {attributed}."),
         f"{thread} is finished, and it "),
        (act(Kind.NEEDS_YOU, thread=thread, need="command_approval", summary=f"It {attributed}."),
         f"{thread} needs your approval to run a command. It "),
    ):
        ATTRIBUTION_CASES.append((f"attribution {said.kind.value}", said, f"{start}{work}.", f"{start}{attributed}."))

ATTRIBUTION_CASES.append(("attribution legacy_update",
                          act(Kind.LEGACY_UPDATE, thread="Parser cleanup", text="Parser cleanup removed the unused parser."),
                          "Parser cleanup removed the unused parser.", "Parser cleanup says it removed the unused parser."))

for said, line, question in (
    (act(Kind.REPLIED, thread="The clamp helper", status="running", reply="I added clamp."),
     "The clamp helper says it added clamp.", "What's next?"),
    (act(Kind.THREAD_STATUS, thread="Parser cleanup", status="working"),
     "Parser cleanup is still working.", "Anything else?"),
    (act(Kind.DONE, thread="Cache tests", status="finished"),
     "Cache tests is finished.", "Do you want me to check anything else?"),
    (act(Kind.LAUNCHED, project="Voice sandbox", request="add a clamp helper"),
     "The new thread started in Voice sandbox.", "What should we do next?"),
    (act(Kind.STOPPING, thread="Queue metrics"),
     "I'm stopping Queue metrics now.", "Need anything else?"),
):
    CASES.append((f"unsolicited question {said.kind.value}", said, f"{line} {question}", line))


TAKES = 3


def main(argv=None) -> int:
    """Record each case's lines. A row already recorded for the same line, acts and questions is kept as it is, so a
    change to some questions re-asks only the lines it touches."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--attribution-probe", action="store_true")
    attribution_probe = parser.parse_args(argv).attribution_probe
    fixture, cases = (ATTRIBUTION_FIXTURE, ATTRIBUTION_CASES) if attribution_probe else (FIXTURE, CASES)
    client = LiquidClient(max_attempts=3)
    kept = json.loads(fixture.read_text(encoding="utf-8")) if fixture.exists() else []
    out = []
    for name, said, escape, right in cases:
        for line, should_pass in ((escape, False), (right, True)):
            if line is None:
                continue
            body = request([said], line)
            if attribution_probe:
                questions = body["questions"]
                unrequested = "a1_asks_unrequested"
                keys = list(questions)
                keys.insert(keys.index(unrequested) + 1, "a1_claims_verified")
                questions["a1_claims_verified"] = {**questions[unrequested], "instructions": ATTRIBUTION_QUESTION}
                body["questions"] = {key: questions[key] for key in keys}
            asked = {name: question["instructions"] for name, question in body["questions"].items()}
            takes = TAKES if name.startswith("pm ") else 1
            old = [row for row in kept if row["case"] == name and row["line"] == line and row["acts"] == [said.to_json()]
                   and row.get("asked") == asked]
            for take in range(takes):
                if take < len(old):
                    out.append({**old[take], "case": name, "take": take + 1, "should_pass": should_pass})
                    continue
                record = client.call("decisions", body, label="t3_line_fixture", fresh=True, cache_record=False)
                answers = (record.get("response") or {}).get("answers")
                if record["status"] != 200 or not isinstance(answers, dict) or set(answers) != set(asked):
                    raise RuntimeError(f"Liquid gave an incomplete answer for {name!r}: status {record['status']}")
                out.append({"case": name, "take": take + 1, "acts": [said.to_json()], "line": line,
                            "should_pass": should_pass, "asked": asked, "status": record["status"],
                            "answers": answers,
                            "ms": round(record["timing"]["ms"], 1)})
                print(json.dumps(out[-1]), flush=True)
    fixture.parent.mkdir(parents=True, exist_ok=True)
    fixture.write_bytes((json.dumps(out, indent=2) + "\n").encode())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
