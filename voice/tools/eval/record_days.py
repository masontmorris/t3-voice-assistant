"""Record the hosted decider's reading of the day and place a lookup runs for, so
voice/tools/tests/test_day_decisions.py replays real answers.

    VOICE_ENV_FILE=<env file> python -m voice.tools.eval.record_days --label after [--samples 3]

Each case is a made-up request, the lookup the call made before it when there is one, and the place and day the lookup
must run for. A case runs as the end of a live turn does (`run.close_with_decider` with the `--t3 on` tool options and
the gate), the dispatched call is built by `Registry.dispatch` from the decider's tool answer and reading, and the
call's day is resolved over a made-up 16-day forecast that starts on `TODAY`. It passes when the place is the case's
(none when the case says the voice must ask) and the first day it picks is the case's day; a case past the forecast
passes when the lookup picks no day. A clarification case is a person's answer to "which city?", read by the decider,
and passes when the place it gives is the case's, or none for words that are not an answer.

`SEEN` was looked at while the decider text was written. `HELD` was written before any run of the shipped text and was
run once on it: report both. `--label before` records the same cases on the code this change started from, and
`--label after` the shipped code. The fixture keeps each recorded answer under a hash of the request it answered and
the sample's index, so a changed request text finds no answer and the test says to record again.
"""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

from voice.live.t3_tasks import T3Registry
from voice.prototype import run
from voice.segmentation import contract
from voice.segmentation.contract import TranscriptUpdate
from voice.segmentation.gate import PLAN_A
from voice.segmentation.hybrid import HybridSegmenter
from voice.tools import lookups, open_meteo, registry
from voice.tools.registry import ToolCall, ToolChoice

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "day_decisions.json"
TOOLS = T3Registry(registry.build(None, None, None)).choices()
RULES = registry.build(None, None, None)
TODAY = date(2026, 11, 23)
FORECAST_DAYS = 16
FORECAST = [open_meteo.Day(TODAY + timedelta(days=i), 50.0, 0) for i in range(FORECAST_DAYS)]
SAMPLES = 3


def earlier(place, when, request):
    return {"ref": "turn_001:task_1", "instruction": request, "side_effect": False,
            "call": {"tool": "weather", "arguments": {"place": place, "when": when}}}


DENVER_TOMORROW = earlier("Denver", "tomorrow", "What's the weather in Denver tomorrow?")
SEATTLE_TODAY = earlier("Seattle", "today", "How's the weather in Seattle today?")
BOSTON_FRIDAY = earlier("Boston", "friday", "Will it rain in Boston on Friday?")
AUSTIN_WEEKEND = earlier("Austin", "weekend", "What's the weather in Austin this weekend?")
FARGO_TOMORROW = earlier("Fargo", "tomorrow", "How cold will it get in Fargo tomorrow?")
TULSA_TODAY = earlier("Tulsa", "today", "Is it going to be windy in Tulsa today?")
RENO_TOMORROW = earlier("Reno", "tomorrow", "Will it be windy in Reno tomorrow?")
DULUTH_WEDNESDAY = earlier("Duluth", "wednesday", "Will it freeze in Duluth on Wednesday?")
SPOKANE_TODAY = earlier("Spokane", "today", "What does today look like in Spokane?")
MADISON_TOMORROW = earlier("Madison", "tomorrow", "Do I need a coat in Madison tomorrow?")
ANY = "any"

