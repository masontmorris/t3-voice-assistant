"""The assistant's own follow-up question, through the gated turn the live server runs, with in-memory sessions.

A confident tool choice with nothing to look up ("What's the weather like?" with no place) does not run and report a
failure: the task waits, and the note asks the person which city. Their next turn's answer runs that task, and the
decider is asked nothing, because the turn answers the assistant's own question. Words that ask something else are a
request of their own and are not forced into the waiting task. These tests stand in for llama.cpp and for the decider,
so they say nothing about what the voice model says.
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from voice.prototype import run as prototype
from voice.prototype.tests import isolate_decider_cache
from voice.prototype.tests.test_run_shared_session import CLOCK, FakeSession
from voice.tools.tests.replay import replay_registry


def pick(choice, question, confidence=0.9):
    """One choice answer with its probabilities, which `_pick` and `parse_decisions` both read."""
    others = [name for name in question["criteria"] if name != choice]
    probabilities = {choice: 1.0 if not others else 0.9}
    probabilities.update({name: 0.1 / len(others) for name in others})
    return {"type": "choice", "choice": choice, "confidence": confidence, "probabilities": probabilities}


class Decider:
    """The decisions answers, written by the test: one segment, the tool it picks, and the place and day it read."""

    def __init__(self, tool="weather", confidence=0.9, place=None, when="none", answers=True, readings=None):
        self.tool, self.confidence, self.place, self.when, self.answers = tool, confidence, place, when, answers
        self.scripts, self.bodies = readings or {}, []

    def decisions(self):
        """The bodies that decided a turn's segments, not the ones that read an answer to the assistant's question."""
        return [body for body in self.bodies if "s1_kind" in body["questions"]]

    def readings(self):
        return [body for body in self.bodies if "r_place" in body["questions"]]

    def call(self, route, body, label=""):
        assert route == "decisions", route
        self.bodies.append(body)
        answers = {}
        spec = {"tool": self.tool, "place": self.place, "when": self.when, "continues": "new",
                **self.scripts.get(body["state"].get("transcript"), {})}
        for name, question in body["questions"].items():
            field = name.split("_", 1)[1]
            if question["type"] == "noul":
                answers[name] = {"type": "noul", "noul": 0.0 if field == "side_effect" else 1.0}
            elif field == "place":
                answers[name] = {"type": "text", "text": spec["place"], "span": None, "found": 0.9}
            elif field == "when":
                answers[name] = pick(spec["when"], question)
            elif field == "continues":
                answers[name] = pick(spec["continues"], question)
            elif field == "answers":
                answers[name] = pick("answers" if self.answers else "other", question)
            elif field == "tool":
                answers[name] = pick(spec["tool"], question, self.confidence)
            elif field in ("kind", "ready"):
                answers[name] = pick("request" if field == "kind" else "dispatch_now", question)
            else:
                answers[name] = pick("none", question)
        return {"status": 200, "response": {"model": "stub", "answers": answers},
                "timing": {"ms": 1.0, "started_at": "stub"}}


class Turn:
    rate, audio_s = 16_000, 0.0

    def __init__(self, n):
        self.name = self.stem = f"turn_{n:03d}"

    async def stream(self, voice, asr):
        await voice.send(type="input_audio_buffer.append", audio="AA==")
        return CLOCK


class Call(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(isolate_decider_cache(Path(self.tmp.name) / "decider-cache", 200))
        original = prototype.Session.open

        async def open_session(url, name, clock, on_event=None):
            return await FakeSession.open(url, name, clock, on_event)

        prototype.Session.open = open_session
        self.addCleanup(setattr, prototype.Session, "open", original)
        self.tools, self.fetch, _ = replay_registry(None)
        self.voice = await FakeSession.open("ws://unused", "voice", lambda: CLOCK)

    async def turn(self, n, words, decider, **kw):
        with mock.patch.object(FakeSession, "transcript", words):
            return await prototype.run_clip(Turn(n), "c", False, "ws://unused", decider, 0.0, Path(self.tmp.name), {},
                                            "hybrid", self.tools, "gated", voice=self.voice, snapshot_mode="note",
                                            final_decider=True, **kw)

    def notes(self):
        return [event["item"]["content"][0]["text"] for event in self.voice.sent
                if event.get("type") == "conversation.item.create"
                and event["item"]["content"][0]["text"].startswith(prototype.NOTE_OPEN)]

    async def first_turn(self):
        record = await self.turn(1, "What's the weather like?", Decider(place=None))
        return record, tuple(prototype.carried_tasks(record))


class ClarificationTest(Call):
    async def test_a_forecast_with_no_place_asks_which_city_and_runs_nothing(self):
        record, _ = await self.first_turn()
        self.assertEqual(record["executions"], {}, "nothing runs until they say where")
        self.assertEqual(record["dispatch_log"], [], "and no task is dispatched")
        task, = record["snapshot"]["tasks"]
        self.assertEqual((task["status"], task["capability"], task["needs"]),
                         ("needs_clarification", "weather", ["place"]))
        self.assertIn("which city or place they mean", self.notes()[-1])
        self.assertNotIn("couldn't check that", self.notes()[-1], "no failure is spoken for a question")

    async def test_the_answer_runs_the_task_the_question_was_about(self):
        _, carried = await self.first_turn()
        self.assertEqual(carried[0]["waiting"]["missing"], ("place",), "the turn carries what it waits for")
        answered = Decider(place="Denver")
        record = await self.turn(2, "In Denver", answered, prior_tasks=carried)
        self.assertEqual(answered.decisions(), [], "the answer turn is not a new decision")
        reading, = answered.readings()
        self.assertEqual(reading["state"]["assistant_asked_for"], {"tool": "weather", "needs": ["place"]},
                         "the decider reads the words, not a word list: it is told what was asked")
        self.assertEqual([entry["instruction"] for entry in record["dispatch_log"]], ["What's the weather like?"],
                         "the request that could not start runs, not a new one for the answer")
        execution, = record["executions"].values()
        self.assertEqual((execution["state"], execution["tool"]["tool"], execution["tool"]["arguments"]),
                         ("completed", "weather", {"place": "Denver", "when": "today"}))
        self.assertTrue(execution["result_summary"].startswith("Forecast for Denver"))
        self.assertIn("finished", self.notes()[-1], "the note carries the result the question was for")

    async def test_the_completed_lookup_is_what_a_later_follow_up_continues(self):
        _, carried = await self.first_turn()
        record = await self.turn(2, "In Denver", Decider(place="Denver"), prior_tasks=carried)
        carried = prototype.carried_tasks(record)
        self.assertEqual([task["call"] for task in carried],
                         [{"tool": "weather", "arguments": {"place": "Denver", "when": "today"}}])
        self.assertEqual([task.get("waiting") for task in carried], [None], "nothing is still waiting")

    async def test_an_answer_that_asks_something_else_is_not_forced_into_the_task(self):
        _, carried = await self.first_turn()
        asked = Decider(tool="time", place="Tokyo", answers=False)
        record = await self.turn(2, "What time is it in Tokyo?", asked, prior_tasks=carried)
        self.assertEqual(len(asked.readings()), 1, "the decider is asked whether the words answer the question")
        self.assertEqual(len(asked.decisions()), 1, "and they are not an answer, so they are a new decision")
        self.assertEqual([entry["instruction"] for entry in record["dispatch_log"]], ["What time is it in Tokyo?"])
        execution, = record["executions"].values()
        self.assertEqual((execution["tool"]["tool"], execution["tool"]["arguments"]), ("time", {"place": "Tokyo"}),
                         "the forecast nobody gave a city for is not run with Tokyo")
        self.assertEqual([task["status"] for task in record["snapshot_after_tools"]["tasks"]], ["completed"])


class FollowUpTest(Call):
    """What a follow-up, a fresh request and a correction run, from the decider's typed reading of each turn: its
    place, its day and whether it continues the last lookup. The registry has no default place, so a place can only
    come from the reading or the earlier lookup of the call."""

    DENVER = "What's the weather in Denver tomorrow?"

    async def call(self, readings, *said):
        decider, records, carried = Decider(readings=readings), [], []
        for n, words in enumerate(said, 1):
            record = await self.turn(n, words, decider, prior_tasks=tuple(carried))
            carried += prototype.carried_tasks(record)
            records.append(record)
        return records, decider

    def lookup(self, record):
        return [(e["tool"]["tool"], e["tool"]["arguments"], e["tool"]["outcome"]) for e in record["executions"].values()
                if e["tool"]]

    def weather(self, place, when, continues="new"):
        return {"tool": "weather", "place": place, "when": when, "continues": continues}

    async def test_a_follow_up_that_names_only_a_day_keeps_the_earlier_place(self):
        records, _ = await self.call({self.DENVER: self.weather("Denver", "tomorrow"),
                                      "And what about Saturday?": self.weather(None, "saturday", "continues")},
                                     self.DENVER, "And what about Saturday?")
        self.assertEqual(self.lookup(records[1]), [("weather", {"place": "Denver", "when": "saturday"}, "ok")])

    async def test_a_follow_up_that_names_only_a_place_keeps_the_earlier_day(self):
        records, _ = await self.call({self.DENVER: self.weather("Denver", "tomorrow"),
                                      "What about Tokyo?": self.weather("Tokyo", "none", "continues")},
                                     self.DENVER, "What about Tokyo?")
        self.assertEqual(self.lookup(records[1]), [("weather", {"place": "Tokyo", "when": "tomorrow"}, "ok")])

    async def test_a_fresh_request_after_a_lookup_does_not_inherit_its_day(self):
        records, decider = await self.call({self.DENVER: self.weather("Denver", "tomorrow"),
                                            "What's the weather in Tokyo?": self.weather("Tokyo", "none")},
                                           self.DENVER, "What's the weather in Tokyo?")
        self.assertEqual(self.lookup(records[1]), [("weather", {"place": "Tokyo", "when": "today"}, "ok")])
        asked = [body for body in decider.decisions() if "s1_continues" in body["questions"]]
        self.assertEqual(len(asked), 1, "the decider is asked whether it continues, once there is a lookup to continue")
        self.assertNotIn("s1_continues", decider.decisions()[0]["questions"], "and not on the first turn of a call")

    async def test_an_amended_correction_keeps_the_place_and_changes_the_day(self):
        records, _ = await self.call({self.DENVER: self.weather("Denver", "tomorrow"),
                                      "And what about Saturday?": self.weather(None, "saturday", "continues"),
                                      "And what about Sunday?": {"place": None, "when": "sunday"}},
                                     self.DENVER, "And what about Saturday?", "No, Sunday.")
        self.assertEqual(self.lookup(records[2]), [("weather", {"place": "Denver", "when": "sunday"}, "ok")])

    async def test_a_correction_that_names_the_rejected_place_runs_the_new_one(self):
        records, _ = await self.call({"What time is it in Tokyo?": {"tool": "time", "place": "Tokyo", "when": "none"},
                                      "What time is it in Denver?": {"place": "Denver", "when": "none"}},
                                     "What time is it in Tokyo?", "No, not Tokyo, Denver.")
        self.assertEqual(self.lookup(records[1]), [("time", {"place": "Denver"}, "ok")])

    async def test_with_no_earlier_place_a_follow_up_waits_for_one(self):
        records, _ = await self.call({"What's the weather tomorrow?": self.weather(None, "tomorrow"),
                                      "And what about Saturday?": self.weather(None, "saturday", "continues")},
                                     "What's the weather tomorrow?", "And what about Saturday?")
        self.assertEqual([self.lookup(record) for record in records], [[], []], "nothing runs on a place nobody said")
        self.assertEqual(self.fetch.urls, [], "no place is looked up at all")
        self.assertEqual([t["status"] for t in records[1]["snapshot"]["tasks"]], ["needs_clarification"])


if __name__ == "__main__":
    unittest.main()
