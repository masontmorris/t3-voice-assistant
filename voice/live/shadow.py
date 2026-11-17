"""The end-of-utterance decider call, made after the reply has played: logged, compared, and never acted on.

A `gated` turn hands the end of the utterance to one side: LFM3's tool turn in the prototype, and the decider's own
final call in live mode, which passes `final_decider`. Either way a live call has no per-turn record of where the
decider would have disagreed, so this module sends the question the `decider` mode sends on the final transcript,
once the reply audio has played, and writes one line per utterance to `shadow.jsonl` beside the turn records.

It cannot change the call. It is handed a request built from a copy of the turn's state, sends that one request, and
parses the answers into a record. It holds no orchestrator and no task service, so it has nothing to dispatch with,
and it writes nothing to the voice session. A failure, a timeout or a skip is one line with `error` set.

It cannot delay a turn either. It asks on its own client (`Lane`): its own connection and pacer, one attempt, and a
short ceiling, and it takes no lock a turn takes. The caller starts it only when no turn is running, and a turn that
starts ends the shadow's request instead of waiting for it.

One line per utterance:
  utterance      the turn's name (`turn_003` in live mode, the clip's stem in a prototype run)
  turn_record    the turn record beside this file, which holds the transcript, LFM3's calls and the turn's own
                 metrics; `transcript` here only ties the line to it, so no call content is copied
  authority      "lfm" or "decider": which rule decided the end of this utterance
  transcript     the final transcript's word count and the first 12 hex digits of its SHA-256
  lfm            LFM3's task calls, the action each asks for, and the task each one reached
  decider        the shadow's task decisions: the task each matches or `new:<sid>`, its action, its side effect, the
                 answers' confidences, and the task gate's verdict
  agreement      the per-task verdict counts, and one row per task naming both sides
  outcomes       every task the turn ended with and its status, which no row above takes credit for
  call           route, decider, model, status, questions, milliseconds, and whether it timed out
  skipped        set when no call was made, with the reason; `abandoned` when a starting turn ended the call

An agreement row asks one thing of each side: does this task run? Both run or both leave it, `agree`; the decider
alone runs it, `decider_only`; LFM3 alone runs it, `lfm_only`; one runs it while the other cancels or amends it,
`disagree`; one cancels or amends it while the other does nothing, `decider_correction` or `lfm_correction`. A decider
piece that is not a task at all (`drop`) has no LFM3 counterpart, so it is a row only when it matches a task the turn
already made.

A row is keyed by a task id. LFM3's calls carry one: `run_task`, `cancel_task` and `amend_task` name it, and a
`delegate` reaches the task its call returned, the one it made or the one the orchestrator linked it to. A decider
piece takes the id of the task the turn already had with the same words. A request piece with no task yet joins a task
LFM3's calls reached, or else one the turn ended with, when the two pass the orchestrator's own duplicate rule (content
word overlap), so one request is one row whichever side worded it.
"""

import hashlib
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from voice.segmentation.contract import CorrectionMode, Kind, parse_decisions
from voice.segmentation.gate import PLAN_A
from voice.segmentation.tasks import MATCH_THRESHOLD, content_words, overlap

FILE = "shadow.jsonl"
# The shadow call's ceiling on each socket operation, with no retry. Measured shadow calls took 0.1 to 2.9 s on the
# local d1 (docs/voice/eou-handoff.md), and a turn never waits for this one, so it bounds a hung backend.
TIMEOUT_S = 8.0
POLL_S = 0.05
TASK_CALLS = {"run_task": "run", "delegate": "run", "cancel_task": "cancel", "amend_task": "amend"}
VERDICTS = ("agree", "disagree", "decider_only", "lfm_only", "decider_correction", "lfm_correction")