# (category, utterance, the call before it, the place it runs for, the day as days after TODAY or None past the forecast)
SEEN = [
    ("today", "What's the weather in Denver today?", None, "Denver", 0),
    ("today", "Will it rain in Boston right now?", None, "Boston", 0),
    ("tomorrow", "What's the forecast for Portland tomorrow?", None, "Portland", 1),
    ("tomorrow", "Do I need an umbrella in Austin tomorrow?", None, "Austin", 1),
    ("weekday", "What's the weather in Denver on Friday?", None, "Denver", 4),
    ("weekday", "Is it going to rain in Boston on Sunday?", None, "Boston", 6),
    ("weekend", "What's the weather in Denver this weekend?", None, "Denver", 5),
    ("weekend", "Will it be nice in Boston over the weekend?", None, "Boston", 5),
    ("in three days", "What's the weather in Denver in three days?", None, "Denver", 3),
    ("in three days", "Will it rain in Boston three days from now?", None, "Boston", 3),
    ("a week from today", "What's the weather in Denver a week from today?", None, "Denver", 7),
    ("a week from today", "What will it be like in Boston a week from now?", None, "Boston", 7),
    ("ten days from now", "What's the weather in Denver ten days from now?", None, "Denver", 10),
    ("ten days from now", "Will it rain in Boston in ten days?", None, "Boston", 10),
    ("follow-up, day only", "What about a week from today?", DENVER_TOMORROW, "Denver", 7),
    ("follow-up, day only", "And on Friday?", DENVER_TOMORROW, "Denver", 4),
    ("follow-up, place only", "What about Boston?", DENVER_TOMORROW, "Boston", 1),
    ("follow-up, place only", "And in Seattle?", BOSTON_FRIDAY, "Seattle", 4),
    ("no day", "What's the weather in Denver?", None, "Denver", 0),
    ("no day", "Is it going to rain in Boston?", None, "Boston", 0),
    ("past the forecast", "What's the weather in Denver three weeks from today?", None, "Denver", None),
    ("past the forecast", "Will it rain in Boston a month from now?", None, "Boston", None),
    # Held-out in the first round, which ran on an earlier design of the day question. That design changed after the run,
    # so these cases were looked at: they are seen now, and `HELD` below is new.
    ("today", "How's the weather in Seattle today?", None, "Seattle", 0),
    ("today", "Is it going to snow in Chicago today?", None, "Chicago", 0),
    ("tomorrow", "Will it be warm in Miami tomorrow?", None, "Miami", 1),
    ("tomorrow", "What's the weather going to be in Omaha tomorrow?", None, "Omaha", 1),
    ("weekday", "How will Seattle look on Thursday?", None, "Seattle", 3),
    ("weekday", "What's the forecast for Phoenix on Saturday?", None, "Phoenix", 5),
    ("weekend", "How's the weekend looking in Seattle?", None, "Seattle", 5),
    ("weekend", "Is it going to rain in Chicago this weekend?", None, "Chicago", 5),
    ("in three days", "Give me the Chicago forecast for three days out.", None, "Chicago", 3),
    ("in three days", "What will Phoenix be like in 3 days?", None, "Phoenix", 3),
    ("a week from today", "How's the forecast for Seattle one week from today?", None, "Seattle", 7),
    ("a week from today", "Will it be sunny in Phoenix in a week?", None, "Phoenix", 7),
    ("ten days from now", "How's Seattle looking ten days out?", None, "Seattle", 10),
    ("ten days from now", "What's the forecast for Chicago in 10 days?", None, "Chicago", 10),
    ("follow-up, day only", "How about in three days?", SEATTLE_TODAY, "Seattle", 3),
    ("follow-up, day only", "And a week from today?", SEATTLE_TODAY, "Seattle", 7),
    ("follow-up, place only", "What about Portland?", BOSTON_FRIDAY, "Portland", 4),
    ("follow-up, place only", "How about Chicago?", AUSTIN_WEEKEND, "Chicago", 5),
    ("no day", "What's the forecast for Seattle?", None, "Seattle", 0),
    ("no day", "How hot is it in Phoenix?", None, "Phoenix", 0),
    ("past the forecast", "What's the weather in Seattle in twenty days?", None, "Seattle", None),
    ("past the forecast", "How will Chicago look in a month and a half?", None, "Chicago", None),
    # Held-out in the last round, on the code before the rules came out of the day and place path. That change moved the
    # question set, so these were looked at: they are seen now, and `HELD` below is new.
    ("today", "Is it going to be windy in Tulsa today?", None, "Tulsa", 0),
    ("today", "Give me today's forecast for Reno.", None, "Reno", 0),
    ("tomorrow", "How cold will it get in Fargo tomorrow?", None, "Fargo", 1),
    ("tomorrow", "Will Nashville get any rain tomorrow?", None, "Nashville", 1),
    ("weekday", "Will it freeze in Duluth on Wednesday?", None, "Duluth", 2),
    ("weekday", "How's the forecast for Tampa on Friday?", None, "Tampa", 4),
    ("weekend", "What's the weather doing in Tulsa this weekend?", None, "Tulsa", 5),
    ("weekend", "Should I pack an umbrella for Reno this weekend?", None, "Reno", 5),
    ("in three days", "What's it going to be like in Fargo three days from today?", None, "Fargo", 3),
    ("in three days", "Will it rain in Nashville in 3 days?", None, "Nashville", 3),
    ("a week from today", "What's the forecast for Reno a week from now?", None, "Reno", 7),
    ("a week from today", "Will it be cold in Duluth one week out?", None, "Duluth", 7),
    ("ten days from now", "Will it rain in Tampa ten days from today?", None, "Tampa", 10),
    ("ten days from now", "What's the weather going to be in Albany in 10 days?", None, "Albany", 10),
    ("follow-up, day only", "And in five days?", FARGO_TOMORROW, "Fargo", 5),
    ("follow-up, day only", "And on Wednesday?", TULSA_TODAY, "Tulsa", 2),
    ("follow-up, place only", "What about Tampa?", RENO_TOMORROW, "Tampa", 1),
    ("follow-up, place only", "And how about Albany?", DULUTH_WEDNESDAY, "Albany", 2),
    ("no day", "What's the weather like in Tulsa?", None, "Tulsa", 0),
    ("no day", "Is it raining in Nashville?", None, "Nashville", 0),
    ("past the forecast", "What's the weather in Reno in three weeks?", None, "Reno", None),
    ("past the forecast", "Will it be warm in Tampa in two months?", None, "Tampa", None),
    ("fresh request after a lookup", "What's the weather in Boston?", DENVER_TOMORROW, "Boston", 0),
    ("fresh request after a lookup", "Is it going to rain in Seattle?", BOSTON_FRIDAY, "Seattle", 0),
    ("no place stated", "What's the weather tomorrow?", None, None, ANY),
    ("no place stated", "Is it going to rain on Friday?", None, None, ANY),
    ("place with a day or month word", "What's the weather in Friday Harbor?", None, "Friday Harbor", 0),
    ("place with a day or month word", "Will it rain in Augusta tomorrow?", None, "Augusta", 1),
    ("two-word place", "What's the weather in Salt Lake City tomorrow?", None, "Salt Lake City", 1),
    ("two-word place", "Will it rain in Kansas City today?", None, "Kansas City", 0),
    ("place with a state or country", "What's the weather in Portland, Maine tomorrow?", None, "Portland, Maine", 1),
    ("place with a state or country", "Will it rain in Paris, France today?", None, "Paris, France", 0),
    ("clarification, just the place", "Seattle.", None, "Seattle", ANY),
    ("clarification, just the place", "Salt Lake City", None, "Salt Lake City", ANY),
    ("clarification, with filler", "Uh, in Boston, I guess.", None, "Boston", ANY),
    ("clarification, with filler", "Um, Salt Lake City, please.", None, "Salt Lake City", ANY),
    ("clarification, not an answer", "What time is it in Tokyo?", None, None, ANY),
    ("clarification, not an answer", "Never mind that.", None, None, ANY),
    ("place with a state or country", "How's the weather in Springfield, Illinois on Friday?", None,
     "Springfield, Illinois", 4),
    ("place with a state or country", "What will it be like in Cairo, Egypt tomorrow?", None, "Cairo, Egypt", 1),
    ("clarification, just the place", "Denver", None, "Denver", ANY),
    ("clarification, just the place", "Friday Harbor.", None, "Friday Harbor", ANY),
    ("clarification, with filler", "Oh, let's say Seattle.", None, "Seattle", ANY),
    ("clarification, with filler", "Sure, how about Kansas City?", None, "Kansas City", ANY),
    ("clarification, not an answer", "Actually, what's the weather in Paris tomorrow?", None, None, ANY),
    ("clarification, not an answer", "What did you say?", None, None, ANY),
]
HELD = [
    ("today", "Is it going to be hot in Lincoln today?", None, "Lincoln", 0),
    ("today", "What does today look like in Spokane?", None, "Spokane", 0),
    ("tomorrow", "Do I need a coat in Madison tomorrow?", None, "Madison", 1),
    ("tomorrow", "Will Boise be sunny tomorrow?", None, "Boise", 1),
    ("weekday", "What's the forecast for Salem on Thursday?", None, "Salem", 3),
    ("weekday", "Is it going to rain in Provo on Saturday?", None, "Provo", 5),
    ("weekend", "How does the weekend look in Boise?", None, "Boise", 5),
    ("weekend", "Will it snow in Lincoln this weekend?", None, "Lincoln", 5),
    ("in three days", "What's the weather going to be in Spokane three days from now?", None, "Spokane", 3),
    ("in three days", "Will it be warm in Madison in three days?", None, "Madison", 3),
    ("a week from today", "How's the weather looking in Salem a week from today?", None, "Salem", 7),
    ("a week from today", "What will Provo be like one week from now?", None, "Provo", 7),
    ("ten days from now", "What's the forecast for Boise ten days from now?", None, "Boise", 10),
    ("ten days from now", "Will it rain in Lincoln ten days out?", None, "Lincoln", 10),
    ("follow-up, day only", "And three days from now?", SPOKANE_TODAY, "Spokane", 3),
    ("follow-up, day only", "What about on Saturday?", MADISON_TOMORROW, "Madison", 5),
    ("follow-up, place only", "What about Lincoln?", SPOKANE_TODAY, "Lincoln", 0),
    ("follow-up, place only", "And Boise?", MADISON_TOMORROW, "Boise", 1),
    ("no day", "What's the weather in Provo?", None, "Provo", 0),
    ("no day", "Is it snowing in Salem?", None, "Salem", 0),
    ("past the forecast", "What's the weather in Boise in four weeks?", None, "Boise", None),
    ("past the forecast", "How will Lincoln look in three months?", None, "Lincoln", None),
    ("fresh request after a lookup", "How's the weather in Omaha?", FARGO_TOMORROW, "Omaha", 0),
    ("fresh request after a lookup", "Will it be sunny in Tampa?", DULUTH_WEDNESDAY, "Tampa", 0),
    ("no place stated", "How will the weather be this weekend?", None, None, ANY),
    ("no place stated", "Will it snow in a week?", None, None, ANY),
    ("place with a day or month word", "How's the weather in Sunday River today?", None, "Sunday River", 0),
    ("place with a day or month word", "Is it going to be cold in Mayfield tomorrow?", None, "Mayfield", 1),
    ("two-word place", "How's the forecast for Santa Fe on Friday?", None, "Santa Fe", 4),
    ("two-word place", "Is it going to snow in Grand Rapids this weekend?", None, "Grand Rapids", 5),
    ("place with a state or country", "What's the forecast for Austin, Texas today?", None, "Austin, Texas", 0),
    ("place with a state or country", "Is it going to rain in Lima, Peru tomorrow?", None, "Lima, Peru", 1),
    ("clarification, just the place", "Omaha.", None, "Omaha", ANY),
    ("clarification, just the place", "Spokane", None, "Spokane", ANY),
    ("clarification, with filler", "Hmm, let me think, Madison I guess.", None, "Madison", ANY),
    ("clarification, with filler", "Yeah, Boise, thanks.", None, "Boise", ANY),
    ("clarification, not an answer", "Could you check the time in Reno?", None, None, ANY),
    ("clarification, not an answer", "Wait, how long is that forecast good for?", None, None, ANY),
]
CASES = [(*case, False) for case in SEEN] + [(*case, True) for case in HELD]


