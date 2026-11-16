"""Variant E: one decisions call per revision over indexed clause markers, after a cursor.

The cursor is a word index into the utterance. Everything before it belongs to committed segments. Each revision asks
the decider about the words after the cursor only, with every candidate break numbered, plus the committed segments as
context for corrections and dependencies. The decider answers independent questions; `judge` applies the condition
between them and `commits` applies the stability rule. Committed segments feed the shared Orchestrator, so the task
state machine and dispatch policy are the baseline's.
"""

import re
from dataclasses import asdict, dataclass
from enum import StrEnum

from voice.segmentation import client as api
from voice.segmentation.client import MODEL
from voice.segmentation.contract import (Correction, CorrectionMode, Kind, Readiness, Segment, SegmentDecision,
                                         TranscriptUpdate)
from voice.segmentation.score import score
from voice.segmentation.tasks import MATCH_THRESHOLD, UNDISPATCHED, Orchestrator, Status, similarity

WORD = re.compile(r"[A-Za-z0-9']+")
CLAUSE_PUNCT = re.compile(r"[,.;:?!]")
CONNECTIVES = sorted([("and", "then"), ("and", "also"), ("oh", "and"), ("no", "wait"), ("i", "mean"),
                      ("never", "mind"), ("and",), ("then",), ("also",), ("actually",), ("but",), ("anyway",),
                      ("nevermind",)], key=len, reverse=True)
FALLBACK_SPAN = 12
SNIPPET = 4


@dataclass(frozen=True)
class Thresholds:
    finished: float = 0.5
    margin: float = 0.2
    side_effect: float = 0.5


THRESHOLDS = Thresholds()


@dataclass(frozen=True)
class Word:
    text: str
    start: int
    end: int


def words(text):
    return [Word(m.group().lower(), m.start(), m.end()) for m in WORD.finditer(text)]


def markers(text, ws, cursor):
    """Candidate break gaps after the cursor, as word indices (a gap sits before that word). The last is the end."""
    n = len(ws)
    gaps, i = set(), cursor
    while i < n:
        phrase = next((p for p in CONNECTIVES if tuple(w.text for w in ws[i:i + len(p)]) == p), None)
        if i > cursor and (phrase or CLAUSE_PUNCT.search(text[ws[i - 1].end:ws[i].start])):
            gaps.add(i)
        i += len(phrase) if phrase else 1
    bounds = sorted(gaps | {cursor, n})
    for a, b in zip(bounds, bounds[1:]):
        if b - a > FALLBACK_SPAN:
            gaps.update(range(a + 1, b))
    return sorted(gaps | {n}) if n > cursor else []


def marked_text(text, ws, cursor, gaps):
    """The words after the cursor with ` [k]` inserted at each gap, numbered from 1."""
    out, prev = [], ws[cursor].start
    for k, gap in enumerate(gaps, 1):
        end = ws[gap - 1].end
        tail = re.match(r"[^\sA-Za-z0-9']*", text[end:]).group()
        out.append(text[prev:end + len(tail)].strip() + f" [{k}]")
        prev = ws[gap].start if gap < len(ws) else len(text)
    return " ".join(out)


def _quote(ws, a, b):
    return " ".join(w.text for w in ws[a:b])


def _span(ws, a, b):
    if b - a <= 2 * SNIPPET + 1:
        return _quote(ws, a, b)
    return f"{_quote(ws, a, a + SNIPPET)} ... {_quote(ws, b - SNIPPET, b)}"


def marker_criteria(ws, cursor, gaps):
    """One option per marker, each naming the request it would close, plus `none`."""
    out = {}
    for k, gap in enumerate(gaps, 1):
        where = "the end of the speech so far" if gap == len(ws) else f"[{k}]"
        out[f"m{k}"] = f"The first request ends at {where}. It is \"{_span(ws, cursor, gap)}\"."
    out["none"] = "No request in new_speech is finished yet."
    return out


FIRST = "the first finished request in new_speech"
ONE_ACTION = ("A request is one action for one worker. \"And\", \"then\" or \"also\" followed by a second action "
              "starts a new request, and so does a correction such as \"no wait\" or \"actually\".")


