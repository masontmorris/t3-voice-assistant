"""Rehearse the T3 call through the real live server, with a scripted T3 in place of the fork environment.

    python -m voice.live.rehearse drive --out DIR [--calls 10] [--port 8771]
    python -m voice.live.rehearse summary --out DIR
    python -m voice.live.rehearse drive --mixed --out DIR [--calls 5] [-- extra server arguments]
    python -m voice.live.rehearse drive --which --out DIR [--calls 5]
    python -m voice.live.rehearse drive --noise --out DIR [--calls 5]
    python -m voice.live.rehearse drive --follow --out DIR [--calls 5]
    python -m voice.live.rehearse drive --more --out DIR [--calls 3]

The score is threads started, of N calls shaped like the call this lane is fixing. `drive` starts `serve` as a child,
renders each call's turns with the server's own TTS, and sends them with the WAV client (to a null output, so nothing plays), one call
after another on one server. Each call is two chat turns, a long hesitant request, a yes, a question about the thread,
and a yes to the thread being confirmed. The server is the real one (voice/live/server.py: LFM3, the segmenter and
decider, the T3 task service, the call-voice relay, the checks); only the seam it reads T3 through is scripted
(voice/t3/fakes.py), so a launch starts nothing and costs nothing.

Calls 1 to 5 are the visible half, 6 to 10 the first held-out half (A), 11 to 15 a second one (B), written after A had
been looked at for causes, and 16 to 20 a third (C), written after B had been run. Each was written once; fixes are
tuned against the visible half only. Report all of them.

A thread counts as started only when the scripted seam saw a launch whose payload is this call's request (most of its
words are in what the launch carried) in the sandbox project, during this call. The seam knows no thread until a launch
makes it. A line the voice said is judged against the seam's record: "a new coding thread has started" is true only
after such a launch, and a status line is true only when it names the thread this call launched. `summary` reads the
records and says, per call, how far it got: dispatched (the request became a launch act), asked (that question was
spoken), yes read (the answer became a launch act), started (the seam saw this call's launch), status answered, and
where the first break was.

`--mixed` runs the mixed calls instead, shaped like the 2026-11-19 call: two chat turns, a weather lookup and a thread
request in one breath, a yes, a weather lookup and a question about the thread in one breath, and a yes. Calls 1, 2 and
4 ask for the lookup first, calls 3 and 5 for the thread. `summary` adds, per turn that had both a lookup and a T3
line, whether the T3 line was said on its first relay, whether a reply said the lookup's forecast high, and whether
both were heard. A lookup reply that names a thread is listed for reading, since nothing in code can tell what it says
about one.

`--which` runs calls about threads that already exist, shaped like the 2026-11-19 afternoon call. The seam starts
with three threads per call: two whose titles are nearly the same and one apart from them. After two chat turns, a
status question fits both of the near-identical titles, the person answers the question that follows by place ("The
second one."), then asks about the third thread by name, and closes. The thread chosen by place and the third
thread are both interrupted with no last reply, so a by-place answer and a direct question get the same kind of status
line. `summary` adds, per call, whether the which question was asked and whether each status line was said on its
first relay.

`--noise` runs calls about two threads that already exist, with sound the endpointer takes for speech. After a chat
turn, a status question about the first thread is followed by a burst of road-like noise in the gap where the reply is
being made: brown noise, low-passed, swelling and fading, generated here at a level relative to the question's own
speech (no recorded audio). Then a question about the second thread comes in two parts with a pause between, which
must still be joined into one turn, and an acknowledgement. `summary` adds, per call, whether the first thread's
status line was said, how many turns were cancelled as continued, and whether the two-part question became one turn
whose status line was said.

`--follow` runs calls about one of two threads that already exist. After a chat turn, a status question names the
thread; the follow-ups after it do not name it ("Is it done yet?", "Tell it to also add tests."), and a lookup or a
thanks between them is about nothing in T3. `summary` adds, per follow-up about the thread, whether its turn said a
checked T3 line of the right kind about that thread, per other turn whether T3 said anything in it, and per status
said again whether its act said that nothing is new.

`--more` runs the same kind of call, shaped like the 2026-11-19 evening call: a thread question worded the way a
transcript garbles it, a follow-up asking why the thread stopped, and requests for more detail, about a thread that
stopped or still works and has a last reply. `summary` adds, per turn about the thread, whether the caller heard a line
that was not a checked T3 line (a free answer in place of T3's), and per status line whether its reply carried the
summary its act held.

`Rig` and `CallClient` are also what the whole-call scenarios run on (voice/live/scenarios.py): a caller that picks its
next line from what the server did, against the real T3 seam.

Needs the GPU (run it inside the lease), VOICE_ENV_FILE for the decider key, and writes only under --out. The turns
are synthetic speech; the T3 text model and the general agent are off, so no OpenRouter call is made. With
`--summaries` the local LFM3-1.7B writes thread summaries, as the page chose on the 2026-11-19 calls; without it no
status line carries one.
"""

import argparse
import asyncio
import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from voice.live import client as client_module
from voice.live import server
from voice.live.t3_tasks import LOCAL_SUMMARISER
from voice.t3 import fakes as F
from voice.t3 import lifecycle as L
from voice.t3 import types as T
from voice.t3.journal import T3Journal

VOICE = Path(os.environ.get("VOICE_HOME", Path.home() / "voice"))
VISIBLE = 5
CHAT = (("Hey there, how are things?", "Pretty good, how about you?"),
        ("Hello there.", "Not bad, just getting set up for the day."),
        ("Hi, can you hear me?", "Great, how has your morning been?"), ("Hey!", "Doing fine, thanks. And you?"),
        ("Good morning.", "Pretty good, a bit tired honestly."),
        ("Morning!", "Pretty well, thanks, and you?"), ("Hi there.", "Can't complain, how's your day going?"),
        ("Hey, are you there?", "Good, I'm just checking in."), ("Hello again.", "Fine, thanks for asking."),
        ("Hey you.", "Not too bad, a bit of a slow start."),
        ("Hey, you there?", "Yeah, all good, you?"), ("Good afternoon.", "Afternoon! Busy day so far."),
        ("Hi!", "Doing well, thanks for asking."), ("Hello, hello.", "Not bad at all, how are you?"),
        ("Hey assistant.", "Pretty good, just warming up."),
        ("Morning, are you around?", "Yep, doing alright, you?"), ("Hey hey.", "Pretty decent, can't complain."),
        ("Hello, can you hear me okay?", "Loud and clear, how's it going?"),
        ("Hi again.", "Good thanks, ready when you are."), ("Hey, quick check-in.", "Sure, what's up?"))
