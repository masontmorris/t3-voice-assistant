"""The end-of-utterance decider call, made after the reply has played: logged, compared, and never acted on.

A `gated` turn hands the end of the utterance to LFM3, so a live call has no per-turn record of where the decider
would have disagreed. This module sends the question the `decider` mode sends on the final transcript, once the reply
audio has played, and writes one line per utterance to `shadow.jsonl` beside the turn records.

It cannot change the call. It is handed a request built from a copy of the turn's state, sends that one request, and
parses the answers into a record. It holds no orchestrator and no task service, so it has nothing to dispatch with,
and it writes nothing to the voice session. A failure or a timeout is one line with `error` set.

One line per utterance:
  utterance      the turn's name (`turn_003` in live mode, the clip's stem in a prototype run)
  turn_record    the turn record beside this file, which holds the transcript, LFM3's calls and the turn's own
                 metrics; `transcript` here only ties the line to it, so no call content is copied
  authority      "lfm" or "decider": which rule decided the end of this utterance
  transcript     the final transcript's word count and the first 12 hex digits of its SHA-256
  lfm            LFM3's task calls, and the action each asks for
  decider        the shadow's task decisions: the task each matches or `new:<sid>`, its action, its side effect, the
                 answers' confidences, and the task gate's verdict
  agreement      agree, disagree, decider_only, lfm_only, and one row per task naming both sides
  outcomes       every task the turn ended with and its status, which no row above takes credit for
  call           route, model, status, questions, milliseconds, and whether it timed out

An agreement row asks one thing of each side: does this task run? Both run or both leave it, `agree`; the decider
alone runs it, `decider_only`; LFM3 alone runs it, `lfm_only`; one runs it while the other cancels or amends it,
`disagree`. A decider piece that is not a task at all (`drop`) has no LFM3 counterpart, so it is a row only when it
matches a task the turn already made. Segments are tied to tasks by their words, since the shadow applies nothing and
so has no task ids of its own.
"""

import hashlib
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from voice.segmentation.contract import CorrectionMode, Kind, parse_decisions
from voice.segmentation.gate import PLAN_A

FILE = "shadow.jsonl"
# The wait for playback is the reply's own length, so this bounds a broken clock rather than a long answer.
TIMEOUT_S = 20.0
POLL_S = 0.05
RUNS = ("run",)
TASK_CALLS = {"run_task": "run", "delegate": "run", "cancel_task": "cancel", "amend_task": "amend"}


def request(utterance, turn_record, authority, body, lfm=(), outcomes=(), intent=None):
    """Everything one shadow call needs, as the turn hands it over.

    `body` is the decisions body `decider` mode sends on the final transcript, built by the turn's own segmenter
    (`HybridSegmenter.final_request`), so the question matches the one the shipped path asks. `lfm` and `outcomes`
    are what the turn did at the end of the utterance, for the comparison.
    """
    return {"utterance": utterance, "turn_record": turn_record, "authority": authority, "intent": intent,
            "body": body, "lfm": list(lfm), "outcomes": list(outcomes)}


def lfm_calls(tool_turn):
    """LFM3's task calls from the turn's tool session, with the action each one asks for.

    The live server opens no tool session on its decider path, so this is empty there and names the turn's rule,
    which `authority` records."""
    calls = []
    for call in (tool_turn or {}).get("function_calls") or ():
        action = TASK_CALLS.get(call.get("name"))
        if action is None:
            continue
        arguments = call.get("arguments")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments or "{}")
            except ValueError:
                arguments = {}
        arguments = arguments if isinstance(arguments, dict) else {}
        key = arguments.get("task_id") or arguments.get("task")
        calls.append({"name": call.get("name"), "action": action,
                      "key": key or f"new:lfm{len(calls) + 1}", "task": key,
                      "instruction": arguments.get("instruction") or arguments.get("new_request")})
    return calls


def text_key(text):
    return " ".join(re.findall(r"\w+", (text or "").lower()))


