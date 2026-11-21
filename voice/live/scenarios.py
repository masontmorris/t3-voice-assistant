"""Whole-call scenarios, and the gate that runs them.

    python -m voice.live.scenarios gate [--long] [--only ID ...] [--spread N] [--out DIR]
    python -m voice.live.scenarios list

A scenario is a caller who holds a whole call with the real live server (voice/live/server.py: LFM3, the decider, the T3
task service, the checks), in synthetic speech, against a sandbox T3 (voice/live/scenario_world.py) whose threads are
free `Replay (dev)` threads. The caller is a generator: it says a line, is told what the server did in that turn, and
picks its next line from that (it asked which thread, it asked to confirm, it said news by itself). Each scenario
states what must be true of the call, and is scored from the turns' records and the T3 afterwards; a model answers a
stated question only where the verdict depends on what a free reply says (voice/live/scenario_score.py).

`gate` runs every scenario of the default set, or with `--long` the long set too, prints one row each and exits
non-zero when one fails. A check that reproduces a known fault is marked in `KNOWN`: its failure is reported as XFAIL
and does not fail the gate, and when it passes the row says XPASS so the mark can go. It needs the GPU and the hosted decider's
key, so it cannot run in CI: run it under the GPU lease (docs/voice/README.md).

What it cannot catch. The calls are synthetic speech through the server's own TTS and ASR: it knows nothing of a real
voice, a microphone, a room, a speaker's echo or a bad network. T3 is the `Replay (dev)` provider, which plays recorded
turns, so it shows nothing of a real agent's replies, their length, or when it asks for what. The caller's lines are
fixed text per scenario, so a fault that needs another wording passes. A scenario that passes in one run or two says
the fault did not show in those, not that it is gone: the model's output varies, which is why some scenarios take two
runs. A free reply is judged by the hosted decider on one stated question, so a claim it does not read as one passes.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Generator

from datetime import date, datetime

from voice.liquid_decider import client as lc
from voice.live import rehearse as R
from voice.live.rehearse import Say, WaitNews
from voice.live.scenario_score import Call, Judge, Result, Turn, Verdict, median_audio, turn_of
from voice.live.scenario_world import Seed, World, finished, pending
from voice.t3.acts import Kind
from voice.t3.verify import default_decider

VOICE = Path(os.environ.get("VOICE_HOME", Path.home() / "voice"))
SERVER_LEDGER = lc.LEDGER  # the live server keeps its own, at the default place, since only this process moves it
SETTLE_S = 3.0
FALSE_START_P = 0.9  # a reading of "start over" or "will not start" reached 0.43 on the wording above; claims 0.97 or more
CALL_TIMEOUT_S = 420

ASKING = (Kind.CONFIRM_LAUNCH, Kind.CONFIRM_THREAD, Kind.CONFIRM_TELL, Kind.CONFIRM_STOP, Kind.CONFIRM_RESPOND,
          Kind.ASK_WHICH)
ABOUT_A_THREAD = (*ASKING, Kind.THREAD_STATUS, Kind.NO_THREAD, Kind.SAME_TITLE)


# Names the caller says and titles the threads carry, so no two runs of a call share a thread title. Each is taken once.
SINGLES = (
    ("save slots", "Tidy the Save Slots"), ("jump timing", "Fix the Jump Timing"),
    ("level loading", "Speed Up Level Loading"), ("controller rumble", "Add Controller Rumble"),
    ("audio settings", "Clean Up the Audio Settings"), ("main menu", "Polish the Main Menu"),
    ("sprite draws", "Batch the Sprite Draws"), ("camera shake", "Tune the Camera Shake"),
    ("inventory icons", "Sort the Inventory Icons"), ("save screen", "Speed Up the Save Screen"),
    ("shader cache", "Tidy the Shader Cache"), ("map loader", "Speed Up the Map Loader"),
    ("photo mode", "Add a Photo Mode"), ("minimap zoom", "Fix the Minimap Zoom"),
    ("audio mixer", "Profile the Audio Mixer"), ("intro video", "Trim the Intro Video"),
    ("pause screen blur", "Fix the Pause Screen Blur"), ("footstep sounds", "Fix the Footstep Sounds"),
    ("controller remapping", "Add Controller Remapping"), ("startup logo", "Trim the Startup Logo"),
    ("crash on exit", "Fix the Crash on Exit"), ("asset loader", "Profile the Asset Loader"),
    ("log output", "Clean Up Log Output"), ("shader warmup", "Speed Up Shader Warmup"))
TWINS = (
    ("volume slider", "Add a Volume Slider", "Add volume slider to the settings page"),
    ("pause menu", "Fix the Pause Menu", "Fix pause menu in the sandbox project"),
    ("retry button", "Add a Retry Button", "Add retry button to the error screen"),
    ("config keys", "Rename the Config Keys", "Rename config keys in the sandbox project"),
    ("mute toggle", "Add a Mute Toggle", "Add mute toggle next to volume"),
    ("night mode", "Add a Night Mode", "Add night mode to the sandbox project"))
CITIES = ("Denver", "Seattle", "Chicago", "Portland", "Atlanta", "Boston", "Austin", "Phoenix")


class Pool:
    """Hands out each name, twin pair, request and city once, so a repeated run never meets a thread of an earlier one."""

    def __init__(self):
        self.used = {"single": 0, "twin": 0, "request": 0, "city": 0}

    def _next(self, kind: str, items: tuple):
        i = self.used[kind]
        if i >= len(items):
            raise SystemExit(f"the scenario pool ran out of {kind}s: raise its size or run fewer scenarios")
        self.used[kind] += 1
        return items[i]

    def single(self) -> tuple[str, str]:
        return self._next("single", SINGLES)

    def twin(self) -> tuple[str, str, str]:
        return self._next("twin", TWINS)

    def request(self) -> str:
        return self._next("request", R.REQUESTS)

    def city(self) -> str:
        return self._next("city", CITIES)


@dataclass
class Plan:
    """One run of a scenario: the threads that exist first, what the caller may say, what the caller does, what
    threads the call starts run on, and what must be true afterwards."""

    seeds: tuple[Seed, ...]
    lines: dict[str, str]
    script: Generator
    check: Callable[[Call], list[Verdict]]
    launch: str = "todo-list"
    notes: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Scenario:
    id: str
    what: str
    build: Callable[[Pool], Plan]
    runs: int = 1
    passes: int = 1
    long: bool = False
    fresh: bool = False  # a scenario that starts threads runs on a T3 no earlier scenario touched


def ok(check: str, condition: bool, why: str, turn: int | None = None) -> Verdict:
    return Verdict(check, bool(condition), "" if condition else why, None if condition else turn)


def requires(verdict: Verdict, condition: bool, why: str) -> Verdict:
    return verdict if condition else Verdict(verdict.check, False, why, verdict.turn, precondition=True)


def nth(turns: tuple[Turn, ...], i: int) -> Turn | None:
    return turns[i] if i < len(turns) else None


def has_reply(turn: Turn) -> bool:
    return any(delivery in (None, "sent") and text for _, delivery, text in turn.replies)


def no_silent_turn(call: Call) -> Verdict:
    """A turn about a thread ends with something said: a T3 line that was worded and refused, with nothing else heard,
    leaves the caller talking to no one. A turn the caller's next words took over is not one."""
    silent = [t for t in call.caller_turns() if t.t3 and not t.said_kinds and not t.free and not t.cancelled]
    return ok("no thread turn ends in silence", not silent,
              f"turn {silent[0].index} tried {[e['kinds'] for e in silent[0].t3]} and nothing was said" if silent else "",
              silent[0].index if silent else None)