THREADS = ("Clean up texture export for the game engine", "Convert audio presets for the editor",
           "Find out why the nightly build is slow", "Fix the dropped spreadsheet columns",
           "Remember window sizes on the settings screen",
           "Reduce noisy logging in the importer", "Rename exported meshes with a script",
           "Find why shader compiles are slow", "Clean up the save slot writer", "Make build notes output markdown",
           "Fix the installer hang at ninety percent", "Tidy the config loader",
           "Find why the dashboard loads slowly", "Fix the typo checker flagging every word",
           "Add dark mode to the settings page",
           "Speed up the level loading screen", "Fix the controller rumble on pause",
           "Clean up the localization export", "Find the memory leak in the particle system",
           "Add a replay mode to the debug menu")
ASKS = ("How is the texture export thread going?", "How's the audio presets thread going?",
        "What is the nightly build thread up to?", "Is the spreadsheet columns thread done yet?",
        "How is the window sizes thread going?",
        "How is the importer logging thread going?", "What is the mesh renaming thread up to?",
        "Is the shader compile thread finished?", "How is the save slot thread going?",
        "How are the build notes going?",
        "How is the installer thread going?", "What's the config loader thread up to?",
        "Is the dashboard thread finished?", "How is the typo checker thread going?",
        "How's the dark mode thread going?",
        "How is the loading screen thread going?", "What's the controller rumble thread up to?",
        "Is the localization thread finished?", "How is the memory leak thread going?",
        "How's the replay mode thread going?")
REQUESTS = (
    "Uhm, I was wondering if you could start a thread to get work going on, uhm, a little project to make, uhm, "
    "textures export into the game engine cleanly? Can you kick that off and keep it tidy?",
    "So, uh, I want to get some work going on, uhm, a small project, a tool that converts audio presets into the "
    "format the editor wants, and, uhm, could you start a thread for that and, you know, keep it organized?",
    "Okay, so, uhm, I was thinking, uh, maybe we could have a thread that works out why the nightly build is so "
    "slow, sort of like a detective, a little project, if that makes sense? Can you kick that off for me?",
    "Right, uhm, here's the thing, there's this spreadsheet export that keeps dropping columns, and, uh, I would "
    "love a thread on that, a little investigation project, whenever you get a chance, no rush, but, yeah, please.",
    "Uh, so, quick one, well, not that quick, uhm, I need some work started on the, the settings screen, the part "
    "that remembers window sizes, a small project really, and, uh, could you open a thread for it and sort it out?",
    "Um, so I've been meaning to, uh, get somebody on the, the logging stuff in the importer, because it's, like, "
    "really noisy right now, and, uhm, would you be able to open a thread for that and, you know, drive it?",
    "Okay, so, uh, there's this little side project, uhm, a script that renames the exported meshes, and I'd love, "
    "uh, a thread started on it, if that's not too much trouble.",
    "Right, so, uhm, how do I put this, the shader compile step takes forever, and, uh, I want a thread to dig into "
    "why, sort of a, a mini project, can you start that?",
    "Uh, hey, so, quick thing, well, a few things, uhm, I need the save system cleaned up, the, the part that writes "
    "slots, and could you, uh, spin up a thread for it please?",
    "So, uhm, this might sound odd, but I'd like to, uh, kick off a thread about, uh, making the build notes "
    "generator output markdown, a small project, nothing urgent.",
    "Hi, so, uh, I keep running into this thing where, um, the installer hangs at ninety percent, and I was hoping, "
    "uh, you could start a thread to look into it, like a proper investigation.",
    "Okay, uhm, so we've got this config loader, right, and it's, it's a mess, so, uh, can you spin up a thread to "
    "tidy it, maybe a small project, nothing huge.",
    "Um, right, so, the thing I want, the dashboard is slow to load, and, uhm, I'd like a thread on that, to figure "
    "out where the time goes, if you can.",
    "So, uh, I need a favor, kind of, a thread for fixing the typo checker, the, the one that flags every other "
    "word, could you open that up for me?",
    "Alright, uhm, here's what I'm thinking, a thread to add dark mode to the settings page, a small project, and, "
    "uh, go ahead and kick it off whenever.",
    "Um, okay, so the level loading screen is, like, painfully slow, and I was thinking, uh, could you start a thread "
    "on that, just to see what's going on, nothing fancy?",
    "Right, uhm, there's this bug where the controller keeps rumbling when you pause, and, uh, I'd really like a "
    "thread on that, if you could get that going.",
    "So, uh, I have this, this localization export, and it's kind of a mess, a small project really, and, uhm, would "
    "you open a thread to clean that up for me?",
    "Okay, so, this is a bit of a long one, uhm, we have a memory leak somewhere in the particle system, I think, "
    "and, uh, I need a thread for tracking it down, thorough, but not crazy.",
    "Hey, uh, quick idea, a replay mode, in the debug menu, uhm, so we can scrub through a session, can you start a "
    "thread to build that?")
YES = ("Please do.", "Yes, please do.", "Yeah, go ahead.", "Please do.", "Yes please.",
       "Please do?", "Yeah, go ahead.", "Sure.", "Yes, do it.", "Please do.",
       "Please do.", "Yeah, go ahead.", "Sure.", "Please do?", "Yes, do it.",
       "Please do.", "Yeah, go ahead.", "Sure.", "Please do?", "Yes, do it.")
assert len(CHAT) == len(THREADS) == len(ASKS) == len(REQUESTS) == len(YES)
# The mixed calls reuse calls 1 to 5's chat, threads and yeses. Written once, before any run of them.
MIXED_REQUESTS = (
    "Could you tell me the weather in Denver tomorrow, and then also start a thread to clean up the texture export "
    "for the game engine?",
    "Uhm, what's the weather going to be in Seattle tomorrow, and could you start a thread on converting the audio "
    "presets for the editor?",
    "Start a thread to find out why the nightly build is slow, and also, what's the weather in Chicago tomorrow?",
    "Can you check tomorrow's weather in Portland and kick off a thread to fix the dropped spreadsheet columns?",
    "Could you open a thread for remembering window sizes on the settings screen, and tell me the weather in "
    "Atlanta tomorrow?")
MIXED_ASKS = ("What's the weather in Boston tomorrow, and how is the texture export thread going?",
              "What's the weather in Austin tomorrow, and how's the audio presets thread going?",
              "What's the weather like in Miami tomorrow, and what is the nightly build thread up to?",
              "What's the weather in Phoenix tomorrow, and is the spreadsheet columns thread done yet?",
              "How is the window sizes thread going, and what's the weather in Nashville tomorrow?")