def task_decisions(answers, body, gate=PLAN_A):
    """The parsed answers as task decisions, each tied to the task it names or to `new:<sid>` when it would create one.

    A decision's action is `run` when the segment is a request and the task gate passes it on the final transcript,
    `hold` when the gate falls short, `drop` when the segment is not a request, and `cancel` or `amend` for a
    correction."""
    texts = body.get("state", {}).get("segments") or {}
    groups = {}
    for task in body.get("state", {}).get("tasks") or ():
        groups.setdefault(text_key(task.get("instruction")), []).append(task.get("task"))
    taken, decided = set(), []
    for sid, decision in answers.segments.items():
        verdict = gate(decision, is_final=True)
        target = None
        if decision.kind is Kind.CORRECTION and decision.corrects is not None:
            action = "cancel" if decision.corrects.mode is CorrectionMode.CANCEL else "amend"
            target = _match(groups, texts.get(decision.corrects.target, ""), taken)
        elif decision.kind is not Kind.REQUEST:
            action = "drop"
        else:
            action = "run" if verdict.passed else "hold"
        if target is None:
            target = _match(groups, texts.get(sid, ""), taken)
        key = target or f"new:{sid}"
        taken.add(key)
        decided.append({"key": key, "side": sid, "text": texts.get(sid, ""), "action": action,
                        "kind": decision.kind.value, "ready": decision.readiness.value,
                        "side_effect": decision.side_effect,
                        "confidence": {name: round(value, 4) for name, value in decision.confidence.items()
                                       if isinstance(value, (int, float))},
                        "gate": {"passed": verdict.passed, "score": round(verdict.score, 4),
                                 "weakest": verdict.weakest, "threshold": verdict.threshold},
                        "tool": None if decision.tool is None else {"tool": decision.tool.tool,
                                                                    "confidence": decision.tool.confidence}})
    return decided


def _match(groups, text, taken):
    for task in groups.get(text_key(text), ()):
        if task not in taken:
            return task
    return None


def _runs(action):
    return action in RUNS


def _stops(action):
    return action in ("cancel", "amend")


def agreement(lfm, decided, texts=None):
    """Per task: both run it or both leave it, `agree`; one runs it alone, that side's `…_only`; one runs it while the
    other cancels or amends it, `disagree`. A `drop` from the decider with no task behind it is not a row."""
    texts = texts or {}
    rows, counts = [], {"agree": 0, "disagree": 0, "decider_only": 0, "lfm_only": 0}
    keys = list(dict.fromkeys([d["key"] for d in decided] + [call["key"] for call in lfm]))
    for key in keys:
        mine = next((d for d in reversed(decided) if d["key"] == key), None)
        theirs = next((c for c in reversed(lfm) if c["key"] == key), None)
        if mine is not None and mine["action"] == "drop" and key.startswith("new:"):
            continue
        mine_runs = mine is not None and _runs(mine["action"])
        theirs_runs = theirs is not None and _runs(theirs["action"])
        if mine_runs and theirs is not None and _stops(theirs["action"]):
            verdict = "disagree"
        elif theirs_runs and mine is not None and _stops(mine["action"]):
            verdict = "disagree"
        elif mine_runs and not theirs_runs:
            verdict = "decider_only"
        elif theirs_runs and not mine_runs:
            verdict = "lfm_only"
        else:
            verdict = "agree"
        counts[verdict] += 1
        rows.append({"task": key, "text": texts.get(key) or (mine or theirs).get("text")
                     or (theirs or {}).get("instruction"),
                     "decider": None if mine is None else mine["action"],
                     "lfm": None if theirs is None else theirs["action"], "verdict": verdict})
    return {**counts, "rows": rows}