def no_false_start(call: Call) -> Verdict:
    """No free reply says a thread was started, or is being started, when T3 holds none the call made."""
    if call.world["made"]:
        return ok("no free reply claims a thread that does not exist", True, "")
    for t in call.caller_turns():
        for line in t.free:
            if call.judge.yes("Does the reply say that the assistant has started a coding thread or is starting one "
                              "now? Starting a conversation over, or declining to start a thread, does not count.",
                              line, "The reply was given to a person who asked a voice assistant for a coding thread.",
                              at=FALSE_START_P):
                return ok("no free reply claims a thread that does not exist", False,
                          f"turn {t.index} said a thread was starting and T3 holds none", t.index)
    return ok("no free reply claims a thread that does not exist", True, "")


# The caller withdraws a question the assistant asked (which thread, or whether it is the one), changes the subject and
# asks to start over: nothing after the withdrawal may put a thread question again.
def withdrawn(pool: Pool) -> Plan:
    spoken, one, two = pool.twin()
    other = pool.single()[1]
    lines = {"ask": f"How is the {spoken} thread going?", "never": "Never mind.",
             "topic": "Can you tell me something interesting about octopuses?", "restart": "Can we start over?",
             "greet": "Okay. Hello again, how are you?"}

    def script():
        for line in lines:
            yield Say(line)

    def check(call: Call) -> list[Verdict]:
        turns = call.caller_turns()
        after = turns[1:]
        offers = [t for t in after if t.said_kinds & set(ABOUT_A_THREAD)]
        asked = ok("asked a thread question", turns and turns[0].said(*ASKING),
                   "the assistant asked no question about a thread, so there was nothing to withdraw", 0)
        questions = [t for t in offers if t.said(Kind.ASK_WHICH)]
        other_lines = [t for t in offers if t.said_kinds & (set(ABOUT_A_THREAD) - {Kind.ASK_WHICH})]
        out = [ok("no which-thread question after the withdrawal", not questions, "asked which thread again",
                  questions[0].index if questions else None),
               ok("no other thread line after the withdrawal", not other_lines, "said another line about a thread",
                  other_lines[0].index if other_lines else None)]
        repeated = None
        for t in after:
            for line in t.free:
                if call.judge.yes("Does the reply ask the person to choose between threads, or list threads as "
                                  "options to choose from?", line, f"The threads are {one} and {two}."):
                    repeated = (t.index, line)
        out.append(ok("no free reply offers the old choices", repeated is None,
                      f"turn {repeated[0]} offered the choices again" if repeated else "", repeated[0] if repeated else None))
        silent = [t for t in after if t.cancelled != "continued" and not has_reply(t) and not t.t3]
        out.append(ok("every completed turn was answered", not silent, "a turn got no reply",
                      silent[0].index if silent else None))
        return [asked, *(requires(v, asked.ok and bool(after), "precondition: no thread question was withdrawn")
                         for v in out)]

    return Plan((finished(one), finished(two), finished(other)), lines, script(), check)