# The which calls: (near-identical title, its twin, the third thread, the question, the answer by place, the question
# about the third thread). The answer picks the twin for "second" and the first title for "first". Written once,
# before any run of them.
WHICH = (
    ("Add a Volume Slider", "Add volume slider to the settings page", "Speed Up Shader Warmup",
     "How is the volume slider thread going?", "The second one.", "And how is the shader warmup thread going?"),
    ("Fix the Pause Menu", "Fix pause menu in the sandbox project", "Clean Up Log Output",
     "How's the pause menu thread doing?", "The first one.", "What about the log output thread?"),
    ("Add a Retry Button", "Add retry button to the error screen", "Profile the Asset Loader",
     "How is the retry button thread going?", "The second one.", "And how is the asset loader thread going?"),
    ("Rename the Config Keys", "Rename config keys in the sandbox project", "Trim the Startup Logo",
     "How is the config keys thread going?", "The first one.", "How is the startup logo thread going?"),
    ("Add a Mute Toggle", "Add mute toggle next to volume", "Fix the Crash on Exit",
     "How's the mute toggle thread going?", "The second one.", "And how is the crash on exit thread going?"))


# The noise calls: (thread asked about before the noise, its question, gap after speech before the noise starts in s,
# noise length in s, noise level in dB against the question's speech, thread asked about in two parts, first part,
# second part). Written once, before any run of them.
NOISY = (
    ("Tidy the Shader Cache", "How is the shader cache thread going?", 1.0, 4.4, -6.0,
     "Fix the Footstep Sounds", "How is the footstep sounds thread going,", "and is it close to done?"),
    ("Speed Up the Map Loader", "How is the map loader thread going?", 1.2, 3.0, -9.0,
     "Add a Photo Mode", "What is the photo mode thread up to,", "and has it saved anything yet?"),
    ("Clean Up the Save Format", "What's the save format thread up to?", 1.1, 2.5, -4.0,
     "Fix the Minimap Zoom", "How is the minimap zoom thread doing,", "now that it has been a while?"),
    ("Profile the Audio Mixer", "How is the audio mixer thread going?", 1.3, 3.6, -12.0,
     "Add Controller Remapping", "Is the controller remapping thread done,", "or is it still going?"),
    ("Trim the Intro Video", "How's the intro video thread doing?", 1.0, 2.0, -6.0,
     "Fix the Pause Screen Blur", "How is the pause screen blur thread going,", "and did it find the cause?"))
# The follow-up calls: (the thread asked about, the other thread, its last reply, the turns after the chat line, each
# with what it is: a T3 tool, "yes" to the question before it, or "none" for a turn about nothing in T3). The first
# turn names the thread; no later one does. Written once, before any run of them.
FOLLOW = (
    ("Tidy the Save Slots", "Add a Night Mode", "I merged the slot list and I'm writing the migration now.",
     (("How is the save slots thread going?", "t3_ask"), ("Give me more detail on that.", "t3_ask"),
      ("Tell it to also add tests.", "t3_tell"), ("Yes, send it.", "yes"), ("Okay, thanks.", "none"))),
    ("Fix the Jump Timing", "Clean Up the Input Code", "The jump now uses the fixed step. I'm checking the slopes.",
     (("What's the jump timing thread up to?", "t3_ask"), ("Is it done yet?", "t3_ask"),
      ("What's the weather in Denver tomorrow?", "none"), ("Thanks, that's all.", "none"))),
    ("Speed Up Level Loading", "Fix the Pause Screen", "Loading the second level dropped from four seconds to two.",
     (("How is the level loading thread going?", "t3_ask"), ("What did it say?", "t3_ask"), ("Stop it.", "t3_stop"),
      ("Yes, stop it.", "yes"), ("Okay, great.", "none"))),
    ("Add Controller Rumble", "Trim the Credits Scroll", "Rumble works on the first controller. I'm adding the second.",
     (("How's the controller rumble thread doing?", "t3_ask"), ("How's that one going now?", "t3_ask"),
      ("Ask it to make the rumble a bit softer.", "t3_tell"), ("Yes please.", "yes"), ("Cool, thank you.", "none"))),
    ("Clean Up the Audio Settings", "Fix the Map Markers", "I removed the unused sliders and I'm renaming the rest.",
     (("How is the audio settings thread going?", "t3_ask"), ("Any update on that?", "t3_ask"),
      ("How are you doing today?", "none"), ("Did it finish?", "t3_ask"), ("Thanks.", "none"))),
    # Calls 6 and 7 answer the question back and then ask for more than the status said, written after run 1 had shown
    # that a follow-up is confirmed back, and before any run of them.
    ("Polish the Main Menu", "Fix the Shadow Flicker", "I moved the buttons into one column and I'm fixing the focus order.",
     (("How is the main menu thread going?", "t3_ask"), ("Fill me in on the rest of it.", "t3_ask"), ("Yes.", "yes"),
      ("Anything else new with it?", "t3_ask"), ("Yes, that one.", "yes"), ("Okay, thanks.", "none"))),
    ("Batch the Sprite Draws", "Add a Replay Camera", "Draw calls are down by half on the forest level.",
     (("What's the sprite draws thread up to?", "t3_ask"), ("What else has it done?", "t3_ask"), ("Yes.", "yes"),
      ("Is there any more news on it?", "t3_ask"), ("Yes.", "yes"), ("Great, thank you.", "none"))))
# The more calls, in the follow-up calls' shape with the thread's activity added: a question worded as a transcript
# garbles it, why it stopped, and more detail. Written once, before any run of them.
MORE = (
    ("Tune the Camera Shake", "Add a Photo Booth", "I lowered the shake on landings and I'm testing explosions next.",
     T.ThreadActivity.INTERRUPTED,
     (("How is the camera shake thread going?", "t3_ask"), ("Do you know why it stopped?", "t3_ask"),
      ("Walk me through what it actually did.", "t3_ask"), ("Okay, thanks.", "none"))),
    ("Sort the Inventory Icons", "Fix the Rain Sound", "I grouped the icons by type and I'm sorting the potions now.",
     T.ThreadActivity.WORKING,
     (("Um, could you say how this sort our icons thread been doing?", "t3_ask"),
      ("Tell me more about how it's going.", "t3_ask"), ("Cool, thank you.", "none"))),
    ("Speed Up the Save Screen", "Add Wind to the Grass", "I cached the slot previews, so the screen opens twice as fast.",
     T.ThreadActivity.CANCELLED,
     (("How's the save screen, were it doing?", "t3_ask"), ("Could you not pull up a recap?", "t3_ask"),
      ("No, pull up a recap.", "t3_ask"), ("Thanks.", "none"))))
# The T3 lines that answer each kind of turn about the thread.
ANSWERS = {"t3_ask": {"thread_status", "confirm_thread", "ask_which"}, "t3_tell": {"confirm_tell"}, "t3_stop": {"confirm_stop"},
           "yes": {"sent_followup", "held_followup", "tell_pending", "tell_unconfirmed", "stopping", "nothing_running",
                   "stop_unconfirmed", "thread_status"}}
CALL_GAP_S = 4.0
PAUSE_S = 0.9  # past the endpointer's 700 ms of silence, so the first part ends a turn and the second continues it


def noise_threads():
    return {f"n{call + 1}_{n + 1}": (title, T.ThreadActivity.WORKING)
            for call, row in enumerate(NOISY) for n, title in enumerate((row[0], row[5]))}