EXTRA_QUESTIONS = {
    "implicit": {
        "type": "noul", "instructions": "Does new_speech imply a task the speaker wants done without asking for it "
                                        "outright?",
        "criteria": {"true": "A helpful assistant would act on it, though the speaker did not phrase it as a request.",
                     "false": "It implies no task beyond what the speaker asked for."}},
    "preemptive": {
        "type": "noul", "instructions": "Would a read-only lookup started now help answer what the speaker is likely "
                                        "to ask next?",
        "criteria": {"true": "A lookup now would likely save time.", "false": "No lookup would help yet."}},
}


def cursor_request(update, ws, cursor, gaps, earlier, extra=False):
    """One decisions body for the words after the cursor. `earlier` maps committed sid to its text."""
    questions = {
        "finished": {"type": "noul",
                     "instructions": "In new_speech, has the speaker finished explicitly asking the assistant for an "
                                     "actionable task, or for a change or withdrawal of an earlier request? "
                                     + ONE_ACTION,
                     "criteria": {"true": "At least one request in new_speech is complete: a worker could act on it "
                                          "as spoken.",
                                  "false": "new_speech holds only filler, small talk, a description of a task, or a "
                                           "request still being spoken."}},
        "at": {"type": "choice", "instructions": f"At which marker does {FIRST} end? {ONE_ACTION}",
               "criteria": marker_criteria(ws, cursor, gaps)},
        "side_effect": {"type": "noul",
                        "instructions": f"Would carrying out {FIRST} change something outside the conversation?",
                        "criteria": {"true": "It sends a message or email, contacts someone, buys, books, posts, or "
                                             "changes or deletes someone's data.",
                                     "false": "It only looks up, computes, or reports information back to the "
                                              "speaker."}},
    }
    if earlier:
        questions["depends"] = {
            "type": "choice", "instructions": f"Does {FIRST} need the result of an earlier request before it can start?",
            "criteria": {"none": "It can start without any earlier request's result.",
                         **{sid: f"It needs the result of {sid} (\"{t}\") first." for sid, t in earlier.items()}}}
        questions["corrects"] = {
            "type": "choice", "instructions": f"Does {FIRST} change or withdraw an earlier request?",
            "criteria": {"none": "It does not change any earlier request.",
                         **{f"replace_{sid}": f"It changes what {sid} (\"{t}\") asked for, so {sid} as first "
                                              "spoken should not be done."
                            for sid, t in earlier.items()},
                         **{f"cancel_{sid}": f"It withdraws {sid} (\"{t}\") with no replacement."
                            for sid, t in earlier.items()}}}
    if extra:
        questions.update(EXTRA_QUESTIONS)
    state = {"earlier_requests": earlier, "new_speech": marked_text(update.text, ws, cursor, gaps),
             "speech_is_final": update.is_final}
    return {"model": MODEL, "state": state, "questions": questions}


def delegate_request(update, ws, cursor, gaps, earlier, delegates):
    """Questions that match each pending delegate submission to a committed request or to new_speech."""
    marked = marked_text(update.text, ws, cursor, gaps) if gaps else ""
    questions = {f"{did}_same_as": {
        "type": "choice",
        "instructions": f"The voice assistant submitted the task \"{instruction}\". Which request asks for the same "
                        "thing?",
        "criteria": {"new": "No request in the speech asks for it.",
                     **({"new_speech": f"The request in new_speech (\"{_quote(ws, cursor, len(ws))}\") asks for it."}
                        if gaps else {}),
                     **{sid: f"{sid} (\"{t}\") asks for the same thing." for sid, t in earlier.items()}}}
        for did, instruction in delegates.items()}
    state = {"earlier_requests": earlier, "new_speech": marked, "speech_is_final": update.is_final}
    return {"model": MODEL, "state": state, "questions": questions}


@dataclass(frozen=True)
class Pick:
    choice: str
    margin: float
    runner_up: str | None


@dataclass(frozen=True)
class CursorAnswer:
    finished: float
    at: Pick
    side_effect: float
    depends: Pick | None
    corrects: Pick | None
    extra: dict


