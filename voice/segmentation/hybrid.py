"""The hybrid segmenter (plan A): variant E's break-finding with the baseline's per-segment classification.

Breaks come in two kinds. Hard cuts are the baseline splitter's cuts (sentence punctuation, "and", "but", "actually",
"no wait", ...) plus "I mean", and the decider cannot merge across them, so a correction always gets its own segment.
Soft cuts sit at variant E's other markers (commas, "then", "also", word gaps in a long stretch) and exist only where
E's `finished` and `at` answers put them. The words after the cursor split into pieces at hard cuts and at the soft
cuts chosen so far.

Each decision point is one decisions call. It asks the baseline's questions for every segment, committed or not, and
E's two break questions for every piece whose end is in doubt. A piece commits when it is the same piece at two
decision points, or at a pause, and both for a side effect. Until it commits its task is held. On the final transcript
nothing is applied until the break answers stop changing the pieces, so every segment is read before any dispatch.
The baseline's Orchestrator owns the tasks, corrections, delegates and dispatch policy.
"""

import re
from dataclasses import asdict, dataclass, replace

from voice.segmentation import client as api
from voice.segmentation.contract import (Decisions, Readiness, Segment, TranscriptUpdate, decision_request,
                                         parse_decisions)
from voice.segmentation.cursor import (ONE_ACTION, SNIPPET, THRESHOLDS, Verdict, _noul, _pick, _span, commits,
                                       judge, marked_text, markers, words)
from voice.segmentation.score import score
from voice.segmentation.spans import _is_filler, clause_cuts
from voice.segmentation.tasks import Orchestrator

I_MEAN = re.compile(r"\bi mean\b", re.IGNORECASE)
MAX_DRAIN = 4
QUIET = (0, (), ())


@dataclass(frozen=True)
class Piece:
    w0: int
    w1: int


@dataclass(frozen=True)
class BreakAnswer:
    finished: float
    at: object


def hard_cuts(text, ws):
    """Word gaps where the baseline splitter cuts, plus "I mean". Gap i sits before word i."""
    cuts = set()
    for offset in clause_cuts(text) | {m.start() for m in I_MEAN.finditer(text)}:
        i = next((k for k, w in enumerate(ws) if w.start >= offset), None)
        if i:
            cuts.add(i)
    return cuts


def piece_words(ws, p):
    return tuple(w.text for w in ws[p.w0:p.w1])


def span_text(text, ws, p):
    """(start, end, text) of the words in a piece, with the punctuation that closes it."""
    end = ws[p.w1 - 1].end
    end += len(re.match(r"[^\sA-Za-z0-9']*", text[end:]).group())
    return ws[p.w0].start, end, text[ws[p.w0].start:end]


class Proposal:
    """The words after the cursor at one decision point: hard cuts, soft cuts, and the pieces between them."""

    def __init__(self, text, ws, cursor, soft):
        self.text, self.ws, self.cursor, self.n = text, ws, cursor, len(ws)
        self.hard = {g for g in hard_cuts(text, ws) if cursor < g < self.n}
        self.soft = {g for g in soft if cursor < g < self.n and g not in self.hard}
        bounds = [cursor, *sorted(self.hard | self.soft), self.n] if self.n > cursor else []
        self.pieces = []
        for a, b in zip(bounds, bounds[1:]):
            if self.pieces and self.filler(self.pieces[-1]):
                a = self.pieces.pop().w0
            self.pieces.append(Piece(a, b))

    def filler(self, piece):
        return _is_filler(" ".join(piece_words(self.ws, piece)))

    def hard_end(self, w0):
        return min([g for g in self.hard if g > w0] + [self.n])

    def options(self, piece):
        """E's markers from the piece start up to its hard end, which is the last option."""
        end = self.hard_end(piece.w0)
        return sorted({g for g in markers(self.text, self.ws, piece.w0) if g < end} | {end})

    def in_doubt(self, piece, is_final):
        """The piece's end is a soft cut or the open end, or a soft marker inside it could split it."""
        certain = piece.w1 in self.hard or (is_final and piece.w1 == self.n)
        return not certain or len(self.options(piece)) > 1