def run(decider, asked, gate=PLAN_A, lock=None):
    """One shadow call for one utterance: the request, its parsed task decisions and the comparison.

    `lock` is the decider's one-call-at-a-time lock the turns already share, so the shadow never puts a second
    request in flight. Never raises: a decider that is down, busy, slow or answers nonsense gives a line with `error`
    set, and the call it was taken from has already finished."""
    body = asked["body"]
    started = time.perf_counter()
    entry = {"utterance": asked["utterance"], "turn_record": asked["turn_record"],
             "authority": asked["authority"], "intent": asked.get("intent"),
             "transcript": _reference(body.get("state", {}).get("transcript", "")),
             "lfm": {"calls": asked.get("lfm", [])}, "outcomes": asked.get("outcomes", []),
             "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    try:
        if lock is not None and not lock.acquire(timeout=TIMEOUT_S):
            raise TimeoutError(f"the decider was busy for {TIMEOUT_S} s")
        try:
            record = _call(decider, body, f"shadow:{asked['utterance']}")
        finally:
            if lock is not None:
                lock.release()
        answers = parse_decisions(record.get("response"), body)
        decided = task_decisions(answers, body, gate)
        entry["decider"] = {"tasks": decided, "problems": answers.problems}
        entry["agreement"] = agreement(asked.get("lfm", []), decided, _texts(body))
        entry["call"] = _call_record(record, body, started)
    except Exception as exc:
        entry["error"] = _detail(exc)
        entry["decider"] = {"tasks": [], "problems": [entry["error"]]}
        entry["agreement"] = agreement(asked.get("lfm", []), [], _texts(body))
        entry["call"] = {"route": "decisions", "status": None, "questions": len(body.get("questions") or {}),
                         "ms": _ms(started), "ok": False, "timed_out": isinstance(exc, TimeoutError)}
    return entry


def _call(decider, body, label):
    """One decider call, sent again even when an identical request is cached: a cached answer would report no latency
    and could not disagree. The local d1 client keeps no cache and takes no such argument."""
    try:
        return decider.call("decisions", body, label=label, fresh=True)
    except TypeError:
        return decider.call("decisions", body, label=label)


def _call_record(record, body, started):
    return {"route": "decisions", "model": record.get("model") or body.get("model"),
            "status": record.get("status"), "questions": len(body.get("questions") or {}),
            "ms": _ms(started), "ok": True, "timed_out": False}


def _ms(started):
    return round((time.perf_counter() - started) * 1000, 1)


def _reference(transcript):
    return {"words": len(re.findall(r"\w+", transcript or "")),
            "sha256": hashlib.sha256((transcript or "").encode()).hexdigest()[:12]}


def _texts(body):
    return {task.get("task"): task.get("instruction") for task in body.get("state", {}).get("tasks") or ()
            if task.get("instruction")}


def timed_out(asked, error=None, ms=None):
    """The line for a call the caller stopped waiting on."""
    detail = _detail(error) if error is not None else f"timed out after {TIMEOUT_S} s"
    entry = {"utterance": asked["utterance"], "turn_record": asked["turn_record"],
             "authority": asked["authority"], "intent": asked.get("intent"),
             "transcript": _reference(asked["body"].get("state", {}).get("transcript", "")),
             "lfm": {"calls": asked.get("lfm", [])}, "outcomes": asked.get("outcomes", []),
             "decider": {"tasks": [], "problems": [detail]},
             "agreement": agreement(asked.get("lfm", []), [], _texts(asked["body"])),
             "call": {"route": "decisions", "status": None, "questions": len(asked["body"].get("questions") or {}),
                     "ms": round(TIMEOUT_S * 1000, 1) if ms is None else ms, "ok": False, "timed_out": True},
             "error": detail,
             "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    return entry


def _detail(error):
    text = str(error)
    return f"{type(error).__name__}: {text}" if text else f"timed out after {TIMEOUT_S} s"


class Log:
    """One `shadow.jsonl` in a call directory: one line per utterance, appended as each call returns."""

    def __init__(self, directory):
        self.path = Path(directory) / FILE

    def write(self, entry):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")
        return entry

    def lines(self):
        if not self.path.is_file():
            return []
        return [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines() if line.strip()]