def _pick(answers, name, options, problems):
    a = answers.get(name)
    probs = a.get("probabilities") if isinstance(a, dict) else None
    if not isinstance(a, dict) or a.get("type") != "choice" or a.get("choice") not in options \
            or not isinstance(probs, dict) or not all(isinstance(p, (int, float)) for p in probs.values()):
        problems.append(f"{name}: malformed {a!r:.80}")
        return None
    ranked = sorted(probs.items(), key=lambda kv: -kv[1])
    margin = ranked[0][1] - (ranked[1][1] if len(ranked) > 1 else 0.0)
    return Pick(a["choice"], margin, ranked[1][0] if len(ranked) > 1 else None)


def _noul(answers, name, problems):
    a = answers.get(name)
    value = a.get("noul") if isinstance(a, dict) and a.get("type") == "noul" else None
    if not isinstance(value, (int, float)) or not 0 <= value <= 1:
        problems.append(f"{name}: malformed {a!r:.80}")
        return None
    return float(value)


def parse_cursor(response, body):
    """Parse untrusted answers against the request. Returns (CursorAnswer or None, problems). Never raises."""
    problems = []
    answers = response.get("answers") if isinstance(response, dict) else None
    if not isinstance(answers, dict):
        return None, ["no answers object"]
    q = body["questions"]
    finished = _noul(answers, "finished", problems)
    at = _pick(answers, "at", set(q["at"]["criteria"]), problems)
    side = _noul(answers, "side_effect", problems)
    depends = _pick(answers, "depends", set(q["depends"]["criteria"]), problems) if "depends" in q else None
    corrects = _pick(answers, "corrects", set(q["corrects"]["criteria"]), problems) if "corrects" in q else None
    extra = {name: _noul(answers, name, problems) for name in ("implicit", "preemptive") if name in q}
    if problems:
        return None, problems
    return CursorAnswer(finished, at, side, depends, corrects, extra), []


def parse_delegates(response, body):
    problems, out = [], {}
    answers = response.get("answers") if isinstance(response, dict) else None
    if not isinstance(answers, dict):
        return {}, ["no answers object"]
    for name, question in body["questions"].items():
        pick = _pick(answers, name, set(question["criteria"]), problems)
        if pick is not None:
            out[name.removesuffix("_same_as")] = pick.choice
    return out, problems


class Verdict(StrEnum):
    NOTHING = "nothing"
    CANDIDATE = "candidate"
    DISAGREE = "disagree"
    NEAR_TIE = "near_tie"


def judge(answer, gaps, is_final, t=THRESHOLDS):
    """Apply the condition the API cannot express. Returns (verdict, boundary gap or None).

    On a partial, a near-tie or a disagreement waits for the next revision. The final transcript has no next
    revision, so when the request is finished it takes the earlier marker among the top two choices.
    """
    finished, located = answer.finished >= t.finished, answer.at.choice != "none"
    if is_final and finished and (answer.at.margin < t.margin or not located):
        top_two = [c for c in (answer.at.choice, answer.at.runner_up) if c not in (None, "none")]
        return (Verdict.CANDIDATE, gaps[min(int(c[1:]) for c in top_two) - 1]) if top_two else (Verdict.DISAGREE, None)
    if answer.at.margin < t.margin:
        return Verdict.NEAR_TIE, None
    if finished and located:
        return Verdict.CANDIDATE, gaps[int(answer.at.choice[1:]) - 1]
    return (Verdict.DISAGREE if finished or located else Verdict.NOTHING), None


def commits(candidate, previous, pause, side_effect, is_final):
    """The stability rule. `candidate` and `previous` are the words from the cursor to the proposed break."""
    if is_final:
        return True
    held = previous is not None and candidate == previous
    return (held and pause) if side_effect else (held or pause)


def decision_for(answer, t=THRESHOLDS):
    """The SegmentDecision a committed segment hands to the shared Orchestrator."""
    depends = None
    if answer.depends is not None:
        top, alt = answer.depends.choice, answer.depends.runner_up
        if answer.depends.margin < t.margin and top == "none" and alt not in (None, "none"):
            top = alt
        depends = None if top == "none" else top
    correction = None
    if answer.corrects is not None and answer.corrects.choice != "none":
        mode, _, target = answer.corrects.choice.partition("_")
        correction = Correction(target, CorrectionMode(mode))
    return SegmentDecision(sid="", kind=Kind.CORRECTION if correction else Kind.REQUEST,
                           readiness=Readiness.DISPATCH_NOW, depends_on=depends, corrects=correction,
                           side_effect=answer.side_effect >= t.side_effect)


