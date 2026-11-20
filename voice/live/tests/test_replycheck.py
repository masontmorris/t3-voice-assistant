"""The check of a free reply: what it does about a wrong claim depends on where the caller is, and it never waits on
the reply or the caller."""

import unittest

from voice.live.replycheck import ReplyCheck, _heard_words
from voice.t3.verify import Claim

WRONG = Claim(0.9, 0.05)
FINE = Claim(0.1, 0.05)
CHAT = Claim(0.05, 0.9)
REPLY = "That thread will carry on now. Let me know if you need anything else from me today."
FACTS = ("t1", {"thread": "Example Thread", "status": "waiting_on_you"})


class Rig:
    """The live server's view of one reply, scripted. `reading` maps each text the check judges to its Claim."""

    def __init__(self, reading=None, found=FACTS, playing=(1.0, 6.0), muted=False, speaking=False, update="update"):
        self.reading = reading or {REPLY: WRONG}
        self.found, self.state = found, dict(playing=playing, muted=muted, speaking=speaking)
        self.update, self.judged, self.stopped = update, [], 0

    def check(self):
        def judge(text, facts):
            self.judged.append(text)
            return self.reading.get(text, CHAT)

        def stop():
            self.stopped += 1

        return ReplyCheck(facts=lambda: self.found, judge=judge, correction=lambda thread, text: self.update,
                          playing=lambda: self.state["playing"], muted=lambda: self.state["muted"],
                          speaking=lambda: self.state["speaking"], stop_playback=stop)


class WrongReply(unittest.IsolatedAsyncioTestCase):
    async def run_check(self, rig):
        check = rig.check()
        check.watch(REPLY)
        plans = await check.settled()
        return plans, check.take_records()

    async def test_watching_never_waits_for_the_verdict(self):
        check = Rig().check()
        check.watch(REPLY)
        self.assertEqual(len(check.tasks), 1, "the check is a task beside the reply, not a step in front of it")
        await check.settled()

    async def test_a_reply_that_says_nothing_wrong_plans_nothing(self):
        for claim in (FINE, CHAT):
            with self.subTest(claim=claim):
                rig = Rig({REPLY: claim})
                plans, records = await self.run_check(rig)
                self.assertEqual(plans, [])
                self.assertEqual(rig.stopped, 0)
                self.assertEqual(records[0]["action"], "none")

    async def test_a_wrong_claim_while_the_reply_plays_stops_the_rest_and_is_corrected_in_its_place(self):
        rig = Rig()
        plans, records = await self.run_check(rig)
        self.assertEqual([(p.update, p.hold) for p in plans], [("update", False)])
        self.assertEqual(rig.stopped, 1)
        self.assertEqual(records[0]["action"], "replaced the rest")
        self.assertEqual(records[0]["stopped_audio_s"], 5.0)

    async def test_a_wrong_claim_after_the_reply_has_played_is_corrected_as_a_follow_up(self):
        rig = Rig(playing=(6.0, 6.0))
        plans, records = await self.run_check(rig)
        self.assertEqual([(p.update, p.hold) for p in plans], [("update", False)])
        self.assertEqual(rig.stopped, 0, "nothing is left to stop")
        self.assertEqual(records[0]["action"], "follow-up")

    async def test_a_wrong_claim_while_the_caller_is_talking_waits_for_a_quiet_moment(self):
        rig = Rig(speaking=True)
        plans, records = await self.run_check(rig)
        self.assertEqual([(p.update, p.hold) for p in plans], [("update", True)])
        self.assertEqual(rig.stopped, 0, "the caller's own barge-in already owns the audio")
        self.assertEqual(records[0]["action"], "held")

    async def test_a_reply_cut_off_before_its_claim_is_never_corrected(self):
        early = " ".join(REPLY.split()[:1])
        rig = Rig({REPLY: WRONG, early: CHAT}, playing=(0.4, 6.0), muted=True)
        plans, records = await self.run_check(rig)
        self.assertEqual(plans, [])
        self.assertEqual(rig.judged, [REPLY, early], "only the part they heard is judged again")
        self.assertEqual(records[0]["action"], "cut off before the claim")

    async def test_a_reply_cut_off_after_its_claim_is_held_for_a_quiet_moment(self):
        heard = _heard_words(REPLY, 3.0, 6.0)
        rig = Rig({REPLY: WRONG, heard: WRONG}, playing=(3.0, 6.0), muted=True)
        plans, records = await self.run_check(rig)
        self.assertEqual([(p.update, p.hold) for p in plans], [("update", True)])
        self.assertEqual(rig.stopped, 0)
        self.assertEqual(records[0]["action"], "held")

    async def test_a_reply_cut_off_before_any_of_it_played_is_never_corrected(self):
        rig = Rig(playing=(0.0, 6.0), muted=True)
        plans, records = await self.run_check(rig)
        self.assertEqual(plans, [])
        self.assertEqual(rig.judged, [REPLY], "nothing was heard, so there is nothing to judge again")

    async def test_no_thread_in_view_asks_the_decider_nothing(self):
        rig = Rig(found=None)
        plans, records = await self.run_check(rig)
        self.assertEqual((plans, rig.judged), ([], []))
        self.assertEqual(records[0]["action"], "no thread in view")

    async def test_a_check_that_did_not_finish_corrects_nothing(self):
        rig = Rig({REPLY: Claim(error="TimeoutError: slow")})
        plans, records = await self.run_check(rig)
        self.assertEqual(plans, [])
        self.assertEqual(records[0]["error"], "TimeoutError: slow")

    async def test_a_thread_that_cannot_be_read_again_is_not_corrected(self):
        rig = Rig(update=None)
        plans, records = await self.run_check(rig)
        self.assertEqual(plans, [])
        self.assertEqual(rig.stopped, 0, "audio is stopped only when a correction is ready to replace it")
        self.assertEqual(records[0]["action"], "thread unreadable")

    async def test_a_check_that_raises_plans_nothing(self):
        def boom():
            raise RuntimeError("seam down")

        check = Rig().check()
        check.facts = boom
        check.watch(REPLY)
        self.assertEqual(await check.settled(), [])


class HeardWords(unittest.TestCase):
    def test_the_share_of_the_audio_that_played_is_the_share_of_the_words_heard(self):
        self.assertEqual(_heard_words("a b c d", 2.0, 4.0), "a b")
        self.assertEqual(_heard_words("a b c d", 9.0, 4.0), "a b c d")
        self.assertEqual(_heard_words("a b c d", 0.0, 4.0), "")
        self.assertEqual(_heard_words("a b c d", 0.1, 4.0), "a", "once any played, the first word was heard")
        self.assertEqual(_heard_words("", 1.0, 2.0), "")


if __name__ == "__main__":
    unittest.main()