class Lane:
    """The shadow's own decider client, one call at a time, which a starting turn ends.

    A client that offers `lane` gives a second connection to the same backend with its own pacer, one attempt and
    `TIMEOUT_S`, so the shadow's request never sits in front of a turn's. A new connection per call, because a server
    drops an idle keep-alive socket and a single attempt has no retry to hide that (measured: 3 of 4 calls failed with
    WinError 10053 on a reused socket to the local d1). A client without `lane` (a test fake) is used as it is."""

    def __init__(self):
        self.client, self.decider, self.ended = None, None, None

    @property
    def busy(self):
        return self.client is not None

    def open(self, decider):
        make = getattr(decider, "lane", None)
        self.decider, self.ended = decider, None
        self.client = make(TIMEOUT_S) if make is not None else decider
        return self.client

    def abort(self, reason):
        """Ends the call in flight, if any. Its line says why."""
        if self.client is None:
            return
        self.ended = reason
        stop = getattr(self.client, "abort", None)
        if stop is not None:
            stop()

    def finish(self):
        client, self.client = self.client, None
        if client is not None and client is not self.decider:
            client.close()


def request(utterance, turn_record, authority, body, lfm=(), outcomes=(), intent=None):
    """Everything one shadow call needs, as the turn hands it over.

    `body` is the decisions body `decider` mode sends on the final transcript, built by the turn's own segmenter
    (`HybridSegmenter.final_request`), so the question matches the one the shipped path asks. `lfm` and `outcomes`
    are what the turn did at the end of the utterance, for the comparison.
    """
    return {"utterance": utterance, "turn_record": turn_record, "authority": authority, "intent": intent,
            "body": body, "lfm": list(lfm), "outcomes": list(outcomes)}


def lfm_calls(tool_results):
    """LFM3's task calls, from the turn's `tool_results` (each call with its parsed arguments and the task service's
    result), with the action each asks for and the task it reached.

    The live server opens no tool session on its decider path, so this is empty there, and `authority` records which
    rule decided the end of the utterance instead."""
    calls = []
    for call in tool_results or ():
        action = TASK_CALLS.get(call.get("name"))
        if action is None:
            continue
        arguments = call.get("arguments") if isinstance(call.get("arguments"), dict) else {}
        result = call.get("result") if isinstance(call.get("result"), dict) else {}
        task = arguments.get("task_id")
        if call["name"] == "delegate" and result.get("ok"):
            task = result.get("task_id")
        calls.append({"name": call["name"], "action": action, "key": task or f"new:lfm{len(calls) + 1}",
                      "task": task, "instruction": arguments.get("instruction") or arguments.get("new_request")})
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


def _side(action):
    """What a side does with a task: runs it, stops it (cancel or amend), or neither."""
    return "run" if action == "run" else "stop" if action in ("cancel", "amend") else None


# (decider, lfm) -> verdict, for what each side does with one task.
VERDICT = {("run", "run"): "agree", ("stop", "stop"): "agree", (None, None): "agree",
           ("run", "stop"): "disagree", ("stop", "run"): "disagree",
           ("run", None): "decider_only", (None, "run"): "lfm_only",
           ("stop", None): "decider_correction", (None, "stop"): "lfm_correction"}


def _joined(decided, lfm, outcomes):
    """The decider's request pieces with no task yet, each moved onto the task the same request became, when one passes
    the orchestrator's duplicate rule. LFM3's tasks come first, so a delegate and the piece it repeats share a row."""
    named = {d["key"] for d in decided}
    words = {item.get("task"): item.get("instruction") for item in outcomes or () if item.get("task")}
    candidates = [c["key"] for c in lfm if not c["key"].startswith("new:") and c["key"] not in named]
    candidates += [task for task in words if task not in named and task not in candidates]
    texts = {c["key"]: c.get("instruction") for c in lfm}
    texts.update({task: text for task, text in words.items() if text})
    joined = []
    for decision in decided:
        if decision["key"].startswith("new:") and decision["action"] in ("run", "hold"):
            mine = content_words(decision.get("text"))
            shares = [(overlap(mine, content_words(texts.get(task))), task) for task in candidates]
            share, task = max(shares, key=lambda item: item[0], default=(0.0, None))
            if task is not None and share >= MATCH_THRESHOLD:
                candidates.remove(task)
                decision = {**decision, "key": task}
        joined.append(decision)
    return joined