@dataclass
class Committed:
    sid: str
    w0: int
    w1: int


class CursorOrchestrator(Orchestrator):
    """The baseline Orchestrator fed one committed segment at a time. Committed segments never re-split."""

    def __init__(self):
        super().__init__()
        self.committed: list[Committed] = []
        self.bound: dict = {}
        self.decided: dict[str, SegmentDecision] = {}
        self.awaiting: list = []
        self.ws: list[Word] = []

    @property
    def cursor(self):
        return self.committed[-1].w1 if self.committed else 0

    def segment(self, c):
        ws = self.ws
        if c.w0 >= len(ws):
            return Segment(c.sid, len(self.text), len(self.text), "", open=False)
        end = ws[min(c.w1, len(ws)) - 1].end
        end += len(re.match(r"[^\sA-Za-z0-9']*", self.text[end:]).group())
        return Segment(c.sid, ws[c.w0].start, end, self.text[ws[c.w0].start:end], open=False)

    def earlier(self):
        return {c.sid: self.segment(c).text for c in self.committed}

    def refresh(self, update):
        """Take a new revision: re-derive committed segment text from the same word ranges."""
        self.revision, self.is_final, self.text = update.revision, update.is_final, update.text
        self.ws = words(update.text)
        for c in self.committed:
            task, seg = self.bound.get(c.sid), self.segment(c)
            if task is not None and task.segment_text != seg.text:
                self._log(task, "segment_text", old=task.segment_text, new=seg.text)
                task.segment_text, task.segment_start = seg.text, seg.start
                if task.status in UNDISPATCHED:
                    task.instruction = self._instruction_for(task, seg.text)

    def commit(self, w1, decision):
        c = Committed(f"s{len(self.committed) + 1}", self.cursor, w1)
        self.committed.append(c)
        seg = self.segment(c)
        decision = SegmentDecision(c.sid, decision.kind, decision.readiness, decision.depends_on, decision.corrects,
                                   decision.side_effect)
        self.decided[c.sid] = decision
        waiting = next((t for t in self.awaiting if similarity(t.instruction, seg.text) >= MATCH_THRESHOLD), None)
        if waiting is not None:
            self.awaiting.remove(waiting)
            waiting.segment_text, waiting.segment_start = seg.text, seg.start
            self.bound[c.sid] = waiting
        self._apply_segment(seg, decision, self.bound)
        task = self.bound.get(c.sid)
        if task is not None and task.status in UNDISPATCHED:
            self._settle(task, seg, decision)
        return c

    def apply_delegates(self, picks):
        by_sid = {c.sid: self.segment(c) for c in self.committed}
        for did, pick in picks.items():
            instruction = self.pending_delegates.pop(did)
            if pick == "new_speech":
                task = self._new_task(instruction, None, {"voice_delegate"})
                self._move(task, Status.DISPATCHED, "voice delegate submitted it")
                self.awaiting.append(task)
            else:
                self._apply_delegate(instruction, by_sid.get(pick), self.bound)

    def finish(self):
        """On the final transcript, settle segments whose dispatch was held for it."""
        for c in self.committed:
            task, decision = self.bound.get(c.sid), self.decided[c.sid]
            if task is not None and task.status in UNDISPATCHED:
                self._settle(task, self.segment(c), decision)


MAX_DRAIN = 8