def road_noise(seconds, rate, rms, seed):
    """Road-like noise: brown noise, low-passed and centred, under a slow swell, at `rms` (int16 scale)."""
    import numpy as np

    rng = np.random.default_rng(seed)
    n = int(seconds * rate)
    brown = np.cumsum(rng.standard_normal(n))
    brown -= np.convolve(brown, np.ones(rate // 10) / (rate // 10), mode="same")
    smooth = np.convolve(brown, np.ones(8) / 8, mode="same")
    swell = np.sin(np.linspace(0, np.pi, n)) ** 0.5
    out = smooth * swell
    out *= rms / max(np.sqrt(np.mean(out ** 2)), 1e-9)
    return np.clip(out, -32767, 32767).astype("<i2")


def noise_pieces(call):
    """The lines the TTS renders for one noise call, in order."""
    _, ask, *_, first, second = NOISY[call]
    return [CHAT[call][0], ask, first, second, "Okay, thanks."]


def compose_noise_call(call, clips, out):
    """The call's turns from the rendered pieces: the chat line, the question with silence then noise after it, the
    two-part question with a pause, and the acknowledgement. Written as WAVs in `out`, in order."""
    import numpy as np
    import soundfile as sf

    _, _, gap_s, noise_s, level_db, *_ = NOISY[call]
    pieces = [sf.read(str(path), dtype="int16") for path in clips]
    rate = pieces[0][1]
    chat, ask, first, second, thanks = (np.asarray(pcm).reshape(-1) for pcm, _ in pieces)
    speech_rms = float(np.sqrt(np.mean(ask.astype(np.float64) ** 2)))
    burst = road_noise(noise_s, rate, speech_rms * 10 ** (level_db / 20), seed=call)
    quiet = np.zeros(int(gap_s * rate), dtype="<i2")
    pause = np.zeros(int(PAUSE_S * rate), dtype="<i2")
    turns = [chat, np.concatenate([ask, quiet, burst]), np.concatenate([first, pause, second]), thanks]
    out.mkdir(parents=True, exist_ok=True)
    for n, pcm in enumerate(turns, 1):
        sf.write(str(out / f"turn_{n:03d}.wav"), pcm, rate, subtype="PCM_16")
    return sorted(out.glob("turn_*.wav"))


def follow_threads():
    """The seam's threads for the follow-up calls, as id to (title, activity, last reply): both working."""
    return {f"f{call + 1}_{n + 1}": (title, T.ThreadActivity.WORKING, reply if n == 0 else None)
            for call, (first, other, reply, _) in enumerate(FOLLOW) for n, title in enumerate((first, other))}


def more_threads():
    """The seam's threads for the more calls: the one asked about in its activity with its last reply, the other
    working."""
    return {f"m{call + 1}_{n + 1}": (title, activity if n == 0 else T.ThreadActivity.WORKING, reply if n == 0 else None)
            for call, (first, other, reply, activity, _) in enumerate(MORE) for n, title in enumerate((first, other))}


def which_threads():
    """The seam's threads for the which calls, as id to (title, activity): the thread the answer picks and the third
    thread are interrupted, the other twin is working."""
    threads = {}
    for call, (first, twin, third, _, answer, _) in enumerate(WHICH):
        picked = twin if "second" in answer else first
        for n, title in enumerate((first, twin, third)):
            activity = T.ThreadActivity.WORKING if title not in (picked, third) else T.ThreadActivity.INTERRUPTED
            threads[f"w{call + 1}_{n + 1}"] = (title, activity)
    return threads


def turns(call, tail=True, mixed=False, which=False, noisy=False, follow=False, more=False):
    """One call's turns as text, in the order the person says them (for the noise calls, the pieces rendered)."""
    if follow or more:
        return [CHAT[call][0], *(said for said, _ in (MORE[call][4] if more else FOLLOW[call][3]))]
    if noisy:
        return noise_pieces(call)
    if which:
        *_, ask, answer, direct = WHICH[call]
        return [*CHAT[call], ask, answer, direct, "Okay, thanks."]
    if mixed:
        return [*CHAT[call], MIXED_REQUESTS[call], YES[call], *((MIXED_ASKS[call], "Yes.") if tail else ())]
    return [*CHAT[call], REQUESTS[call], YES[call], *((ASKS[call], "Yes.") if tail else ())]


FILLER = frozenset({"uh", "uhm", "um", "so", "okay", "right", "and", "the", "a", "to", "it", "that", "i", "you", "if",
                    "of", "on", "in", "for", "can", "could", "would", "be", "is", "my", "me", "this", "there"})


def words_of(text):
    return {w for w in "".join(c if c.isalnum() or c == "'" else " " for c in (text or "").lower()).split()
            if w not in FILLER}


def overlap(request, payload):
    """The share of `request`'s content words that `payload` carries."""
    want = words_of(request)
    return len(want & words_of(payload)) / len(want) if want else 0.0


class ScriptedSeam(F.FakeSeam):
    """The seam a call reads: one sandbox project, and no thread until a launch makes one. A launch is logged with the
    time it came, its payload, and which call's request that payload is."""

    def __init__(self, log_path: Path, requests=REQUESTS, threads=None):
        super().__init__()
        self.log_path, self.requests = log_path, requests
        self.launches = []
        self.threads = {tid: title for tid, (title, *_) in (threads or {}).items()}
        self.activity = {tid: activity for tid, (_, activity, *_) in (threads or {}).items()}
        self.replies = {tid: T.LastReply(rest[0], f"m_{tid}", None, f"r_{tid}", "2026-11-18T10:00:00Z")
                        for tid, (_, _, *rest) in (threads or {}).items() if rest and rest[0]}

    @property
    def listing(self):
        return T.ThreadList(tuple(T.ThreadSummary(tid, "p1", title, self.activity.get(tid, T.ThreadActivity.WORKING),
                                                  "2026-11-18T10:00:00Z", False)
                                  for tid, title in self.threads.items()),
                            len(self.threads), False, 200)

    @listing.setter
    def listing(self, _):
        pass

    def read_thread(self, thread_id):
        self.calls.append(("read", thread_id))
        return F.state(thread=thread_id, title=self.threads[thread_id], as_of=210,
                       activity=self.activity.get(thread_id, T.ThreadActivity.WORKING),
                       last_reply=self.replies.get(thread_id))

    def send(self, request_id, thread_id, text):
        self.calls.append(("send", request_id, thread_id, text))
        return F.sent(thread=thread_id, sequence=211)

    def interrupt(self, request_id, thread_id):
        self.calls.append(("interrupt", request_id, thread_id))
        return F.requested(sequence=211)

    def drain(self, timeout=None):
        time.sleep(min(timeout or 0.5, 0.5))
        return iter(())

    def launch(self, request_id, project_id, instruction):
        self.calls.append(("launch", request_id, project_id, instruction))
        best = max(range(len(self.requests)), key=lambda i: overlap(self.requests[i], instruction))
        thread = f"t{best + 1}"
        self.threads[thread] = THREADS[best]
        self.launches.append({"at": time.time(), "project": project_id, "instruction": instruction, "call": best,
                              "overlap": round(overlap(self.requests[best], instruction), 2), "thread": thread})
        self.log_path.write_text(json.dumps(self.launches), encoding="utf-8")
        return F.launched(thread=thread, run=f"r{best + 1}", sequence=93 + len(self.launches))


def serve(args, rest):
    """The live server with its T3 seam replaced and no T3 text model, or with `--summaries` the local one. With
    `--scenario` the seam is the real one, to the T3 the environment's token and origin name."""
    seam = None if args.scenario else ScriptedSeam(
        args.out / "seam-calls.json", MIXED_REQUESTS if args.mixed else REQUESTS,
        which_threads() if args.which else noise_threads() if args.noise
        else follow_threads() if args.follow else more_threads() if args.more else None)
    opened = []
    real_open = server.T3Tasks.open.__func__

    def open_t3(cls, origin, token_file, journal_path, model=None, decider=None, **options):
        if seam is None:
            opened.append(real_open(cls, origin, token_file, journal_path, model, decider, **options))
        else:
            opened.append(cls(L.Follow(T3Journal(journal_path), F.Connector(seam)), None, decider, **options))
        return opened[-1]

    choices = server.summariser_choices

    def local_summaries(local_url=None, gate_url=None):
        offered = choices(local_url, gate_url)
        if args.summaries:
            if not local_url:
                raise SystemExit("--summaries needs the local summariser, which did not start")
            opened[-1].summarisers = offered
            opened[-1].use_summariser(LOCAL_SUMMARISER)
        return offered

    server.T3Tasks.open = classmethod(open_t3)
    server.summariser_choices = local_summaries
    server.default_model = lambda: None
    if args.cpu:
        real = server.Server
        server.Server = lambda port, log, extra, slots: real(port, log, (*extra, "-ngl", "0", "--no-mmproj-offload"), slots)
    sys.argv = ["voice.live.server", *rest]
    server.main()


class Rig:
    """The live server as a child process, up for as many calls as a drive makes, and gone when the drive ends. A
    file `STOP` in `out` ends it, as it does for every serve."""

    def __init__(self, out, port, max_minutes, flags=(), rest=(), env=None):
        out.mkdir(parents=True, exist_ok=True)
        self.out, self.port = out, port
        self.env = dict(os.environ, VOICE_T3_JOURNAL=str(out / "t3-journal.json"), VOICE_AGENT="off",
                        PYTHONIOENCODING="utf-8", **(env or {}))
        self.stop = out / "STOP"
        self.command = [sys.executable, "-u", "-m", "voice.live.rehearse", "serve", "--out", str(out), *flags, "--",
                        "--host", "127.0.0.1", "--port", str(port), "--t3", "on", "--snapshot", "note",
                        "--max-minutes", str(max_minutes), "--out", str(out / "server"), "--stop-file", str(self.stop),
                        "--decider-cache", str(out / "decider-cache"), *rest]
        self.ready, self.lines, self.child = threading.Event(), [], None

    def __enter__(self):
        self.child = subprocess.Popen(self.command, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                      text=True, encoding="utf-8")

        def read():
            with (self.out / "server.out").open("w", encoding="utf-8", buffering=1) as log:
                for line in self.child.stdout:
                    log.write(line)
                    self.lines.append(line)
                    if line.startswith("live server on "):
                        self.ready.set()
            self.ready.set()

        threading.Thread(target=read, daemon=True).start()
        if not self.ready.wait(180) or not any(line.startswith("live server on ") for line in self.lines):
            self.__exit__()
            raise SystemExit("the live server did not come up, see server.out")
        return self

    def __exit__(self, *exc):
        self.stop.touch()
        try:
            self.child.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.child.terminate()
            self.child.wait(timeout=15)

    def render(self, text_file, clips, lfm_port):
        """Renders the lines of `text_file` to `clips` with the server's own TTS, in order."""
        subprocess.run([sys.executable, "-m", "voice.live.script", "--turns", str(text_file), "--out", str(clips),
                        "--server", f"http://127.0.0.1:{lfm_port}"], env=self.env, check=True,
                       stdout=text_file.with_suffix(".out").open("w"), stderr=subprocess.STDOUT)
        return sorted(clips.glob("turn_*.wav"))


def run_drive(args, rest=()):
    out = args.out
    flags = [*(["--cpu"] if args.cpu else []), *(["--mixed"] if args.mixed else []),
             *(["--which"] if args.which else []), *(["--noise"] if args.noise else []),
             *(["--follow"] if args.follow else []), *(["--more"] if args.more else []),
             *(["--summaries"] if args.summaries else [])]
    windows = []
    with Rig(out, args.port, args.max_minutes, flags, rest) as rig:
        client = VOICE / "venv-live-client/Scripts/python.exe"
        if not client.is_file():
            client = Path(sys.executable)
        for call in args.only or range(args.calls):
            text = out / f"call{call + 1}.txt"
            lines = turns(call, not args.no_tail, args.mixed, args.which, args.noise, args.follow, args.more)
            text.write_text("\n".join(lines) + "\n", encoding="utf-8")
            clips = (args.clips_from or out) / f"clips{call + 1}"
            if args.clips_from is not None:
                # Rendering gives the server time to close the call before; without it, wait as long.
                time.sleep(CALL_GAP_S)
            else:
                rig.render(text, clips, args.lfm_port)
            wavs = sorted(clips.glob("turn_*.wav"))
            if args.noise:
                wavs = compose_noise_call(call, wavs, out / f"composed{call + 1}")
            started = time.time()
            subprocess.run([str(client), "-m", "voice.live.client", "--server", f"ws://127.0.0.1:{args.port}",
                            "--secret-file", str(VOICE / "live/secret"), "--log-dir", str(out / f"client{call + 1}"),
                            "--output", "null", "--wav", *map(str, wavs)], env=rig.env, check=True, timeout=240,
                           stdout=(out / f"client{call + 1}.out").open("w"), stderr=subprocess.STDOUT)
            windows.append({"call": call + 1, "start": started, "end": time.time()})
            (out / "windows.json").write_text(json.dumps(windows), encoding="utf-8")
            print(f"call {call + 1} done in {time.time() - started:.0f} s", flush=True)


@dataclass(frozen=True)
class Say:
    """The caller says `line` (a key of the scenario's lines). With `over`, a pair of a line and a delay in ms, the caller
    then says that line over the reply that begins, that long after it starts."""

    line: str
    over: tuple[str, int] | None = None


@dataclass(frozen=True)
class WaitNews:
    """The caller is quiet until the assistant says news by itself, or `timeout_s` passes."""

    timeout_s: float = 90.0


@dataclass(frozen=True)
class Pause:
    seconds: float


@dataclass(frozen=True)
class Do:
    """Something happens in the world, off the call's loop. Its result comes back as `Heard.value`."""

    fn: Callable[[], Any]


@dataclass(frozen=True)
class Heard:
    """What the server did since the caller's last step: every turn's record in order (the caller's turns and the news
    the assistant said by itself), and the result of a `Do` or whether a `WaitNews` ran out."""

    turns: tuple[dict, ...]
    value: Any = None
    timed_out: bool = False


class CallClient(client_module.Client):
    """A synthetic caller that holds a whole call. `script` is a generator that yields what the caller does next and
    is sent what the server did; `clips` maps each line it may say to its rendered clip; `load` returns a turn's
    record by number, written before the server tells the client the turn is done."""

    def __init__(self, args, script, clips, load):
        self.script, self.clips, self.load = script, clips, load
        first = next(script)
        args.wav = [clips[first.line]]
        super().__init__(args)
        self.queued, self.heard, self.expect, self.over, self.news = [], [], True, None, None
        self.over_armed = False
        self.over = (clips[first.over[0]], first.over[1]) if first.over else None

    def on_control(self, message):
        kind = message.get("type")
        if kind == "reply_start" and self.over is not None and not self.over_armed:
            self.over_armed = True
            asyncio.get_running_loop().call_later(self.over[1] / 1000, self.say_over)
        if kind == "turn_done":
            self.heard.append(self.load(message["turn"]))
            if message.get("idle") and message.get("replies") and self.news is not None:
                self.news.cancel()
                self.news = None
                asyncio.get_running_loop().create_task(self.after_playback(lambda: self.resume(None, False)))
        super().on_control(message)

    def say_over(self):
        clip, self.over, self.over_armed = self.over[0], None, False
        self.send(clip)

    def send(self, clip):
        self.queued.append(clip)
        self.next_clip.set()

    def advance(self):
        # While a line waits to be said over the reply, the turn that began the reply is not the one that ends the step.
        if self.expect and self.over is None:
            self.expect = False
            asyncio.get_running_loop().create_task(self.after_playback(lambda: self.resume(None, False)))

    def resume(self, value, timed_out):
        turns, self.heard = tuple(t for t in self.heard if t is not None), []
        loop = asyncio.get_running_loop()
        try:
            action = self.script.send(Heard(turns, value, timed_out))
        except StopIteration:
            self.finished.set()
            return
        if isinstance(action, Say):
            self.expect = True
            self.over = (self.clips[action.over[0]], action.over[1]) if action.over else None
            self.send(self.clips[action.line])
        elif isinstance(action, WaitNews):
            self.news = loop.call_later(action.timeout_s, lambda: (setattr(self, "news", None),
                                                                    self.resume(None, True)))
        elif isinstance(action, Pause):
            loop.call_later(action.seconds, self.resume, None, False)
        elif isinstance(action, Do):
            loop.run_in_executor(None, action.fn).add_done_callback(lambda done: self.resume(done.result(), False))
        else:
            raise TypeError(f"a caller script yields Say, WaitNews, Pause or Do, not {action!r}")


def hold_call(rig, script, clips, load, log_dir, timeout_s=300):
    """Runs `script` as one call to the rig's server, in this process, with the reply audio going nowhere."""
    args = client_module.build_parser().parse_args(
        ["--server", f"ws://127.0.0.1:{rig.port}", "--secret-file", str(VOICE / "live/secret"), "--log-dir",
         str(log_dir), "--output", "null", "--no-record"])
    caller = CallClient(args, script, clips, load)
    asyncio.run(asyncio.wait_for(caller.run(), timeout_s))


ASKING = {"confirm_launch", "ask_which", "no_project"}
ANSWERED = {"launched", "launch_pending", "launch_unconfirmed", "refused", "cancelled"}


def entries(record):
    """The T3 lines a turn tried to say: the acts' kinds, whether the draft passed, and how the call's reply went."""
    found, current = [], None
    for event in record.get("timeline", []):
        if event["kind"] == "t3_words":
            kinds = [a["kind"] for a in event.get("acts") or ()]
            current = {"kinds": kinds, "nothing_new": any((a.get("slots") or {}).get("since_last_asked")
                                                          for a in event.get("acts") or ()),
                       "summary": next(((a.get("slots") or {}).get("summary") for a in event.get("acts") or ()
                                        if (a.get("slots") or {}).get("summary")), None),
                       "draft": event.get("line"), "draft_problems": event.get("problems") or
                       ([event["error"]] if event.get("error") else []), "reply": None, "status": None,
                       "delivery": "unsent", "relays": 0}
            found.append(current)
        elif event["kind"] == "t3_call_reply" and current is not None:
            current["reply"], current["status"] = event.get("text"), event.get("status")
            current["relays"] += 1
        elif event["kind"] == "t3_delivery" and current is not None:
            current["delivery"] = event.get("delivery")
    return found


def forecast_highs(record):
    """The high temperature of each weather lookup that answered in this turn, as the digits a reply would say."""
    highs = []
    for execution in (record.get("executions") or {}).values():
        summary, tool = execution.get("result_summary") or "", execution.get("tool") or {}
        if tool.get("tool") == "weather" and tool.get("outcome") == "ok" and "a high of " in summary:
            highs.append(summary.split("a high of ", 1)[1].split()[0])
    return highs


def free_lines(row):
    """What the caller heard in a turn that was not a checked T3 line."""
    return [text for kind, delivery, text in row["replies"]
            if kind not in ("t3", "t3_update") and delivery in (None, "sent") and text]


def naming_a_thread(row):
    """The heard lines outside a checked T3 line that name a thread, for reading: code cannot tell what they say
    about it, and a word check can only flag them."""
    return [text for text in free_lines(row) if "thread" in text.lower()]


def mixed_turn(row):
    """How a turn with both a lookup and a T3 line went: the T3 line said on its first relay, a reply said the
    lookup's forecast high (in the reply or the turn's late update), and the heard lines outside the checked T3 line
    that name a thread. None for any other turn."""
    if not row["highs"] or not row["t3"]:
        return None
    said = any(e["delivery"] == "sent" for e in row["t3"])
    heard = any(high in text for high in row["highs"] for text in free_lines(row))
    first = row["t3"][0]
    return {"path": row["path"], "first_relay_recited": first["delivery"] == "sent" and first["relays"] == 1,
            "t3_said": said, "lookup_heard": heard, "both_heard": heard and said,
            "free_lines_naming_a_thread": naming_a_thread(row)}


def which_call(call_rows, call):
    """How one which call went: the which question asked and said, the first status line after it (the thread the
    answer picked by place) and the status line for the third thread, asked by name, each said on its first relay.
    `call` is its index from 0."""
    third = WHICH[call][2]
    lines = [e for row in call_rows for e in row["t3"]]
    asked = next((i for i, e in enumerate(lines) if "ask_which" in e["kinds"]), None)
    after = lines[asked + 1:] if asked is not None else []
    by_place = next(([e] for e in after if "thread_status" in e["kinds"]
                     and third.lower() not in (e["draft"] or "").lower()), [])
    by_name = [e for e in lines if "thread_status" in e["kinds"] and third.lower() in (e["draft"] or "").lower()][:1]

    def status(said):
        return {"drafted": bool(said), "first_relay": bool(said) and said[0]["delivery"] == "sent"
                and said[0]["relays"] == 1, "said": bool(said) and said[0]["delivery"] == "sent",
                "replies": [e["reply"] for e in said]}

    return {"asked": asked is not None, "asked_said": asked is not None and lines[asked]["delivery"] == "sent",
            "by_place": status(by_place), "by_name": status(by_name)}


def noise_call(call_rows, call):
    """How one noise call went: the first thread's status line said, the turns cancelled as continued, and the
    two-part question joined into one turn whose status line was said. `call` is its index from 0."""
    first, ask, _, _, _, second, part_one, part_two = NOISY[call]
    lines = [e for row in call_rows for e in row["t3"]]

    def said(title):
        return any("thread_status" in e["kinds"] and e["delivery"] == "sent"
                   and title.lower() in (e["draft"] or "").lower() for e in lines)

    joined = [row for row in call_rows if overlap(part_one, row["said"] or "") >= 0.6
              and overlap(part_two, row["said"] or "") >= 0.6]
    alone = [row for row in call_rows if overlap(part_one, row["said"] or "") >= 0.6
             and overlap(part_one, row["said"] or "") > overlap(ask, row["said"] or "")
             and overlap(part_two, row["said"] or "") < 0.6 and any(r[1] in (None, "sent") for r in row["replies"])]
    return {"first_said": said(first), "continued": sum(row["cancelled"] == "continued" for row in call_rows),
            "joined": bool(joined), "second_said": said(second), "first_part_answered_alone": bool(alone)}


def follow_call(call_rows, call, calls=FOLLOW):
    """How one follow-up call went, per scripted turn after the chat line: the turn it became (the one with most of
    its words), whether it said a checked T3 line of the kind the turn wants about the call's thread, or for a turn
    about nothing in T3 whether T3 said anything in it, for a status whether its act said nothing is new and whether
    its reply carried the act's summary, and for a turn about the thread whether a free line was heard in it. `call`
    is its index from 0 in `calls`."""
    title, *_, scripted = calls[call]
    spoken = [row for row in call_rows if not row["idle"]]
    graded = []
    for n, (said, want) in enumerate(scripted):
        row = max(spoken, key=lambda r: overlap(said, r["said"] or ""), default=None)
        found = row is not None and overlap(said, row["said"] or "") >= 0.6
        entry = {"said": said, "want": want, "named": n == 0, "turn": row["path"] if found else None}
        if found and want == "none":
            entry["pulled_into_t3"] = bool(row["t3"])
        elif found:
            right = [e for e in row["t3"] if e["delivery"] == "sent" and ANSWERS[want] & set(e["kinds"])
                     and (want == "yes" or title.lower() in (e["draft"] or "").lower())]
            summarised = [e for e in right if e["summary"]]
            entry.update(checked_line=bool(right), kinds=[e["kinds"] for e in row["t3"]],
                         nothing_new=any(e["nothing_new"] for e in right), replies=[e["reply"] for e in right],
                         summary_heard=(any(overlap(e["summary"], e["reply"]) >= 0.5 for e in summarised)
                                        if summarised else None),
                         free=free_lines(row))
        graded.append(entry)
    return graded


MATCH = 0.75  # How much of the request a launch's payload must carry to be this call's request.


def grade(call_rows, call, launches):
    """How far one call got and where it first broke. `call_rows` are its turns in order, with their T3 entries, `call`
    its index from 0, and `launches` the seam's launches during it. A thread is started only by a launch that carries
    this call's request, and each line the voice said is judged against that record."""
    lines = [e for row in call_rows for e in row["t3"]]
    mine = [x for x in launches if x["call"] == call and x["overlap"] >= MATCH and x["project"] == "p1"]
    started, title = bool(mine), THREADS[call]
    said = [e for e in lines if e["delivery"] == "sent"]
    claims = []
    for e in said:
        kinds, text = set(e["kinds"]), (e["reply"] or "").lower()
        if "launched" in kinds:
            claims.append({"line": e["reply"], "true": started})
        elif {"thread_status", "confirm_thread"} & kinds:
            claims.append({"line": e["reply"], "true": started and title.lower() in text})
    asking = [e for e in lines if ASKING & set(e["kinds"])]
    asked = [e for e in asking if e["delivery"] == "sent"]
    answered = [e for e in lines if ANSWERED & set(e["kinds"])]
    status = any("thread_status" in e["kinds"] and title.lower() in (e["reply"] or "").lower() for e in said) and started
    stages = {"dispatched": bool(asking), "asked": bool(asked), "yes_read": bool(answered), "started": started,
              "launch_overlap": [x["overlap"] for x in launches if x["call"] == call],
              "status_answered": status, "false_claims": [c["line"] for c in claims if not c["true"]]}
    if started:
        cause = None
    elif not asking:
        cause = "request not dispatched as a launch"
    elif not asked:
        cause = ("draft refused" if all(e["draft_problems"] for e in asking) else "relay reply held")
    elif not answered:
        cause = "yes not read"
    elif launches:
        cause = "launch payload is not the request"
    else:
        cause = "launch not performed"
    return {**stages, "first_break": cause}


def summarize(out):
    """One row per turn, one grade per call. Calls split where the ledger's turn count starts over, and the seam's
    launches are matched to a call by the time they came."""
    rows, call = [], 0
    for path in sorted((out / "server").glob("turn_*.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        live = record.get("live") or {}
        number = (live.get("call") or {}).get("turn", 0)
        call += number == 1
        replies = [(r.get("kind"), r.get("delivery"), r.get("text")) for r in record.get("replies", [])]
        rows.append({"call": call, "turn": number, "idle": bool(record.get("idle")), "said": record.get("final_transcript"),
                     "replies": replies, "first_audio_after_speech_end_s": live.get("speech_end_to_first_audio_sent_s"),
                     "held": [r for r in replies if r[1] not in (None, "sent")], "t3": entries(record),
                     "highs": forecast_highs(record), "cancelled": live.get("cancelled"),
                     "repeats": [e["reply"] for e in record.get("timeline", [])
                                 if e.get("kind") == "fact_check" and e.get("repeated")],
                     "fact_checks": sum(e.get("kind") == "fact_check" for e in record.get("timeline", [])),
                     "error": record.get("error"), "path": path.name})
    launches = out / "seam-calls.json"
    started = json.loads(launches.read_text(encoding="utf-8")) if launches.exists() else []
    windows = json.loads((out / "windows.json").read_text(encoding="utf-8")) if (out / "windows.json").exists() else []
    grades = {}
    for order, window in enumerate(windows, 1):
        mine = [r for r in rows if r["call"] == order]
        during = [x for x in started if window["start"] <= x["at"] <= window["end"] + 5]
        grades[window["call"]] = grade(mine, window["call"] - 1, during)
    return rows, grades, started


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mode", choices=["drive", "serve", "summary"])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--calls", type=int, default=15)
    parser.add_argument("--only", type=int, nargs="+", help="call indexes from 0, for a smoke test")
    parser.add_argument("--no-tail", action="store_true", help="leave out the status question and its yes")
    parser.add_argument("--port", type=int, default=8771)
    parser.add_argument("--lfm-port", type=int, default=8095)
    parser.add_argument("--max-minutes", type=float, default=13.5)
    parser.add_argument("--cpu", action="store_true", help="smoke test on the CPU: no GPU layers")
    parser.add_argument("--mixed", action="store_true", help="the mixed calls: a lookup and a T3 request in one breath")
    parser.add_argument("--noise", action="store_true",
                        help="the noise calls: a noise burst after a question, and a question in two parts")
    parser.add_argument("--which", action="store_true",
                        help="the which calls: a which-thread question answered by place")
    parser.add_argument("--follow", action="store_true",
                        help="the follow-up calls: questions about a thread just discussed that do not name it")
    parser.add_argument("--more", action="store_true",
                        help="the more calls: a garbled thread question, why it stopped, and more detail")
    parser.add_argument("--summaries", action="store_true",
                        help="the local LFM3-1.7B writes thread summaries, as on the 2026-11-19 calls")
    parser.add_argument("--scenario", action="store_true",
                        help="serve only: the real T3 seam in place of the scripted one (see voice/live/scenarios.py)")
    parser.add_argument("--clips-from", type=Path,
                        help="send the clips an earlier drive rendered into this directory in place of rendering "
                             "new ones, so two runs hear the same audio")
    argv = sys.argv[1:]
    head, rest = (argv[:argv.index("--")], argv[argv.index("--") + 1:]) if "--" in argv else (argv, [])
    args = parser.parse_args(head)
    if args.mode == "summary":
        rows, grades, started = summarize(args.out)
        halves = {"visible": [g for c, g in grades.items() if c <= VISIBLE],
                  "held_out_a": [g for c, g in grades.items() if VISIBLE < c <= 2 * VISIBLE],
                  "held_out_b": [g for c, g in grades.items() if 2 * VISIBLE < c <= 3 * VISIBLE],
                  "held_out_c": [g for c, g in grades.items() if c > 3 * VISIBLE]}
        causes = {}
        for g in grades.values():
            causes[g["first_break"] or "started"] = causes.get(g["first_break"] or "started", 0) + 1
        score = {name: {"started": f"{sum(g['started'] for g in gs)} of {len(gs)}",
                        "status_answered": f"{sum(g['status_answered'] for g in gs)} of {len(gs)}",
                        "false_claims": sum(len(g["false_claims"]) for g in gs)} for name, gs in halves.items() if gs}
        if args.noise:
            graded = [noise_call([r for r in rows if r["call"] == order], window["call"] - 1)
                      for order, window in enumerate(json.loads((args.out / "windows.json").read_text("utf-8")), 1)]
            score["noise"] = {key: f"{sum(bool(g[key]) for g in graded)} of {len(graded)}"
                              for key in ("first_said", "joined", "second_said", "first_part_answered_alone")}
            score["noise"]["turns_cancelled_as_continued"] = sum(g["continued"] for g in graded)
            score["noise_calls"] = graded
            path = args.out / "server" / "overlaps.jsonl"
            score["held_sound"] = ([json.loads(line) for line in path.read_text("utf-8").splitlines()]
                                   if path.is_file() else [])
        if args.which:
            graded = [which_call([r for r in rows if r["call"] == order], window["call"] - 1)
                      for order, window in enumerate(json.loads((args.out / "windows.json").read_text("utf-8")), 1)]
            score["which"] = {"asked": f"{sum(g['asked_said'] for g in graded)} of {len(graded)}",
                              **{f"{kind}_{key}": f"{sum(g[kind][key] for g in graded)} of {len(graded)}"
                                 for kind in ("by_place", "by_name") for key in ("drafted", "first_relay", "said")}}
            score["which_calls"] = graded
        if args.follow or args.more:
            graded = [t for order, window in enumerate(json.loads((args.out / "windows.json").read_text("utf-8")), 1)
                      for t in follow_call([r for r in rows if r["call"] == order], window["call"] - 1,
                                           MORE if args.more else FOLLOW)]

            def count(chosen, key):
                return f"{sum(bool(t.get(key)) for t in chosen)} of {len(chosen)}"

            score["follow"] = {
                "named_status_answered": count([t for t in graded if t["named"]], "checked_line"),
                "follow_ups_answered_with_a_checked_line":
                    count([t for t in graded if t["want"].startswith("t3_") and not t["named"]], "checked_line"),
                "yes_answered": count([t for t in graded if t["want"] == "yes"], "checked_line"),
                "other_turns_pulled_into_t3": count([t for t in graded if t["want"] == "none"], "pulled_into_t3"),
                "statuses_said_again": count([t for t in graded if not t["named"] and t.get("checked_line")
                                              and ["thread_status"] in t.get("kinds", [])], "checked_line"),
                "statuses_said_again_told_nothing_is_new":
                    count([t for t in graded if not t["named"] and t.get("checked_line")
                           and ["thread_status"] in t.get("kinds", [])], "nothing_new"),
                "status_lines_with_a_summary_heard":
                    count([t for t in graded if t.get("summary_heard") is not None], "summary_heard"),
                "thread_turns_with_a_free_line_heard":
                    count([t for t in graded if t["want"].startswith("t3_") and t["turn"]], "free"),
                "turns_not_found": sum(t["turn"] is None for t in graded)}
            score["follow_turns"] = graded
        mixed = [m for m in map(mixed_turn, rows) if m is not None]
        if mixed:
            score["mixed_turns"] = {key: f"{sum(bool(m[key]) for m in mixed)} of {len(mixed)}"
                                    for key in ("first_relay_recited", "t3_said", "lookup_heard", "both_heard",
                                                "free_lines_naming_a_thread")}
        score["free_lines_naming_a_thread_in_any_turn"] = [line for row in rows for line in naming_a_thread(row)]
        score["replies_repeating_an_earlier_one"] = [line for row in rows for line in row["repeats"]]
        print(json.dumps({"score": score, "first_break_causes": causes, "mixed_turns": mixed, "grades": grades,
                          "rows": rows}, indent=1))
    else:
        serve(args, rest) if args.mode == "serve" else run_drive(args, rest)


if __name__ == "__main__":
    main()