# A turn the decider reads as conversation must not get a free reply that claims something T3 does not support.
def claims_approval(call: Call, titles: str, turns: tuple[Turn, ...]) -> list[Verdict]:
    """After the caller asked for an approval T3 has no way to give: a reply must not say it went through while the
    request is still pending. If a later change lets the voice approve, the request is resolved and a claim is true."""
    still = [v for v in call.world["seeded"].values() if v["waiting_on"] == "approval"]
    still += [v for v in call.world["made"].values() if v["waiting_on"] == "approval"]
    claimed = None
    for t in turns:
        for line in t.free:
            if call.judge.yes("Does the reply say that an approval or permission request was approved, allowed or "
                              "granted, or that it went through?", line, f"The reply is about the thread {titles}."):
                claimed = (t.index, line)
    said = [t for t in call.caller_turns() if t.said(Kind.RESPONDED)]
    return [ok("no approval claimed while T3 still holds it", not (claimed and still),
               f"turn {claimed[0]} claimed an approval that is still pending" if claimed else "",
               claimed[0] if claimed else None),
            ok("a response said done is done in T3", not (said and still),
               f"turn {said[0].index} said the request was answered and T3 still holds it" if said else "",
               said[0].index if said else None)]


def approval(pool: Pool) -> Plan:
    spoken, title = pool.single()
    lines = {"ask": f"Is the {spoken} thread waiting on me for anything?",
             "approve": f"Please approve the request on the {spoken} thread.", "yes": "Yeah, go ahead.",
             "check": "So is it approved now?"}

    def script():
        yield Say("ask")
        heard = yield Say("approve")
        if heard.turns and heard.turns[-1].said_kinds & set(ASKING):
            yield Say("yes")
        yield Say("check")

    def check(call: Call) -> list[Verdict]:
        turns = call.caller_turns()[1:]
        return [requires(v, len(turns) >= 2, "precondition: the approval and follow-up turns were not completed")
                for v in claims_approval(call, title, turns)]

    return Plan((pending(title),), lines, script(), check)


# Every turn about a thread ends in a checked line that was said: not refused, not run on into the instruction.
def thread_lines(pool: Pool) -> Plan:
    (one, first), (two, second) = pool.single(), pool.single()
    lines = {"ask": f"How is the {one} thread going?", "more": "Give me more detail on that.",
             "ask2": f"And how's the {two} thread going?", "thanks": "Okay, thanks."}

    def script():
        for line in lines:
            yield Say(line)

    def check(call: Call) -> list[Verdict]:
        turns = call.caller_turns()
        out = []
        for i, name in enumerate(("status", "more detail", "second thread")):
            t = nth(turns, i)
            out.append(ok(f"{name}: a checked line was said",
                          t is not None and t.said(Kind.THREAD_STATUS, Kind.CONFIRM_THREAD, Kind.ASK_WHICH),
                          "the turn said no checked T3 line" + (f" (tried {[e['kinds'] for e in t.t3]})" if t and t.t3
                                                                else ", T3 never saw it"), t.index if t else i))
        extra = [t for t in turns[:3] if t.said(Kind.THREAD_STATUS) and t.free]
        out.append(ok("no free line beside a status line", not extra,
                      "a free reply came with the checked line", extra[0].index if extra else None))
        return out

    return Plan((finished(first), finished(second)), lines, script(), check)


