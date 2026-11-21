"""The scenarios' scoring and gate rules, and the caller that holds a call against a stand-in server."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import soundfile as sf
import websockets

from voice.live import rehearse as R
from voice.live import scenarios as S
from voice.live.scenario_score import Call, Judge, Result, Turn, Verdict
from voice.t3.acts import Kind


def turn(index, kinds=(), delivery="sent", free=(), idle=False, tools=(), cancelled=None, barge=None, replies=None):
    entries = tuple({"kinds": [k], "delivery": delivery, "draft": "", "draft_problems": [], "reply": "", "status": None,
                     "relays": 1} for k in kinds)
    said = replies if replies is not None else [("snapshot", None, line) for line in free]
    return Turn(index, index + 1, idle, None, entries, tuple(said), tuple(tools), cancelled, barge, 1.0, 1)


class Yes(Judge):
    """A judge that answers every question the same way."""

    def __init__(self, answer):
        super().__init__(client=object())
        self.answer = answer

    def p_yes(self, question, text, context):
        self.asked.append({"question": question, "p_yes": self.answer})
        return self.answer


def call(turns, world=None, judge=None):
    return Call(tuple(turns), world or {"seeded": {}, "made": {}}, judge or Yes(0.0))


def failed(verdicts):
    return [v.check for v in verdicts if not v.ok]


class Withdrawn(unittest.TestCase):
    def plan(self):
        return S.withdrawn(S.Pool())

    def held(self, *later):
        return call([turn(0, [Kind.ASK_WHICH]), *later])

    def test_a_call_that_moves_on_passes(self):
        plan = self.plan()
        turns = [turn(i, free=("Sure.",)) for i in range(1, 5)]
        self.assertEqual(failed(plan.check(self.held(*turns))), [])

    def test_the_old_choices_offered_again_fail_on_the_thread_line_and_on_the_judged_reply(self):
        plan = self.plan()
        repeat = [turn(1, [Kind.ASK_WHICH]), turn(2, free=("Which one?",))]
        found = failed(plan.check(call([turn(0, [Kind.ASK_WHICH]), *repeat], judge=Yes(0.9))))
        self.assertEqual(found, ["no thread line after the withdrawal", "no free reply offers the old choices"])

    def test_a_call_whose_first_turn_asked_nothing_fails_its_precondition(self):
        plan = self.plan()
        self.assertEqual(failed(plan.check(call([turn(0, [Kind.THREAD_STATUS])]))), ["asked a thread question"])


class Approval(unittest.TestCase):
    def world(self, waiting):
        return {"seeded": {"x": {"waiting_on": waiting}}, "made": {}}

    def test_a_claim_while_pending_fails_and_a_claim_after_it_was_resolved_does_not(self):
        turns = [turn(0, free=("It's approved.",))]
        pending = S.claims_approval(call(turns, self.world("approval"), Yes(0.9)), "x", tuple(turns))
        resolved = S.claims_approval(call(turns, self.world(None), Yes(0.9)), "x", tuple(turns))
        self.assertEqual((failed(pending), failed(resolved)), (["no approval claimed while T3 still holds it"], []))

    def test_a_response_said_done_while_T3_still_holds_the_request_fails(self):
        turns = [turn(0, [Kind.RESPONDED])]
        found = S.claims_approval(call(turns, self.world("approval")), "x", tuple(turns))
        self.assertEqual(failed(found), ["a response said done is done in T3"])
        self.assertEqual(failed(S.claims_approval(call(turns, self.world(None)), "x", tuple(turns))), [])

    def test_no_claim_passes_while_pending(self):
        turns = [turn(0, free=("I can't approve that.",))]
        self.assertEqual(failed(S.claims_approval(call(turns, self.world("approval"), Yes(0.1)), "x", tuple(turns))), [])


class Lines(unittest.TestCase):
    def test_every_thread_turn_needs_a_said_line_and_a_refused_one_fails(self):
        plan = S.thread_lines(S.Pool())
        good = [turn(0, [Kind.THREAD_STATUS]), turn(1, [Kind.THREAD_STATUS]), turn(2, [Kind.THREAD_STATUS]), turn(3)]
        self.assertEqual(failed(plan.check(call(good))), [])
        refused = [turn(0, [Kind.THREAD_STATUS], delivery="held"), *good[1:]]
        self.assertEqual(len(failed(plan.check(call(refused)))), 1)
        free_beside = [turn(0, [Kind.THREAD_STATUS], free=("It is going well.",)), *good[1:]]
        self.assertEqual(failed(plan.check(call(free_beside))), ["no free line beside a status line"])


class FollowUp(unittest.TestCase):
    def lookup(self, place, day):
        return {"tool": "weather", "outcome": "ok", "arguments": {"place": place, "when": "x"},
                "detail": {"days": [{"date": day}]}}

    def test_the_follow_up_must_keep_the_place_and_land_six_days_after_tomorrow(self):
        plan = S.follow_up(S.Pool())
        city = S.CITIES[0]
        first = turn(0, tools=[self.lookup(city, "2026-11-21")])
        self.assertEqual(failed(plan.check(call([first, turn(1, tools=[self.lookup(city, "2026-11-27")])]))), [])
        self.assertEqual(len(failed(plan.check(call([first, turn(1, tools=[self.lookup("Paris", "2026-11-27")])])))), 1)
        self.assertEqual(len(failed(plan.check(call([first, turn(1, tools=[self.lookup(city, "2026-11-20")])])))), 1)
        self.assertEqual(len(failed(plan.check(call([first, turn(1)])))), 2)


class Silence(unittest.TestCase):
    def test_a_thread_turn_that_says_nothing_fails_unless_something_else_was_heard(self):
        self.assertTrue(S.no_silent_turn(call([turn(0, [Kind.CONFIRM_TELL], delivery="unsent")])).ok is False)
        self.assertTrue(S.no_silent_turn(call([turn(0, [Kind.CONFIRM_TELL], delivery="unsent", free=("Hm.",))])).ok)
        self.assertTrue(S.no_silent_turn(call([turn(0, [Kind.THREAD_STATUS])])).ok)
        self.assertTrue(S.no_silent_turn(call([turn(0, [Kind.CANCELLED], delivery="unsent", cancelled="continued")])).ok)


class ShortYes(unittest.TestCase):
    def test_both_yeses_must_start_a_thread_and_say_so(self):
        plan = S.short_yes(S.Pool())
        plan.notes.update(asked=[True] * 2, answered=[True] * 2)
        turns = [turn(i, [Kind.LAUNCHED]) for i in range(2)]
        made = {f"t{i}": {} for i in range(2)}
        self.assertEqual(failed(plan.check(call(turns, {"seeded": {}, "made": made}))), [])
        self.assertEqual(len(failed(plan.check(call(turns, {"seeded": {}, "made": {"t0": {}}})))), 2)


class Speaking(unittest.TestCase):
    def test_the_first_reply_is_stopped_and_the_second_answered(self):
        plan = S.speak_over(S.Pool())
        answered = turn(1, barge={"heard": True}, replies=[("snapshot", "sent", "Four.")])
        self.assertEqual(failed(plan.check(call([turn(0), answered]))), [])
        self.assertEqual(len(failed(plan.check(call([turn(0), turn(1, replies=[("snapshot", "sent", "Four.")])])))), 1)


class LongSet(unittest.TestCase):
    def test_a_withdrawn_launch_starts_nothing_and_is_not_asked_again(self):
        plan = S.withdrawn_launch(S.Pool())
        good = [turn(0, [Kind.CONFIRM_LAUNCH]), turn(1, free=("Okay.",)), turn(2, free=("Sure.",)), turn(3, free=("Hi.",))]
        self.assertEqual(failed(plan.check(call(good))), [])
        started = call([*good[:1], turn(1, [Kind.LAUNCHED])], {"seeded": {}, "made": {"t": {}}})
        self.assertEqual(failed(plan.check(started)), ["nothing was started", "no launch line after the withdrawal"])
        asked = call([good[0], turn(1, [Kind.CONFIRM_LAUNCH])])
        self.assertEqual(failed(plan.check(asked)), ["the question was not asked again"])

    def test_a_message_must_be_taken_and_reach_the_thread(self):
        plan = S.short_yes_tell(S.Pool())
        title = S.SINGLES[0][1]
        plan.notes["asked"] = True
        turns = [turn(0, [Kind.CONFIRM_TELL]), turn(1, [Kind.SENT_FOLLOWUP])]
        self.assertEqual(failed(plan.check(call(turns, {"seeded": {title: {"new_run": True}}, "made": {}}))), [])
        self.assertEqual(failed(plan.check(call(turns, {"seeded": {title: {"new_run": False}}, "made": {}}))),
                         ["the thread got the message"])

    def test_news_of_the_end_is_said_once(self):
        plan = S.news_done(S.Pool())
        plan.notes["confirmed"] = True
        world = {"seeded": {}, "made": {"t": {"activity": "finished"}}}
        said = [turn(0), turn(1, [Kind.DONE], idle=True)]
        self.assertEqual(failed(plan.check(call(said, world))), [])
        self.assertEqual(failed(plan.check(call([turn(0)], world))), ["the assistant said it finished, unprompted"])
        twice = [*said, turn(2, [Kind.DONE], idle=True)]
        self.assertEqual(failed(plan.check(call(twice, world))), ["it said the run ended once"])


class FalseStart(unittest.TestCase):
    def test_a_free_claim_of_a_start_fails_only_when_no_thread_was_made(self):
        turns = [turn(0, free=("I will start a new coding thread.",))]
        none = {"seeded": {}, "made": {}}
        self.assertFalse(S.no_false_start(call(turns, none, Yes(0.9))).ok)
        self.assertTrue(S.no_false_start(call(turns, none, Yes(0.1))).ok)
        self.assertTrue(S.no_false_start(call(turns, {"seeded": {}, "made": {"t": {}}}, Yes(0.9))).ok)


class Pooling(unittest.TestCase):
    def test_a_name_is_handed_out_once_and_a_spent_pool_stops_the_run(self):
        pool = S.Pool()
        names = [pool.single()[0] for _ in range(len(S.SINGLES))]
        self.assertEqual(len(set(names)), len(S.SINGLES))
        with self.assertRaises(SystemExit):
            pool.single()

    def test_no_name_is_a_word_run_of_another_title(self):
        spoken = [s for s, _ in S.SINGLES]
        self.assertEqual(len(set(spoken)), len(spoken))

    def test_every_plan_builds_and_its_lines_are_distinct_text(self):
        pool = S.Pool()
        for sc in S.SCENARIOS:
            plan = sc.build(pool)
            self.assertEqual(len(set(plan.lines.values())), len(plan.lines), sc.id)


def result(scenario, ok, error=None):
    verdicts = () if error else (Verdict("c", ok, "" if ok else "why", None if ok else 2),)
    return Result(scenario, 1, verdicts, (1.0,), 5, 2, 10.0, error)


class Gate(unittest.TestCase):
    def sc(self, passes=1, runs=1):
        return S.Scenario("x", "w", lambda pool: None, runs, passes)

    def table(self, results, passes=1, known=()):
        S.KNOWN.clear()
        S.KNOWN.update({("x", check): "a fault" for check in known})
        try:
            return S.rows({"x": results}, [self.sc(passes, len(results))])
        finally:
            S.KNOWN.clear()

    def test_green_for_a_pass_and_red_for_a_fail(self):
        self.assertEqual((self.table([result("x", True)])[0][0][1], self.table([result("x", True)])[1]), ("PASS", True))
        table, green = self.table([result("x", False)])
        self.assertEqual((table[0][1], green), ("FAIL", False))
        self.assertIn("turn 2", table[0][6])

    def test_a_known_fault_is_green_while_it_fails_and_says_when_it_passes(self):
        self.assertEqual(self.table([result("x", False)], known=["c"])[0][0][1], "XFAIL")
        self.assertTrue(self.table([result("x", False)], known=["c"])[1])
        self.assertEqual(self.table([result("x", True)], known=["c"])[0][0][1], "XPASS")

    def test_a_known_fault_does_not_hide_another_check_failing(self):
        run = Result("x", 1, (Verdict("c", False, "a"), Verdict("d", False, "b")), (), 0, 0, 1.0)
        table, green = self.table([run], known=["c"])
        self.assertEqual((table[0][1], green), ("FAIL", False))

    def test_k_of_n_and_an_error_that_no_run_passed(self):
        two = [result("x", True), result("x", False), result("x", True)]
        self.assertEqual(self.table(two, passes=2)[0][0][1], "PASS")
        self.assertEqual(self.table(two[:2], passes=2)[0][0][1], "FAIL")
        broken = self.table([result("x", False, error="RuntimeError: the server died")])
        self.assertEqual((broken[0][0][1], broken[1]), ("ERROR", False))


class FakeServer:
    """What the client sees of the live server: after speech and 700 ms of silence it sends one turn's control messages
    and a short reply, and when `news_after` is set it says news by itself that long after a turn."""

    def __init__(self, news_after=None):
        self.turns, self.speaking, self.quiet, self.news_after = 0, False, 0, news_after

    async def handle(self, ws):
        await ws.send(json.dumps({"type": "ready", "mode": "vad", "silence_ms": 700}))
        async for message in ws:
            if isinstance(message, str):
                data = json.loads(message)
                if data.get("type") == "ping":
                    await ws.send(json.dumps({"type": "pong", "t": data["t"]}))
                continue
            loud = np.abs(np.frombuffer(message, dtype="<i2")).max() > 500
            if loud:
                self.speaking, self.quiet = True, 0
            elif self.speaking:
                self.quiet += 20
                if self.quiet >= 700:
                    self.speaking = False
                    await self.reply(ws)

    async def reply(self, ws):
        self.turns += 1
        n = self.turns
        await ws.send(json.dumps({"type": "turn_start", "turn": n}))
        await ws.send(json.dumps({"type": "turn_end", "turn": n, "trailing_ms": 700}))
        await ws.send(json.dumps({"type": "reply_start", "turn": n, "rate": 24000}))
        await ws.send(b"\x00\x00" * 4800)
        await ws.send(json.dumps({"type": "reply_end", "turn": n}))
        done = {"type": "turn_done", "turn": n, "transcript": "x", "replies": ["ok"], "rows": [], "metrics": {},
                "cancelled": None}
        await ws.send(json.dumps(done))
        if self.news_after is not None and n == 2:
            await asyncio.sleep(self.news_after)
            self.turns += 1
            await ws.send(json.dumps({"type": "turn_done", "turn": self.turns, "idle": True, "replies": ["news"],
                                      "rows": ["done-event"], "metrics": {}, "cancelled": None}))


class Holding(unittest.TestCase):
    def clips(self, tmp, names):
        out = {}
        for name in names:
            path = Path(tmp) / f"{name}.wav"
            tone = (np.sin(np.linspace(0, 2000, 4800)) * 12000).astype("<i2")
            sf.write(str(path), tone, 16000, subtype="PCM_16")
            out[name] = path
        return out

    def hold(self, script, names, news_after=None):
        with tempfile.TemporaryDirectory() as tmp:
            secret = Path(tmp) / "secret"
            secret.write_text("s", encoding="utf-8")
            fake = FakeServer(news_after)
            seen = []

            async def main():
                async with websockets.serve(fake.handle, "127.0.0.1", 0) as server:
                    port = server.sockets[0].getsockname()[1]
                    args = R.client_module.build_parser().parse_args(
                        ["--server", f"ws://127.0.0.1:{port}", "--secret-file", str(secret), "--log-dir", tmp,
                         "--output", "null", "--no-record"])
                    caller = R.CallClient(args, script(seen), self.clips(tmp, names), lambda n: SimpleNamespace(n=n))
                    await asyncio.wait_for(caller.run(), 60)

            asyncio.run(main())
            return seen, fake

    def test_the_caller_picks_its_next_line_from_what_the_server_did(self):
        def script(seen):
            heard = yield R.Say("a")
            seen.append([t.n for t in heard.turns])
            heard = yield R.Say("b" if heard.turns else "c")
            seen.append([t.n for t in heard.turns])

        seen, fake = self.hold(script, ["a", "b", "c"])
        self.assertEqual((seen, fake.turns), ([[1], [2]], 2))

    def test_a_line_said_over_the_reply_ends_the_step_with_its_own_turn(self):
        def script(seen):
            heard = yield R.Say("a", over=("b", 300))
            seen.append([t.n for t in heard.turns])

        seen, fake = self.hold(script, ["a", "b"])
        self.assertEqual((seen, fake.turns), ([[1, 2]], 2))

    def test_a_caller_waits_for_news_the_assistant_says_by_itself(self):
        def script(seen):
            yield R.Say("a")
            yield R.Say("a")
            heard = yield R.WaitNews(20)
            seen.append((heard.timed_out, [t.n for t in heard.turns]))

        seen, _ = self.hold(script, ["a"], news_after=1.0)
        self.assertEqual(seen, [(False, [3])])

    def test_waiting_for_news_that_never_comes_runs_out(self):
        def script(seen):
            yield R.Say("a")
            heard = yield R.WaitNews(1.5)
            seen.append(heard.timed_out)

        seen, _ = self.hold(script, ["a"])
        self.assertEqual(seen, [True])


if __name__ == "__main__":
    unittest.main()
