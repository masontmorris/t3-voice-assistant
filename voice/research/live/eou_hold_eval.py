"""Offline replay of end-of-turn policies over whole utterances, with the live server's own endpointer and policy.

An utterance is PCM16 mono at 16 kHz. `simulate` feeds it to `Endpointer` frame by frame, asks a decider at each `Check`
as the server's `ask_end` does (one question in flight, answers dropped past their deadline or once the silence they
described has ended), and records when each turn ended. The true end of an utterance is its last voiced frame by the
endpointer's own rule, so it is the same speech the live server hears. A turn that ended while voiced frames were still
to come is a cut-off. `wait_s` is from the true end to the end of the turn that holds it.

Deciders are anything with `p_done(pcm, timeout_s) -> Answer`; `Cached` stores answers by (utterance, turn audio) so a
sweep of policies asks each question once. Nothing here names a person's words: utterances are identified by name only.
"""

import json
import statistics
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from voice.live.endpoint import Audio, Check, End, Endpointer, FRAME_BYTES, FRAME_MS, RATE, Settings, Start
from voice.live.eou_audio import Answer, Policy

SAMPLE_BYTES = 2
FRAME_SAMPLES = FRAME_BYTES // SAMPLE_BYTES


@dataclass(frozen=True)
class Utterance:
    name: str
    group: str
    pcm: bytes


def read_wav(path):
    with wave.open(str(path)) as wav:
        assert wav.getframerate() == RATE and wav.getnchannels() == 1 and wav.getsampwidth() == 2, path
        return wav.readframes(wav.getnframes())