# A short answer to the assistant's own confirm question is the answer.
def short_yes(pool: Pool) -> Plan:
    requests = [pool.request() for _ in range(2)]
    yeses = ("Yeah.", "Sure.")
    lines = {**{f"req{i}": r for i, r in enumerate(requests)}, **{f"yes{i}": y for i, y in enumerate(yeses)}}
    notes = {"asked": [], "answered": []}

    def script():
        for i in range(2):
            heard = yield Say(f"req{i}")
            confirm = heard.turns and heard.turns[-1].said(Kind.CONFIRM_LAUNCH)
            notes["asked"].append(bool(confirm))
            if confirm:
                heard = yield Say(f"yes{i}")
                notes["answered"].append(bool(heard.turns and heard.turns[-1].said(Kind.LAUNCHED)))

    def check(call: Call) -> list[Verdict]:
        made = len(call.world["made"])
        said = [t for t in call.caller_turns() if t.said(Kind.LAUNCHED)]
        confirmed = notes["asked"] == [True, True]
        return [ok("each request was confirmed back", confirmed, f"asked to confirm {sum(notes['asked'])} of 2 requests"),
                requires(ok("each yes to a confirmation started a thread", made >= 2, f"{made} threads exist after two yeses"),
                         confirmed, "precondition: both confirmations must be asked before the caller can say yes"),
                requires(ok("each started thread was acknowledged", sum(notes["answered"]) >= made,
                            f"{sum(notes['answered'])} of {made} thread starts were acknowledged"),
                         confirmed, "precondition: both confirmations must be asked before the caller can say yes"),
                ok("no extra thread was started", made <= 2, f"{made} threads exist after two requests"),
                ok("no line says a thread started that did not", len(said) <= made,
                   f"{len(said)} lines said a thread started, {made} exist"), no_false_start(call)]

    return Plan((), lines, script(), check, notes=notes)


def forecast_days(first: dict | None, second: dict | None) -> int | None:
    """Days from the first forecast's date to the second's, from the dates the lookups returned."""
    try:
        return (date.fromisoformat(second["detail"]["days"][0]["date"])
                - date.fromisoformat(first["detail"]["days"][0]["date"])).days
    except (TypeError, KeyError, IndexError, ValueError):
        return None


# A follow-up that depends on the last turn keeps its context: "tomorrow" then "a week from today" is six days on.
def follow_up(pool: Pool) -> Plan:
    city = pool.city()
    lines = {"first": f"What's the weather in {city} tomorrow?", "follow": "What about a week from today?"}

    def script():
        yield Say("first")
        yield Say("follow")

    def check(call: Call) -> list[Verdict]:
        turns = call.caller_turns()
        weather = [[t for t in turn.lookups if t.get("tool") == "weather"] for turn in turns]
        first_run = weather[0][0] if turns and weather[0] else None
        second_run = weather[1][0] if len(turns) > 1 and weather[1] else None
        first, second = (r["arguments"] if r else None for r in (first_run, second_run))
        out = [ok("the first question ran a forecast for the city", first is not None
                   and city.lower() in str(first.get("place", "")).lower(), f"the forecast ran with {first}", 0),
                ok("the follow-up kept the place", second is not None
                   and city.lower() in str(second.get("place", "")).lower(),
                   f"the follow-up ran {second}" if second else "the follow-up ran no forecast", 1)]
        days = forecast_days(first_run, second_run)
        fell_back_to_today = days == -1 and second.get("when") == "today"
        ready = all(v.ok for v in out) and days is not None
        out.extend(requires(v, ready, "precondition: both forecasts must return dates for the requested city") for v in (
            ok("the follow-up did not fall back to today", not fell_back_to_today, "the tool ran today's forecast", 1),
            ok("the follow-up's forecast is for a week from today", days == 6 or fell_back_to_today,
               f"the follow-up's forecast is {days} days after the first, not 6", 1)))
        return out

    return Plan((), lines, script(), check)


# A thread the caller started needs permission by itself: the assistant says so unprompted, once, and does not claim
# the approval it cannot give.
def news_permission(pool: Pool) -> Plan:
    request = pool.request()
    lines = {"request": request, "yes": "Yeah.", "approve": "Okay, go ahead and approve it.",
             "check": "So is it approved now?"}
    notes = {"confirmed": False}

    def script():
        heard = yield Say("request")
        notes["confirmed"] = bool(heard.turns and heard.turns[-1].said(Kind.CONFIRM_LAUNCH))
        if notes["confirmed"]:
            yield Say("yes")
            heard = yield WaitNews(100)
            notes["timed_out"] = heard.timed_out
            heard = yield Say("approve")
            if heard.turns and heard.turns[-1].said_kinds & set(ASKING):
                yield Say("yes")
            yield Say("check")

    def check(call: Call) -> list[Verdict]:
        news = [t for t in call.news() if t.said(Kind.NEEDS_YOU)]
        refused = any(Kind.NEEDS_YOU in entry["kinds"] for t in call.news() for entry in t.refused)
        made = call.world["made"]
        out = launch_checks(call, notes)
        waited = "timed_out" in notes and len(made) == 1
        news_checks = [ok("permission news was said or reached the reply checks", bool(news) or refused,
                  "no needs-you line was said by itself" + (" (the wait ran out)" if notes.get("timed_out") else ""),
                  call.news()[0].index if call.news() else None),
               ok("permission news was not lost to a refused line", bool(news) or not refused,
                  "the checks refused the only attempt at permission news"),
               ok("no duplicate permission news", len(news) <= 1, f"{len(news)} needs-you lines were said by themselves",
                  news[1].index if len(news) > 1 else None)]
        titles = ", ".join(v["title"] for v in made.values())
        approval_turns = call.caller_turns()[2:]
        return [*out, *(requires(v, waited, "precondition: no wait for news about a started thread") for v in news_checks),
                *(requires(v, waited and len(approval_turns) >= 2, "precondition: no completed approval exchange for a started thread")
                  for v in claims_approval(call, titles, approval_turns))]

    return Plan((), lines, script(), check, launch="approval", notes=notes)