def key(body: dict) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode("utf-8")).hexdigest()


def picked_offset(when):
    """Days after TODAY that `when` picks over the made-up forecast, or None when it picks none (or is no day the
    code under test knows: the code this change started from has no number for a day)."""
    try:
        if "today" in inspect.signature(lookups.pick_days).parameters:
            days = lookups.pick_days(when, FORECAST, TODAY)
        else:
            days = lookups.pick_days(when, FORECAST)
    except (ValueError, TypeError):
        return None
    return (days[0].date - TODAY).days if days else None


def decide(client, utterance, before, sample):
    """One end of turn as the live server runs it. Returns what the dispatched weather call ran with, and the day it
    picks, or None when no weather call was dispatched over the registry's bar."""
    segmenter = HybridSegmenter(client, f"days:{sample}", tools=TOOLS,
                                arguments=run.decider_arguments(client, t3_enabled=True))
    segmenter.conversation_mode = True
    segmenter.orch.gate, segmenter.orch.side_effects_wait_for_final = PLAN_A, True
    prior = (before,) if before else ()
    segmenter.prior_resolved_tasks = run.with_thread(prior, None)
    run.close_with_decider(segmenter, TranscriptUpdate(1, utterance, True), prior)
    dispatched = {d["task_id"] for d in segmenter.dispatch_log}
    for record in segmenter.records():
        tool = record.get("tool") or {}
        if record["task_id"] in dispatched and tool.get("tool") == "weather":
            routed = RULES.dispatch(utterance, ToolCall(**before["call"]) if before else None,
                                    choice=ToolChoice(**tool))
            if routed.call is None or routed.call.tool != "weather":
                return None
            return {"arguments": routed.call.arguments, "offset": picked_offset(routed.call.arguments["when"]),
                    "reading": tool.get("arguments")}
    return None