def silence_like(pcm, seconds, noise):
    """`seconds` of silence to follow `pcm`: the clip's own last half second tiled when `noise`, else digital zero."""
    n = int(seconds * RATE) * SAMPLE_BYTES
    if not noise:
        return bytes(n)
    tail = pcm[-RATE // 2 * SAMPLE_BYTES:]
    return (tail * (n // len(tail) + 1))[:n]


def pad_tail(utterance, seconds, noise):
    """The utterance with enough trailing silence for any hold to play out."""
    return Utterance(utterance.name, utterance.group, utterance.pcm + silence_like(utterance.pcm, seconds, noise))


def compose(name, group, pieces, gaps_s):
    """Rendered pieces joined by digital silence: `gaps_s[i]` seconds between piece i and i+1. The true end is the end
    of the last piece by construction."""
    parts = [pieces[0]]
    for gap, piece in zip(gaps_s, pieces[1:]):
        parts += [bytes(int(gap * RATE) * SAMPLE_BYTES), piece]
    return Utterance(name, group, b"".join(parts))


_VOICING = {}


def voicing(pcm):
    """One bool per whole 20 ms frame, by the endpointer's own rule."""
    key = hash(pcm)
    if key not in _VOICING:
        ep = Endpointer(Settings())
        _VOICING[key] = [ep.classify(pcm[i:i + FRAME_BYTES])[1] for i in range(0, len(pcm) - FRAME_BYTES + 1, FRAME_BYTES)]
    return _VOICING[key]


@dataclass
class Cached:
    """A decider that keeps its answers, by name, keyed on utterance and the audio it was asked about."""
    decider: object
    label: str
    store: dict = field(default_factory=dict)

    def ask(self, utterance, end_sample, start_sample, pcm):
        """The answer, or None when this question was never asked and there is no decider to ask it."""
        key = f"{utterance.name}|{start_sample}|{end_sample}"
        if key not in self.store:
            if self.decider is None:
                return None
            started = time.process_time()
            answer = self.decider.p_done(pcm, None)
            self.store[key] = [answer.p_done, answer.ms, round((time.process_time() - started) * 1000, 1)]
        p, ms, _ = self.store[key]
        return Answer(p, ms)

    def save(self, path):
        Path(path).write_text(json.dumps(self.store), encoding="utf-8")

    @classmethod
    def load(cls, decider, label, path):
        cache = cls(decider, label)
        if Path(path).is_file():
            cache.store = json.loads(Path(path).read_text(encoding="utf-8"))
        return cache


@dataclass
class Run:
    name: str
    group: str
    true_end_s: float
    ends_s: list
    cut_offs: int
    wait_s: float | None
    checks: list


def simulate(utterance, cache, policy, silence_ms=700):
    """One utterance through the endpointer with `policy`'s checks, holds and early ends. `policy` None is the plain
    silence rule."""
    pcm, flags = utterance.pcm, voicing(utterance.pcm)
    settings = Settings(silence_ms=silence_ms, check_ms=policy.check_ms if policy else (),
                        hold_ms=policy.hold_ms if policy else 0)
    ep = Endpointer(settings)
    turn, turn_start, pending, ends, checks = bytearray(), 0, None, [], []
    for index, voiced in enumerate(flags):
        now_ms = index * FRAME_MS
        if pending is not None and pending["due_ms"] <= now_ms:
            asked, pending = pending, None
            fresh = bool(turn) and ep.silent_ms >= asked["silent_ms"]
            late = asked["ms"] / 1000 > asked["deadline_s"] + 0.05
            verdict = None if late or not fresh else policy.verdict(asked["p"])
            checks.append({"silent_ms": asked["silent_ms"], "p": None if late else round(asked["p"], 4),
                           "ms": asked["ms"], "verdict": verdict})
            if verdict == "end":
                ends.append(now_ms)
                ep.finish(asked["silent_ms"])
                turn = bytearray()
            elif verdict == "hold":
                ep.hold()
        frame = pcm[index * FRAME_BYTES:(index + 1) * FRAME_BYTES]
        for event in ep.push(frame):
            at_ms = (index + 1) * FRAME_MS
            if isinstance(event, Start):
                turn, turn_start = bytearray(event.pcm), (index + 1) * FRAME_SAMPLES - len(event.pcm) // SAMPLE_BYTES
            elif isinstance(event, Audio):
                turn += event.pcm
            elif isinstance(event, Check):
                if pending is None and policy is not None:
                    end_sample = (index + 1) * FRAME_SAMPLES
                    answer = cache.ask(utterance, end_sample, turn_start, bytes(turn[-int(policy.window_s * RATE) * 2:]))
                    if answer is None:
                        continue
                    deadline = policy.deadline_s(event.silent_ms, policy.fallback_ms(event.silent_ms, silence_ms))
                    pending = {"due_ms": at_ms + answer.ms, "silent_ms": event.silent_ms, "p": answer.p_done,
                               "ms": answer.ms, "deadline_s": deadline}
            elif isinstance(event, End):
                ends.append(at_ms)
                pending, turn = None, bytearray()
    last = max((i for i, v in enumerate(flags) if v), default=-1)
    true_end_ms = (last + 1) * FRAME_MS
    cuts = sum(any(flags[i] for i in range(end // FRAME_MS, len(flags))) for end in ends)
    after = [e for e in ends if e >= true_end_ms]
    wait = (min(after) - true_end_ms) / 1000 if after else None
    return Run(utterance.name, utterance.group, true_end_ms / 1000, [e / 1000 for e in ends], cuts, wait, checks)


def probe(utterance, cache, grid_ms, window_s):
    """The decider's answer at each `grid_ms` point of every pause, as the live server would be asked it: rows of
    (kind, gap_ms, silent_ms, p_done, ms), kind "end" for the true end and "pause" for any other. Fills `cache` for
    `simulate` as a side effect."""
    pcm, flags = utterance.pcm, voicing(utterance.pcm)
    last = max((i for i, v in enumerate(flags) if v), default=-1)
    ep = Endpointer(Settings(silence_ms=10**9, max_turn_s=10**6, check_ms=tuple(grid_ms)))
    turn, turn_start, rows = bytearray(), 0, []
    for index in range(len(flags)):
        for event in ep.push(pcm[index * FRAME_BYTES:(index + 1) * FRAME_BYTES]):
            if isinstance(event, Start):
                turn, turn_start = bytearray(event.pcm), (index + 1) * FRAME_SAMPLES - len(event.pcm) // SAMPLE_BYTES
            elif isinstance(event, Audio):
                turn += event.pcm
            elif isinstance(event, Check):
                begin = index + 1 - event.silent_ms // FRAME_MS
                run = begin
                while run < len(flags) and not flags[run]:
                    run += 1
                answer = cache.ask(utterance, (index + 1) * FRAME_SAMPLES, turn_start,
                                   bytes(turn[-int(window_s * RATE) * 2:]))
                rows.append(("end" if begin > last else "pause", (run - begin) * FRAME_MS if run < len(flags) else 10**9,
                             event.silent_ms, answer.p_done, answer.ms))
    return rows


def percentile(values, q):
    values = sorted(values)
    return values[min(len(values) - 1, int(round(q * (len(values) - 1))))] if values else None


def summarize(runs, late_s=1.0):
    waits = [r.wait_s for r in runs if r.wait_s is not None]
    return {
        "utterances": len(runs),
        "median_wait_s": round(statistics.median(waits), 3) if waits else None,
        "p90_wait_s": round(percentile(waits, 0.9), 3) if waits else None,
        "cut_offs": sum(r.cut_offs for r in runs),
        "utterances_cut": sum(r.cut_offs > 0 for r in runs),
        "held_too_long": sum(1 for w in waits if w > late_s),
        "no_end": sum(1 for r in runs if r.wait_s is None),
        "checks": sum(len(r.checks) for r in runs),
    }