def break_questions(proposal, sids, is_final):
    """E's `finished` and `at` for each piece in doubt, over one marked copy of the words after the cursor."""
    asked = {sid: p for sid, p in zip(sids, proposal.pieces)
             if not proposal.filler(p) and proposal.in_doubt(p, is_final)}
    gaps = sorted({g for p in asked.values() for g in proposal.options(p)} | {proposal.n})
    ws, questions = proposal.ws, {}
    for sid, p in asked.items():
        opening = _span(ws, p.w0, min(p.w0 + SNIPPET, proposal.n))
        criteria = {}
        for gap in proposal.options(p):
            k = gaps.index(gap) + 1
            where = "the end of the speech so far" if gap == proposal.n else f"[{k}]"
            criteria[f"m{k}"] = f"It ends at {where}. It is \"{_span(ws, p.w0, gap)}\"."
        criteria["none"] = "No request that starts there is finished yet."
        questions[f"{sid}_finished"] = {
            "type": "noul",
            "instructions": f"In new_speech, look at the words that start with \"{opening}\". Has the speaker "
                            "finished explicitly asking the assistant there for an actionable task, or for a change or "
                            f"withdrawal of an earlier request? {ONE_ACTION}",
            "criteria": {"true": "A request that starts there is complete: a worker could act on it as spoken.",
                         "false": "The words there are only filler, small talk, a description of a task, or a "
                                  "request still being spoken."}}
        questions[f"{sid}_at"] = {
            "type": "choice",
            "instructions": f"In new_speech, at which marker does the request that starts with \"{opening}\" end? "
                            + ONE_ACTION,
            "criteria": criteria}
    marked = marked_text(proposal.text, ws, proposal.cursor, gaps) if proposal.pieces else ""
    return asked, gaps, marked, questions


def parse_breaks(answers, asked, gaps, body, is_final, t):
    """Per asked piece: (verdict, gap, record). A malformed answer gives no verdict and a problem string."""
    out, problems = {}, []
    for sid in asked:
        local = []
        finished = _noul(answers, f"{sid}_finished", local)
        at = _pick(answers, f"{sid}_at", set(body["questions"][f"{sid}_at"]["criteria"]), local)
        problems += local
        if local:
            continue
        verdict, gap = judge(BreakAnswer(finished, at), gaps, is_final, t)
        out[sid] = (verdict, gap, {"finished": finished, "at": at.choice, "margin": at.margin,
                                   "runner_up": at.runner_up, "verdict": verdict.value, "gap": gap})
    return out, problems


def revise(proposal, sids, verdicts):
    """Move the soft cuts to where the answers put each request's end. Returns (soft cuts, candidate pieces)."""
    soft, candidates = set(proposal.soft), set()
    starts = {proposal.cursor} | proposal.hard | proposal.soft
    for sid, p in zip(sids, proposal.pieces):
        verdict = verdicts.get(sid)
        if verdict is None or verdict[0] != Verdict.CANDIDATE or p.w0 not in starts:
            continue
        gap = verdict[1]
        soft -= {g for g in soft if p.w0 < g < gap}
        starts -= {g for g in starts if p.w0 < g < gap}
        if gap < proposal.hard_end(p.w0):
            soft.add(gap)
        candidates.add(Piece(p.w0, gap))
    return soft, candidates


def confirmed(proposal, piece, verdict):
    """The piece's end holds at this point. A hard end holds unless the answer is a near tie. A soft or open end holds
    only when the answer chose it."""
    if piece.w1 in proposal.hard:
        return verdict is None or verdict[0] != Verdict.NEAR_TIE
    return verdict is not None and verdict[0] == Verdict.CANDIDATE and verdict[1] == piece.w1


def hold_near_tie(decision, is_final, margin):
    """On a partial, a dispatch_now that beat the runner-up by less than `margin` waits for the next point."""
    if is_final or decision.readiness != Readiness.DISPATCH_NOW or decision.confidence.get("ready", 1.0) >= margin:
        return decision
    return replace(decision, readiness=Readiness.WAIT)


def _jsonable(value):
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value.value if hasattr(value, "value") else value