# The caller speaks over a reply: it stops, and the new words are answered.
def speak_over(pool: Pool) -> Plan:
    spoken, title = pool.single()
    lines = {"ask": f"How is the {spoken} thread going?", "over": "Actually, never mind that. What's two plus two?"}

    def script():
        yield Say("ask", over=("over", 1500))

    def check(call: Call) -> list[Verdict]:
        turns = call.caller_turns()
        last = turns[-1] if turns else None
        answered = len(turns) > 1 and has_reply(last)
        refused = len(turns) > 1 and bool(last.refused) and not last.said_kinds and not last.cancelled
        return [ok("the first reply was stopped", any(t.barge_in or t.cancelled == "barge_in" for t in turns),
                   "the caller speaking over the reply was not taken as a barge-in", 0),
                ok("the new words were answered or reached the T3 reply checks", answered or refused,
                   "the second turn got no reply and tried no refused T3 line", last.index if last else None),
                ok("the second turn was not left silent by refused T3 lines", answered or not refused,
                   "the T3 reply checks refused every line", last.index if last else None)]

    return Plan((finished(title),), lines, script(), check)


def launch_checks(call: Call, notes: dict) -> list[Verdict]:
    made = len(call.world["made"])
    return [ok("the launch confirmation was asked", notes["confirmed"], "no launch confirmation was said", 0),
            requires(ok("the yes started a thread", made > 0, "no thread exists after the yes", 1), notes["confirmed"],
                     "precondition: no launch confirmation was asked"),
            ok("no extra thread was started", made <= 1, f"{made} threads exist after one request"),
            no_false_start(call)]


# The long set. A thread the caller started finishes by itself and the assistant says so, once.
def news_done(pool: Pool) -> Plan:
    request = pool.request()
    lines = {"request": request, "yes": "Yeah.", "thanks": "Okay, thanks."}
    notes = {"confirmed": False}

    def script():
        heard = yield Say("request")
        notes["confirmed"] = bool(heard.turns and heard.turns[-1].said(Kind.CONFIRM_LAUNCH))
        if notes["confirmed"]:
            yield Say("yes")
            heard = yield WaitNews(100)
            notes["timed_out"] = heard.timed_out
            yield Say("thanks")

    def check(call: Call) -> list[Verdict]:
        made = call.world["made"]
        news = [t for t in call.news() if t.said(Kind.DONE, Kind.REPLIED)]
        finished_by_itself = [v for v in made.values() if v["activity"] == "finished"]
        out = [ok("the thread finished in T3", len(finished_by_itself) == 1, f"the thread is {list(made.values())}"),
                ok("the assistant said it finished, unprompted", bool(news),
                   "no done or replied line was said by itself" + (" (the wait ran out)" if notes.get("timed_out")
                                                                   else ""),
                   call.news()[0].index if call.news() else None),
                ok("no duplicate done notices", sum(t.said(Kind.DONE) for t in call.news()) <= 1,
                   "the end of the run was said more than once")]
        waited = "timed_out" in notes and len(made) == 1
        return launch_checks(call, notes) + [requires(v, waited, "precondition: no wait for news about a started thread")
                                             for v in out]

    return Plan((), lines, script(), check, notes=notes)


# A message to a thread, confirmed with a short yes, reaches the thread.
def short_yes_tell(pool: Pool) -> Plan:
    spoken, title = pool.single()
    lines = {"tell": f"Tell the {spoken} thread to also add a unit test.", "yes": "Yeah."}
    notes = {"asked": False}

    def script():
        heard = yield Say("tell")
        notes["asked"] = bool(heard.turns and heard.turns[-1].said(Kind.CONFIRM_TELL))
        if notes["asked"]:
            yield Say("yes")

    def check(call: Call) -> list[Verdict]:
        turns = call.caller_turns()
        told = [t for t in turns[1:] if t.said(Kind.SENT_FOLLOWUP, Kind.HELD_FOLLOWUP, Kind.TELL_PENDING)]
        asked = ok("the message was confirmed back", notes["asked"], "no confirm_tell question was said", 0)
        taken = ok("the yes was taken", bool(told), "the yes was not answered with the message going", 1)
        reached = asked.ok and len(turns) >= 2
        delivered = ok("the thread got the message", call.world["seeded"].get(title, {}).get("new_run", False),
                       "T3 shows no new run on the thread", 1)
        return [asked, *(requires(v, reached, "precondition: the caller did not answer a message confirmation")
                         for v in (taken, delivered))]

    return Plan((finished(title),), lines, script(), check, notes=notes)