def clarify(client, text, sample):
    """What the decider reads in a person's answer to "which city?", run as the live turn runs it, or what the word
    rules read in the code this change started from. The task waited on a place, the call's earlier lookup is none."""
    waiting = {"tool": "weather", "arguments": {"place": None, "when": "today"}, "missing": ("place",)}
    if hasattr(contract, "reading_request"):
        body = contract.reading_request(text, None, {"tool": "weather", "needs": ["place"]})
        record = client.call("decisions", body, label=f"days:{sample}:answer")
        reading = contract.parse_reading(record["response"], body) if record["status"] == 200 else None
        call = RULES.answered(waiting, reading)
    else:
        call = RULES.answered(waiting, text)
    return {"arguments": call.arguments if call else {"place": None, "when": None}, "offset": ANY, "reading": None}


def run_case(client, category, utterance, before, sample):
    """One case as the live turn runs it: a reading of the answer to "which city?" for a clarification case, else one
    end of turn."""
    if category.startswith("clarification"):
        return clarify(client, utterance, sample)
    return decide(client, utterance, before, sample)


def passes(case_place, case_offset, got):
    """The place is the case's, none when the case says to ask (a turn that dispatched nothing asks too), and the day
    is the case's unless it says any."""
    if got is None:
        return case_place is None
    place = got["arguments"]["place"]
    same_place = place is None if case_place is None else (place or "").lower() == case_place.lower()
    return same_place and (case_offset == ANY or got["offset"] == case_offset)