def agreement(lfm, decided, texts=None, outcomes=None):
    """Per task, what each side does with it (`VERDICT`). A `drop` from the decider with no task behind it is not a
    row. `texts` names the tasks the request listed, and `outcomes` is what the turn ended up doing with each task (its
    status), so every row shows what the turn did."""
    texts = texts or {}
    status = {item.get("task"): item.get("status") for item in outcomes or () if item.get("task")}
    words = {item.get("task"): item.get("instruction") for item in outcomes or () if item.get("task")}
    decided = _joined(decided, lfm, outcomes)
    rows, counts = [], dict.fromkeys(VERDICTS, 0)
    for key in dict.fromkeys([d["key"] for d in decided] + [call["key"] for call in lfm]):
        mine = next((d for d in reversed(decided) if d["key"] == key), None)
        theirs = next((c for c in reversed(lfm) if c["key"] == key), None)
        if mine is not None and mine["action"] == "drop" and key.startswith("new:"):
            continue
        verdict = VERDICT[(_side(mine and mine["action"]), _side(theirs and theirs["action"]))]
        counts[verdict] += 1
        rows.append({"task": key,
                     "text": texts.get(key) or words.get(key) or (mine or {}).get("text")
                     or (theirs or {}).get("instruction"),
                     "decider": None if mine is None else mine["action"],
                     "lfm": None if theirs is None else theirs["action"],
                     "turn": status.get(key), "verdict": verdict})
    return {**counts, "rows": rows}


def run(decider, asked, gate=PLAN_A, name=None):
    """One shadow call for one utterance: the request, its parsed task decisions and the comparison.

    `decider` is the shadow's own client (`Lane.open`), never the turns' one. `name` is the configured decider (`d1`,
    `cloud` or `liquid`), which the record needs because the body's model field names the cloud client whatever
    answers. Never raises: a decider that is down, slow, ended by a turn or answers nonsense gives a line with `error`
    set, and the call it was taken from has already finished."""
    body = asked["body"]
    started = time.perf_counter()
    entry = _line(asked)
    try:
        record = decider.call("decisions", body, label=f"shadow:{asked['utterance']}", fresh=True)
        if record.get("status") != 200:
            raise ConnectionError(f"decisions http {record.get('status')}")
        answers = parse_decisions(record.get("response"), body)
        decided = task_decisions(answers, body, gate)
        entry["decider"] = {"tasks": decided, "problems": answers.problems}
        entry["agreement"] = agreement(asked.get("lfm", []), decided, _texts(body), asked.get("outcomes"))
        entry["call"] = _call_record(record, body, started, name)
    except Exception as exc:
        entry.update(_failed(asked, _detail(exc), name, _ms(started), timed_out=isinstance(exc, TimeoutError)))
    return entry


def _line(asked):
    return {"utterance": asked["utterance"], "turn_record": asked["turn_record"],
            "authority": asked["authority"], "intent": asked.get("intent"),
            "transcript": _reference(asked["body"].get("state", {}).get("transcript", "")),
            "lfm": {"calls": asked.get("lfm", [])}, "outcomes": asked.get("outcomes", []),
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}


def _failed(asked, detail, name, ms, timed_out=False):
    return {"error": detail, "decider": {"tasks": [], "problems": [detail]},
            "agreement": agreement(asked.get("lfm", []), [], _texts(asked["body"]), asked.get("outcomes")),
            "call": {"route": "decisions", "decider": name, "status": None,
                     "questions": len(asked["body"].get("questions") or {}), "ms": ms, "ok": False,
                     "timed_out": timed_out}}


def _call_record(record, body, started, name=None):
    return {"route": "decisions", "decider": name,
            "model": record.get("model") or (body.get("model") if name in (None, "cloud") else None),
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


def timed_out(asked, error=None, ms=None, name=None):
    """The line for a call the caller stopped waiting on."""
    detail = _detail(error) if error is not None else f"timed out after {TIMEOUT_S} s"
    return {**_line(asked), **_failed(asked, detail, name, round(TIMEOUT_S * 1000, 1) if ms is None else ms,
                                      timed_out=True)}


def skipped(asked, reason, name=None):
    """The line for an utterance the shadow did not ask about, and why: a turn was running, the decider was busy, or
    the call ended before the reply finished playing."""
    return {**_line(asked), **_failed(asked, f"no shadow call: {reason}", name, 0.0), "skipped": reason}


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