class HybridSegmenter:
    """Plan A behind the prototype's Segmenter interface. Feed it transcript updates and voice delegate submissions."""

    def __init__(self, client, label, thresholds=THRESHOLDS):
        self.client, self.label, self.t = client, label, thresholds
        self.orch = Orchestrator()
        self.committed: list[Piece] = []
        self.soft: set[int] = set()
        self.soft_words: tuple = ()
        self.seen: dict[tuple, bool | None] = {}
        self.called_on = QUIET
        self.last_text = None
        self.last = None
        self.ws = []
        self.points: list[dict] = []

    @property
    def dispatch_log(self):
        return self.orch.dispatch_log

    @property
    def cursor(self):
        return self.committed[-1].w1 if self.committed else 0

    def records(self):
        return self.orch.task_records()

    def update(self, update):
        """One transcript revision. Returns its decision point, or None when no call was needed."""
        self.last, self.ws = update, words(update.text)
        pause = update.text.strip() == self.last_text
        self.last_text = update.text.strip()
        current = tuple(w.text for w in self.ws)
        self.soft = {g for g in self.soft if current[:g] == self.soft_words[:g]}
        if not self._needs_call(update, self._proposal(), pause):
            return None
        first = len(self.points)
        point = self._point(update, self._proposal(), pause, "revision")
        drains = 0
        while not point["applied"]:
            drains += 1
            point = self._point(update, self._proposal(), pause, "drain", last=drains == MAX_DRAIN)
        return {**point, "calls": [c for p in self.points[first:] for c in p["calls"]]}

    def submit_delegate(self, instruction):
        self.orch.submit_delegate(instruction)
        if self.last is None:
            return None
        return self._point(self.last, self._proposal(), False, "delegate")

    def _proposal(self):
        return Proposal(self.last.text, self.ws, self.cursor, self.soft)

    @staticmethod
    def _signature(proposal):
        gaps = sorted((set(markers(proposal.text, proposal.ws, proposal.cursor)) | proposal.hard) - {proposal.n})
        upto = gaps[-1] if gaps else proposal.cursor
        return proposal.cursor, tuple(gaps), tuple(w.text for w in proposal.ws[proposal.cursor:upto])

    def _needs_call(self, update, proposal, pause):
        """Call on the final transcript, for a delegate, when a marker appears or moves, or for a second look."""
        if update.is_final or self.orch.pending_delegates or self._signature(proposal) != self.called_on:
            return True
        for p in proposal.pieces:
            key = (p.w0, piece_words(self.ws, p))
            if key in self.seen and (pause or (p.w1 < proposal.n and not self.seen[key])):
                return True
        return False

    def _segment(self, sid, piece, open_):
        return Segment(sid, *span_text(self.last.text, self.ws, piece), open=open_)

    def _ask(self, update, committed, proposal, sids, live, kind):
        final = update.is_final
        asked_segs = committed + [self._segment(sid, p, not final and p.w1 == proposal.n) for sid, p in live.items()]
        tasks = [{"task": t.task_id, "instruction": t.instruction, "status": t.status.value,
                  "sources": sorted(t.sources)} for t in self.orch.tasks.values()]
        body = decision_request(update, asked_segs, tasks, dict(self.orch.pending_delegates))
        asked, gaps, marked, questions = break_questions(proposal, sids, final)
        body["state"]["new_speech"] = marked
        body["questions"].update(questions)
        label = f"{self.label}:r{update.revision}" + ("" if kind == "revision" else f":{kind}{len(self.points)}")
        record = self.client.call("decisions", body, label=label)
        call = {"label": label, "route": "decisions", "file": api.cache_path("decisions", body).name,
                "status": record["status"], "ms": round(record["timing"]["ms"], 1),
                "started_at": record["timing"]["started_at"]}
        if record["status"] != 200:
            return asked_segs, call, Decisions({}, {}, [f"decisions http {record['status']}"]), {}, []
        answers = (record["response"] or {}).get("answers") or {}
        verdicts, problems = parse_breaks(answers, asked, gaps, body, final, self.t)
        return asked_segs, call, parse_decisions(record["response"], body), verdicts, problems

    def _point(self, update, proposal, pause, kind, last=False):
        """One decisions call and its effect on the pieces, the commits, and the tasks."""
        final = update.is_final
        k = len(self.committed)
        committed = [self._segment(f"s{i + 1}", c, False) for i, c in enumerate(self.committed) if c.w0 < len(self.ws)]
        sids = [f"s{k + i + 1}" for i in range(len(proposal.pieces))]
        live = {sid: p for sid, p in zip(sids, proposal.pieces) if not proposal.filler(p)}
        asked_segs, call, decisions, verdicts, problems = self._ask(update, committed, proposal, sids, live, kind)

        soft, candidates = (proposal.soft, set()) if kind == "delegate" else revise(proposal, sids, verdicts)
        revised = Proposal(update.text, self.ws, proposal.cursor, soft)
        same = next((i for i, (a, b) in enumerate(zip(proposal.pieces, revised.pieces)) if a != b),
                    min(len(proposal.pieces), len(revised.pieces)))
        valid = {s.sid for s in committed} | set(sids[:same])

        if final:
            applied = revised.pieces == proposal.pieces or last
            newly = list(live) if applied else []
            tail = proposal.pieces
        else:
            applied, tail = True, revised.pieces
            newly = self._commits(proposal, sids[:same], decisions, verdicts, pause) if kind == "revision" else []
            if kind == "revision":
                self._remember(proposal, revised, sids, newly, decisions, verdicts, candidates)
        if kind != "delegate":
            self.soft, self.soft_words = soft, tuple(w.text for w in self.ws)
        self.committed += [live[sid] for sid in newly]

        apply_segs = committed + [self._segment(f"s{k + i + 1}", p, f"s{k + i + 1}" not in newly)
                                  for i, p in enumerate(tail) if not revised.filler(p)]
        kept = {sid: hold_near_tie(d, final, self.t.margin) for sid, d in decisions.segments.items() if sid in valid}
        delegates = {did: sid for did, sid in decisions.delegates.items() if sid is None or sid in valid}
        if applied:
            self.orch.apply(update, apply_segs, Decisions(kept, delegates, decisions.problems))
        self.called_on = self._signature(self._proposal())
        point = {"revision": update.revision, "is_final": final, "label": call["label"], "kind": kind,
                 "cursor": proposal.cursor, "pause": pause, "pieces": [[p.w0, p.w1] for p in proposal.pieces],
                 "revised": [[p.w0, p.w1] for p in revised.pieces], "hard": sorted(proposal.hard),
                 "soft": sorted(soft), "breaks": {sid: v[2] for sid, v in verdicts.items()},
                 "committed": newly, "applied": applied,
                 "segments": [asdict(s) for s in (apply_segs if applied else asked_segs)], "calls": [call],
                 "decisions": {sid: _jsonable(asdict(d)) for sid, d in kept.items()},
                 "delegates": delegates, "problems": decisions.problems + problems}
        self.points.append(point)
        return point

    def _commits(self, proposal, sids, decisions, verdicts, pause):
        """The unchanged pieces, in order from the cursor, that pass E's stability rule at this point."""
        newly = []
        for sid, p in zip(sids, proposal.pieces):
            decision = decisions.segments.get(sid)
            if proposal.filler(p) or decision is None or not confirmed(proposal, p, verdicts.get(sid)):
                break
            key = (p.w0, piece_words(self.ws, p))
            previous = key[1] if key in self.seen else None
            if not commits(key[1], previous, pause, decision.side_effect, False):
                break
            newly.append(sid)
        return newly

    def _remember(self, proposal, revised, sids, newly, decisions, verdicts, candidates):
        """Keep the uncommitted pieces this point confirmed or proposed, for the next point's stability check."""
        by_piece = dict(zip(proposal.pieces, sids))
        keep = {p for p, sid in by_piece.items() if confirmed(proposal, p, verdicts.get(sid))} | candidates
        keep = {p for p in keep if p in revised.pieces and by_piece.get(p) not in newly}
        side = {p: decisions.segments[sid].side_effect for p, sid in by_piece.items() if sid in decisions.segments}
        self.seen = {(p.w0, piece_words(self.ws, p)): side.get(p) for p in keep}


class HybridRunner:
    """Runs a scenario's revision stream through the hybrid, in the result shape of voice.segmentation.run."""

    def __init__(self, client, thresholds=THRESHOLDS):
        self.client, self.t = client, thresholds

    def run(self, scenario, mode="stream"):
        assert mode == "stream", "the hybrid runs on revision streams only"
        seg = HybridSegmenter(self.client, scenario["id"], self.t)
        revisions = 0
        for event in scenario["events"]:
            if event["type"] == "delegate":
                seg.submit_delegate(event["instruction"])
                continue
            revisions += 1
            seg.update(TranscriptUpdate(event["revision"], event["text"], event["is_final"]))
        snapshot = seg.orch.snapshot("conv_stage2", f"utt_{scenario['id']}")
        records = seg.records()
        return {"scenario": scenario["id"], "covers": scenario["covers"], "option": "h", "mode": mode,
                "revisions": revisions, "decision_points": seg.points, "tasks": records,
                "dispatch_log": seg.dispatch_log, "snapshot": snapshot, "expected": scenario["expected"],
                "score": score(scenario["expected"], records, seg.dispatch_log, snapshot)}