class Recording:
    """The decider, with each answer kept under the hash of its request and the sample it was asked for."""

    def __init__(self, client, answers, sample):
        self.client, self.answers, self.sample = client, answers, sample

    def call(self, route, body, label=""):
        record = self.client.call(route, body, label=label, fresh=True)
        if record["status"] == 200:
            self.answers.setdefault(key(body), {})[str(self.sample)] = record["response"]
        return record

    def __getattr__(self, name):
        return getattr(self.client, name)


class Replay:
    """The decider, answering each request from the fixture, and only a request and sample it recorded."""

    def __init__(self, answers, sample):
        self.answers, self.sample = answers, sample

    def call(self, route, body, label="", **_):
        answer = self.answers.get(key(body), {}).get(str(self.sample))
        if answer is None:
            raise KeyError("no recorded answer for this request: re-run voice.tools.eval.record_days")
        return {"status": 200, "response": answer, "timing": {"ms": 0.0, "started_at": ""}}


def tally(rows):
    """Correct samples and samples run, per category and in all, for the seen cases and the held-out ones."""
    out = defaultdict(lambda: [0, 0])
    for row in rows:
        for name in (row["category"], "ALL"):
            counts = out[(row["held"], name)]
            counts[0] += row["passes"]
            counts[1] += 1
    return out


def report(rows, label):
    counts = tally(rows)
    print(f"\n{label}: correct samples of samples run")
    for name in [*dict.fromkeys(row["category"] for row in rows), "ALL"]:
        seen, held = counts[(False, name)], counts[(True, name)]
        print(f"  {name:24} seen {seen[0]:>2} of {seen[1]:<2}   held-out {held[0]:>2} of {held[1]:<2}")


def main() -> int:
    import os

    from voice.local_decider.client import decider as make_decider
    from voice.prototype.segmenter import use_decider_cache

    parser = argparse.ArgumentParser()
    parser.add_argument("--label", choices=("before", "after"), required=True)
    parser.add_argument("--samples", type=int, default=SAMPLES)
    parser.add_argument("--only", choices=("seen", "held"), help="run one split, and keep the other's rows")
    args = parser.parse_args()
    use_decider_cache(Path(os.environ.get("VOICE_HOME", Path.home() / "voice")) / "evals" / "day-decisions", 2000)
    kept = json.loads(FIXTURE.read_text(encoding="utf-8")) if FIXTURE.exists() else {"rows": [], "answers": {}}
    answers = kept["answers"]
    rows = [r for r in kept["rows"] if r["label"] != args.label
            or (args.only and r["held"] != (args.only == "held"))]
    with make_decider("liquid") as client:
        for category, utterance, before, place, offset, held in CASES:
            if args.only and held != (args.only == "held"):
                continue
            for sample in range(args.samples):
                got = run_case(Recording(client, answers, sample), category, utterance, before, sample)
                rows.append({"label": args.label, "category": category, "utterance": utterance, "before": before,
                             "place": place, "offset": offset, "held": held, "sample": sample, "got": got,
                             "passes": passes(place, offset, got)})
                print(json.dumps(rows[-1]), flush=True)
    FIXTURE.write_bytes((json.dumps({"rows": rows, "answers": answers}, indent=1) + "\n").encode("utf-8"))
    report([r for r in rows if r["label"] == args.label], args.label)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