# A launch confirmation withdrawn starts nothing, and is not asked again.
def withdrawn_launch(pool: Pool) -> Plan:
    request = pool.request()
    lines = {"request": request, "never": "Never mind, don't start anything.", "restart": "Can we start over?",
             "hi": "Hi, how are you?"}

    def script():
        for line in lines:
            yield Say(line)

    def check(call: Call) -> list[Verdict]:
        turns = call.caller_turns()
        after = turns[1:]
        asked_again = [t for t in after if t.said(Kind.CONFIRM_LAUNCH)]
        other_questions = [t for t in after if t.said_kinds & (set(ASKING) - {Kind.CONFIRM_LAUNCH})]
        asked = ok("the launch was asked about", bool(turns) and turns[0].said(Kind.CONFIRM_LAUNCH, Kind.ASK_WHICH),
                   "no launch question was said, so nothing was withdrawn", 0)
        return [asked,
                ok("nothing was started", not call.world["made"], f"{len(call.world['made'])} threads exist"),
                ok("no launch line after the withdrawal", not any(t.said(Kind.LAUNCHED, Kind.LAUNCH_PENDING)
                                                                  for t in after), "a launch was said after it"),
                requires(ok("the question was not asked again", not asked_again,
                   f"turn {asked_again[0].index} asked {sorted(asked_again[0].said_kinds)}" if asked_again else "",
                   asked_again[0].index if asked_again else None), asked.ok and bool(after),
                         "precondition: no launch question was withdrawn"),
                ok("no different thread question was asked", not other_questions, "asked another thread question",
                   other_questions[0].index if other_questions else None), no_false_start(call)]

    return Plan((), lines, script(), check)


SCENARIOS = (
    Scenario("speak-over", "the caller speaking over a reply stops it and is answered", speak_over, 2, 1),
    Scenario("approval-claim", "a free reply never says a pending approval went through", approval),
    Scenario("thread-lines", "every thread question ends in a checked line that is said", thread_lines),
    Scenario("follow-up", "a follow-up keeps the last question's context", follow_up),
    Scenario("withdrawn-question", "a withdrawn which-thread question is not offered again", withdrawn),
    # The scenarios that start threads come after, each run on a T3 and a live server of its own.
    Scenario("news-permission", "a started thread's need for permission is said unprompted, once, and not approved",
             news_permission, 2, 1, fresh=True),
    Scenario("short-yes", "a short yes answers the confirm question", short_yes, 2, 1, fresh=True),
    Scenario("news-done", "a started thread's end is said unprompted, once", news_done, 2, 1, long=True, fresh=True),
    Scenario("short-yes-tell", "a short yes sends a message to a thread", short_yes_tell, 2, 1, long=True),
    Scenario("withdrawn-launch", "a withdrawn launch question starts nothing and is not asked again",
             withdrawn_launch, 2, 1, long=True, fresh=True))
# Checks that reproduce a fault nobody has fixed yet, as (scenario, check): the gate expects them to fail, says XFAIL, and
# fails on everything else. Take a mark out when its fix lands and the row says XPASS.
DISPATCH_YES = ("a short yes to the launch confirmation is not always taken; a "
                "no_thread line beside the confirmation clears the question; docs/voice/spoken-acts.md")
KNOWN: dict[tuple[str, str], str] = {
    ("news-permission", "the yes started a thread"): DISPATCH_YES,
    ("news-done", "the yes started a thread"): DISPATCH_YES,
    ("short-yes-tell", "the yes was taken"):
        "a short yes to the confirmation of a message is not always taken (1 of 2 baseline runs said nothing)",
    ("withdrawn-launch", "the question was not asked again"):
        "after 'never mind' the launch confirmation is put again in some runs (1 of 2 in the baseline)",
    ("follow-up", "the follow-up did not fall back to today"):
        "'a week from today' runs as today: the forecast tool's `when` takes a weekday, today or tomorrow",
    ("withdrawn-question", "no free reply offers the old choices"):
        "after the fix in #90, 1 of 3 baseline runs still offered the old choices in a free reply",
    ("news-permission", "permission news was not lost to a refused line"):
        "the needs-you line is refused by its checks in some runs (1 of 3 in the baseline) and nothing is said",
    ("short-yes", "each yes to a confirmation started a thread"): DISPATCH_YES}


def collect(server: Path, seen: list[int]) -> tuple[Turn, ...]:
    """The call's turns in order, read from the files the server wrote, after it has had time to finish them."""
    time.sleep(SETTLE_S)
    return tuple(read_turn(server, i, n) for i, n in enumerate(sorted(set(seen))))


def read_turn(server: Path, index: int, n: int) -> Turn:
    path = server / f"turn_{n:03d}.json"
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(record, dict) or record.get("clip") != path.stem:
            raise ValueError("record does not identify the completed turn")
        if record.get("error"):
            raise ValueError(f"server reported {record['error']}")
        return turn_of(index, record, R.entries)
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        raise RuntimeError(f"cannot score {path.name}: {exc}") from exc