class CursorRunner:
    def __init__(self, client, extra=False, thresholds=THRESHOLDS, repeat=None):
        self.client, self.extra, self.t, self.repeat = client, extra, thresholds, repeat

    def _call(self, body, label, calls):
        if self.repeat is not None:
            body = {**body, "user": f"repeat-{self.repeat}"}
        record = self.client.call("decisions", body, label=label)
        calls.append({"label": label, "route": "decisions", "file": api.cache_path("decisions", body).name,
                      "status": record["status"], "ms": round(record["timing"]["ms"], 1),
                      "started_at": record["timing"]["started_at"]})
        return body, record

    def _ask(self, orch, update, label, calls):
        gaps = markers(update.text, orch.ws, orch.cursor)
        if not gaps:
            return gaps, None, []
        body = cursor_request(update, orch.ws, orch.cursor, gaps, orch.earlier(), self.extra)
        sent, record = self._call(body, label, calls)
        if record["status"] != 200:
            return gaps, None, [f"decisions http {record['status']}"]
        answer, problems = parse_cursor(record["response"], sent)
        return gaps, answer, problems

    def _point(self, orch, update, label, previous, pause):
        """One decisions call and its effect. Returns (point record, candidate words to carry to the next revision)."""
        calls = []
        cursor = orch.cursor
        gaps, answer, problems = self._ask(orch, update, label, calls)
        verdict, gap = (Verdict.NOTHING, None) if answer is None else judge(answer, gaps, update.is_final, self.t)
        candidate = tuple(w.text for w in orch.ws[cursor:gap]) if gap is not None else None
        side = answer is not None and answer.side_effect >= self.t.side_effect
        committed = None
        if candidate is not None and commits(candidate, previous, pause, side, update.is_final):
            committed = orch.commit(gap, decision_for(answer, self.t)).sid
        point = {"revision": update.revision, "is_final": update.is_final, "label": label, "cursor": cursor,
                 "markers": gaps, "calls": calls, "pause": pause, "verdict": verdict.value, "boundary": gap,
                 "candidate": " ".join(candidate) if candidate else None, "committed": committed,
                 "answer": _jsonable(asdict(answer)) if answer else None, "problems": problems}
        return point, (None if committed else candidate)

    def _delegates(self, orch, update, label):
        calls = []
        gaps = markers(update.text, orch.ws, orch.cursor)
        body = delegate_request(update, orch.ws, orch.cursor, gaps, orch.earlier(), dict(orch.pending_delegates))
        sent, record = self._call(body, label, calls)
        picks, problems = ({}, [f"decisions http {record['status']}"]) if record["status"] != 200 \
            else parse_delegates(record["response"], sent)
        orch.apply_delegates({did: picks.get(did, "new") for did in list(orch.pending_delegates)})
        return {"revision": update.revision, "is_final": update.is_final, "label": label, "cursor": orch.cursor,
                "calls": calls, "delegates": picks, "problems": problems}

    def run(self, scenario, mode="stream"):
        assert mode == "stream", "variant E runs on revision streams only"
        orch, points = CursorOrchestrator(), []
        previous, last_words, last, revisions = None, None, None, 0
        for event in scenario["events"]:
            if event["type"] == "delegate":
                orch.submit_delegate(event["instruction"])
                if last is not None:
                    points.append(self._delegates(orch, last, f"{scenario['id']}:r{last.revision}:delegate"))
                continue
            revisions += 1
            last = TranscriptUpdate(event["revision"], event["text"], event["is_final"])
            orch.refresh(last)
            current = tuple(w.text for w in orch.ws)
            pause = current == last_words
            last_words = current
            if orch.pending_delegates:
                points.append(self._delegates(orch, last, f"{scenario['id']}:r{last.revision}:delegate"))
            point, previous = self._point(orch, last, f"{scenario['id']}:r{last.revision}", previous, pause)
            points.append(point)
            drain = 0
            while last.is_final and point["committed"] and orch.cursor < len(orch.ws) and drain < MAX_DRAIN:
                drain += 1
                point, previous = self._point(orch, last, f"{scenario['id']}:r{last.revision}:drain{drain}",
                                              None, True)
                points.append(point)
        orch.finish()
        snapshot = orch.snapshot("conv_stage2", f"utt_{scenario['id']}")
        records = orch.task_records()
        return {"scenario": scenario["id"], "covers": scenario["covers"], "option": "e", "mode": mode,
                "revisions": revisions, "decision_points": points, "tasks": records,
                "segments": [{"sid": c.sid, "w0": c.w0, "w1": c.w1, "text": orch.segment(c).text}
                             for c in orch.committed],
                "dispatch_log": orch.dispatch_log, "snapshot": snapshot, "expected": scenario["expected"],
                "score": score(scenario["expected"], records, orch.dispatch_log, snapshot)}


def _jsonable(value):
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value.value if hasattr(value, "value") else value
