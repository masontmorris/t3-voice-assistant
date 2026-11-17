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

    def __init__(self, tool="weather", confidence=0.9, place=None, when="none"):
        self.tool, self.confidence, self.place, self.when = tool, confidence, place, when
        self.bodies = []

    def call(self, route, body, label=""):
        assert route == "decisions", route
        self.bodies.append(body)
        answers = {}
        for name, question in body["questions"].items():
            field = name.split("_", 1)[1]
            if question["type"] == "noul":
                answers[name] = {"type": "noul", "noul": 0.0 if field == "side_effect" else 1.0}
            elif field == "place":
                answers[name] = {"type": "text", "text": self.place, "span": None, "found": 0.9}
            elif field == "when":
                answers[name] = pick(self.when, question)
            elif field == "tool":
                answers[name] = pick(self.tool, question, self.confidence)
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
        self.assertEqual(answered.bodies, [], "the answer turn is not a new decision")
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
        asked = Decider(tool="time", place="Tokyo")
        record = await self.turn(2, "What time is it in Tokyo?", asked, prior_tasks=carried)
        self.assertEqual(len(asked.bodies), 1, "their own question is a new decision, and it is asked")
        self.assertEqual([entry["instruction"] for entry in record["dispatch_log"]], ["What time is it in Tokyo?"])
        execution, = record["executions"].values()
        self.assertEqual((execution["tool"]["tool"], execution["tool"]["arguments"]), ("time", {"place": "Tokyo"}),
                         "the forecast nobody gave a city for is not run with Tokyo")
        self.assertEqual([task["status"] for task in record["snapshot_after_tools"]["tasks"]], ["completed"])


if __name__ == "__main__":
    unittest.main()