def run_once(scenario: Scenario, run: int, world: World, rig, judge: Judge, pool: Pool, out: Path,
             lfm_port: int) -> Result:
    started, name = time.time(), f"{scenario.id}-{run}"
    work = out / name
    try:
        plan = scenario.build(pool)
        work.mkdir(parents=True, exist_ok=True)
        seeded = world.seed(plan.seeds)
        world.configure(plan.launch)
        before = set(world.titles())
        text = work / "lines.txt"
        text.write_text("\n".join(plan.lines.values()) + "\n", encoding="utf-8")
        wavs = rig.render(text, work / "clips", lfm_port)
        clips = dict(zip(plan.lines, wavs))
        seen = []
        server = rig.out / "server"

        def load(n):
            seen.append(n)
            return read_turn(server, len(seen) - 1, n)

        asked = len(judge.asked)
        R.hold_call(rig, plan.script, clips, load, work / "client", CALL_TIMEOUT_S)
        turns = collect(server, seen)
        call = Call(turns, world.snapshot(seeded, before), judge, plan.notes)
        verdicts = (*plan.check(call), no_silent_turn(call))
        (work / "call.json").write_text(json.dumps({"turns": [t.__dict__ for t in turns], "world": call.world,
                                                    "judge": judge.asked[asked:]}, indent=1, default=str),
                                        encoding="utf-8")
        time.sleep(R.CALL_GAP_S)
        return Result(scenario.id, run, verdicts, tuple(t.first_audio_s for t in turns if t.first_audio_s),
                      sum(t.decider_calls for t in turns), len(judge.asked) - asked, time.time() - started,
                      window=(started, time.time()))
    except (Exception, SystemExit) as exc:
        return Result(scenario.id, run, (), (), 0, 0, time.time() - started, f"{type(exc).__name__}: {exc}",
                      (started, time.time()))


def ledger(out: Path) -> list[tuple[float, float]]:
    """Every hosted decider attempt the run made, from its ledger, as (when, dollars): the call's own, the T3 line
    checks' and the scorer's."""
    found = []
    files = [out / "decider-cache" / "calls.jsonl", *out.glob("s*/decider-cache/calls.jsonl"), SERVER_LEDGER]
    for entry in (e for path in files for e in lc.entries(path)):
        try:
            when = datetime.fromisoformat(entry["at"].replace("Z", "+00:00")).timestamp()
        except (KeyError, ValueError):
            continue
        found.append((when, float(entry.get("cost") or 0.0)))
    return found


def unexpected(result: Result) -> list[Verdict]:
    return [v for v in result.failed if v.precondition or (result.scenario, v.check) not in KNOWN]


def rows(results: dict[str, list[Result]], scenarios: list[Scenario], attempts=()) -> tuple[list[list[str]], bool]:
    """One row per scenario, and whether the gate is green. A run passes when no check failed that `KNOWN` does not
    name. A scenario passes when `passes` of its runs do, and says XFAIL while a named check still fails in it and XPASS
    when every named check passed, so its mark can go."""
    table, green = [], True
    for sc in scenarios:
        runs = results.get(sc.id, [])
        if not runs:
            continue
        marked = {check for (sid, check) in KNOWN if sid == sc.id}
        failures = [unexpected(r) for r in runs]
        good = sum(r.error is None and not u for r, u in zip(runs, failures))
        still = {v.check for r in runs for v in r.failed if not v.precondition and v.check in marked}
        error = next((r.error for r in runs if r.error is not None), None)
        if error is not None:
            verdict, green = "ERROR", False
        elif good >= sc.passes:
            verdict = "XFAIL" if still else "XPASS" if marked else "PASS"
        else:
            verdict, green = "FAIL", False
        first = next((u for u in failures if u), [])
        why = error or "; ".join(v.check + (f" (turn {v.turn})" if v.turn is not None else "") + f": {v.why}"
                                 for v in first)
        if not why and still:
            why = "known: " + ", ".join(sorted(still))
        table.append([sc.id, verdict, f"{good}/{len(runs)}", f"need {sc.passes}", str(median_audio(runs) or "-"),
                      str(sum(lo <= when <= hi for r in runs for lo, hi in [r.window] for when, _ in attempts)),
                      why[:180]])
    return table, green


def show(table: list[list[str]]) -> None:
    head = ["scenario", "result", "passed", "rule", "audio p50 s", "decider calls", "first failure"]
    widths = [max(len(str(r[i])) for r in [head, *table]) for i in range(len(head))]
    for r in [head, *table]:
        print("  ".join(str(c).ljust(widths[i]) for i, c in enumerate(r)).rstrip(), flush=True)


class Session:
    """A sandbox T3 and a live server on it. The server does not find a T3 that restarted under it, so a scenario that
    starts threads gets a session of its own: it meets no thread an earlier scenario made, and its own threads are
    gone before the next scenario."""

    def __init__(self, world: World, out: Path, args):
        self.world, self.out, self.args, self.rig = world, out, args, None

    def __enter__(self):
        self.world.up()
        env = {"VOICE_T3_ORIGIN": self.world.origin, "VOICE_T3_TOKEN_FILE": str(self.world.token_file)}
        self.rig = R.Rig(self.out, self.args.port, self.args.max_minutes, ["--real-seam", "--summaries"],
                         ["--decider", "liquid", "--local-text", "on", "--lfm-port", str(self.args.lfm_port)], env)
        self.rig.__enter__()
        return self

    def __exit__(self, *exc):
        errors = []

        def stop(name, close):
            try:
                close()
            except (Exception, SystemExit) as failure:
                errors.append(f"{name}: {type(failure).__name__}: {failure}")

        try:
            if self.rig is not None:
                stop("live server", self.rig.__exit__)
        finally:
            stop("sandbox T3", self.world.down)
        if errors:
            raise RuntimeError("teardown failed: " + "; ".join(errors))


def gate(args) -> int:
    chosen = [s for s in SCENARIOS if (args.long or not s.long) and (not args.only or s.id in args.only)]
    if args.spread:
        chosen = [Scenario(**{**s.__dict__, "runs": args.spread, "passes": min(s.passes, args.spread)}) for s in chosen]
    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    lc.use_cache(out / "decider-cache")
    world, judge, pool = World(), Judge(), Pool()
    if default_decider() is None:
        print("no decider key: set VOICE_ENV_FILE", file=sys.stderr)
        return 2
    started, results = time.time(), {}
    session, owner, count, after_launches = None, None, 0, False

    def close_session():
        nonlocal session
        if session is None:
            return True
        closing, session = session, None
        try:
            closing.__exit__()
        except (Exception, SystemExit) as exc:
            runs = results[owner]
            if not runs:
                runs.append(Result(owner, 1, (), (), 0, 0, 0, str(exc)))
                return False
            last = runs[-1]
            runs[-1] = replace(last, error=f"{last.error}; {exc}" if last.error else str(exc))
            return False
        return True

    try:
        for sc in chosen:
            results[sc.id] = []
            for run in range(1, sc.runs + 1):
                if session is None or sc.fresh or after_launches:
                    if not close_session():
                        break
                    count += 1
                    session = Session(world, out / f"s{count}", args)
                    owner = sc.id
                    try:
                        session.__enter__()
                    except (Exception, SystemExit) as exc:
                        results[sc.id].append(Result(sc.id, run, (), (), 0, 0, 0, f"startup failed: {exc}"))
                        break
                after_launches = sc.fresh
                result = run_once(sc, run, world, session.rig, judge, pool, out, args.lfm_port)
                results[sc.id].append(result)
                owner = sc.id
                status = "ERROR" if result.error is not None else "FAIL" if unexpected(result) else "pass"
                print(f"{sc.id} run {run}: {status} in {result.seconds:.0f} s", flush=True)
                done = sum(not unexpected(r) for r in results[sc.id] if r.error is None)
                if result.error is not None or (not args.spread and
                                                (done >= sc.passes or run - done > sc.runs - sc.passes)):
                    break
            if session is None or (results[sc.id] and results[sc.id][-1].error is not None):
                if session is not None and close_session():
                    continue
                break
    finally:
        close_session()
    attempts = [a for a in ledger(out) if a[0] >= started]
    table, green = rows(results, chosen, attempts)
    print(flush=True)
    show(table)
    print(f"\n{'green' if green else 'RED'}: {len(chosen)} scenarios in {(time.time() - started) / 60:.1f} min, "
          f"{len(attempts)} hosted decider calls (${sum(cost for _, cost in attempts):.3f}), {len(judge.asked)} of "
          f"them scoring", flush=True)
    (out / "results.json").write_text(json.dumps({sid: [r.__dict__ | {"verdicts": [v.__dict__ for v in r.verdicts]}
                                                       for r in rs] for sid, rs in results.items()}, indent=1,
                                                 default=str), encoding="utf-8")
    return 0 if green else 1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mode", choices=["gate", "list"])
    parser.add_argument("--out", type=Path, default=VOICE / "evidence" / "scenarios" / time.strftime("%Y%m%d-%H%M%S"))
    parser.add_argument("--long", action="store_true", help="the long set too")
    parser.add_argument("--only", nargs="+", help="scenario ids")
    parser.add_argument("--spread", type=int, help="run every scenario this many times unless an attempt errors")
    parser.add_argument("--port", type=int, default=8771)
    parser.add_argument("--lfm-port", type=int, default=8095)
    parser.add_argument("--max-minutes", type=float, default=24.0)
    args = parser.parse_args()
    if args.mode == "list":
        for s in SCENARIOS:
            print(f"{s.id:22} {'long' if s.long else 'default':8} {s.passes} of {s.runs}  {s.what}")
        return
    sys.exit(gate(args))


if __name__ == "__main__":
    main()
