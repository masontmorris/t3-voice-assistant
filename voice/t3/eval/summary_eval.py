"""The unit 11 eval: thread summaries for "how's that thread going?", and the slot-check pass rate of rendered acts.

    python -m voice.t3.eval.summary_eval freeze --db <read-only copy of statev2.sqlite> --out <dir>
    python -m voice.t3.eval.summary_eval run --dir <dir> [--renderer particle-1|local]
    python -m voice.t3.eval.summary_eval acts --dir <dir> [--renderer particle-1|local]

`freeze` builds thread states from a copy of a T3 database, the way the voice seam's read gives them: title,
activity, latest run, pending need, and the last reply cut at the seam's 1,200 characters (outcome.ts). A state can
be frozen as of a moment inside a run, so working, waiting and queued threads are real states too. It writes
`states.json`. The asks and the facts a good answer must hold go in `labels.json`, by hand, before any run.

`run` writes, for each state, the summary and the line the voice would say for its `thread_status` act, to
`summaries.json`, before any scoring. `acts` renders `acts.json`'s acts, built from the frozen states, and writes
each line with its slot-check and decider results to `rendered.json`. Both use the voice's own text model, chosen by
`--renderer` as the voice chooses it, and the hosted decider, and save every output. A local renderer's per-call
latency goes to `ledger-<command>.json`. The frozen data names real threads, so it stays under VOICE_HOME and
never enters the repo.
"""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
import time
from pathlib import Path

from voice.live.t3_tasks import status_act
from voice.t3 import types as T
from voice.t3.acts import Act, Kind, act, check
from voice.t3.pick.model import LiveClient
from voice.t3 import speech
from voice.t3.speech import RENDERERS, Lane, LocalText, Speech
from voice.t3.verify import Decider
from voice.liquid_decider.client import LiquidClient

RESULT_TEXT_LIMIT = 1_200
TERMINAL = {"completed": "finished", "failed": "failed", "interrupted": "interrupted", "cancelled": "cancelled",
            "rolled_back": "rolled_back"}
PLAN = {"finished_long": 7, "finished_short": 5, "failed": 5, "interrupted": 3, "working": 5, "waiting_on_you": 3,
        "queued": 2}
SEED = 11


def cut_at_sentence(text: str, limit: int = RESULT_TEXT_LIMIT) -> str:
    """outcome.ts `cutAtSentence`, so a frozen reply is what the seam would serve."""
    if len(text) <= limit:
        return text
    head = text[:limit]
    end = max(head.rfind(mark) for mark in (". ", "! ", "? ", ".\n", "!\n", "?\n", "\n\n"))
    if end > 0:
        return head[:end + 1].rstrip()
    space = head.rfind(" ")
    return (head[:space] if space > 0 else head).rstrip() + "\N{HORIZONTAL ELLIPSIS}"


class Records:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)

    def rows(self, sql, *args):
        return self.db.execute(sql, args).fetchall()

    def thread(self, thread_id):
        (title, project, payload), = self.rows("select title, project_id, payload_json from "
                                               "orchestration_v2_projection_threads where thread_id = ?", thread_id)
        return title, project

    def runs(self, thread_id):
        return [json.loads(p) for (p,) in self.rows("select payload_json from orchestration_v2_projection_runs where "
                                                    "thread_id = ? order by ordinal", thread_id)]

    def replies(self, thread_id):
        out = []
        for (payload,) in self.rows("select payload_json from orchestration_v2_projection_messages where thread_id = ? "
                                    "and role = 'assistant'", thread_id):
            message = json.loads(payload)
            if message.get("runId") and not message.get("streaming") and (message.get("text") or "").strip():
                out.append(message)
        return sorted(out, key=lambda m: m["updatedAt"])

    def errors(self, run_id):
        out = []
        for (payload,) in self.rows("select payload_json from orchestration_v2_projection_turn_items where run_id = ? "
                                    "and type = 'error'", run_id):
            failure = json.loads(payload).get("failure") or {}
            if failure.get("message"):
                out.append(failure["message"])
        return out

    def prompts(self):
        return [json.loads(p) for (p,) in self.rows("select payload_json from orchestration_v2_projection_turn_items "
                                                    "where type in ('approval_request', 'user_input_request')")]


def state_as_of(records: Records, thread_id: str, at: str | None, *, pending=None, queued=False) -> dict:
    """The seam's read of `thread_id` as it stood at `at` (ISO time), or now when `at` is None."""
    title, project = records.thread(thread_id)
    runs = [r for r in records.runs(thread_id) if at is None or r["requestedAt"] <= at]
    run = runs[-1]
    ended = run.get("completedAt") and (at is None or run["completedAt"] <= at)
    replies = [m for m in records.replies(thread_id) if at is None or m["updatedAt"] <= at]
    if ended:
        activity, status = TERMINAL.get(run["status"], run["status"]), run["status"]
        own = [m for m in replies if m["runId"] == run["id"]]
        text = own[-1]["text"] if own else (records.errors(run["id"]) or [""])[-1]
    else:
        status = "queued" if queued else "running"
        activity = "waiting_on_you" if pending else ("queued" if queued else "working")
        text = replies[-1]["text"] if replies else ""
    return {"thread_id": thread_id, "project_id": project, "title": title, "activity": activity,
            "run": {"run_id": run["id"], "status": status}, "as_of": at,
            "pending": pending, "last_reply": cut_at_sentence(text) if text else None,
            "reply_chars": len(text), "reply_cut": len(text) > RESULT_TEXT_LIMIT}


def freeze(args) -> int:
    records = Records(args.db)
    rng = random.Random(SEED)
    picked: dict[str, list[dict]] = {name: [] for name in PLAN}
    used: set[str] = set()

    def take(kind, thread_id, at=None, **more):
        if thread_id in used or len(picked[kind]) >= PLAN[kind]:
            return
        state = state_as_of(records, thread_id, at, **more)
        if state["last_reply"] or kind in ("queued", "waiting_on_you"):
            used.add(thread_id)
            picked[kind].append({"kind": kind, **state})

    latest = records.rows("select r.thread_id, r.status, r.payload_json from orchestration_v2_projection_runs r "
                          "join (select thread_id, max(ordinal) o from orchestration_v2_projection_runs group by "
                          "thread_id) l on l.thread_id = r.thread_id and l.o = r.ordinal "
                          "join orchestration_v2_projection_threads t on t.thread_id = r.thread_id "
                          "where t.deleted_at is null")
    rng.shuffle(latest)
    for thread_id, status, _ in latest:
        if status == "completed":
            replies = records.replies(thread_id)
            long = bool(replies) and len(replies[-1]["text"]) > 1500
            take("finished_long" if long else "finished_short", thread_id)
        elif status == "failed":
            take("failed", thread_id)
        elif status in ("interrupted", "cancelled"):
            take("interrupted", thread_id)
    prompts = records.prompts()
    rng.shuffle(prompts)
    for item in prompts:
        if item.get("prompt") and item.get("startedAt"):
            need = {"kind": "approval" if item["type"] == "approval_request" else "user_input",
                    "prompt": item["prompt"][:400]}
            take("waiting_on_you", item["threadId"], item["startedAt"], pending=need)
    for thread_id, status, payload in latest:
        replies = records.replies(thread_id)
        runs = records.runs(thread_id)
        if len(runs) >= 2 and len(replies) >= 3:
            run = runs[-1]
            inside = [m for m in replies if m["runId"] == run["id"]][:-1]
            if inside and run.get("completedAt"):
                take("working", thread_id, inside[-1]["updatedAt"])
    for thread_id, status, payload in latest:
        runs = records.runs(thread_id)
        for earlier, later in zip(runs, runs[1:]):
            if earlier.get("completedAt") and later["requestedAt"] < earlier["completedAt"] and later.get("startedAt"):
                take("queued", thread_id, later["requestedAt"], queued=True)
                break
    states = [state for kind in PLAN for state in picked[kind]]
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "states.json").write_text(json.dumps(states, indent=1), encoding="utf-8")
    print(json.dumps({kind: len(found) for kind, found in picked.items()}))
    return 0


def thread_state(raw: dict) -> T.ThreadState:
    pending = raw.get("pending")
    reply = raw.get("last_reply")
    return T.ThreadState(
        raw["thread_id"], raw["project_id"], raw["title"], T.ThreadActivity(raw["activity"]),
        T.RunInfo(raw["run"]["run_id"], T.RunStatus(raw["run"]["status"]), None),
        T.PendingNeed("q", T.PendingKind.APPROVAL if pending and pending["kind"] == "approval"
                      else T.PendingKind.QUESTION, pending["prompt"],
                      T.NeedSource(pending.get("source") or ("command" if pending["kind"] == "approval"
                                                              else "user_input")))
        if pending else None,
        T.LastReply(reply, "m", None, raw["run"]["run_id"], raw.get("as_of") or "") if reply else None,
        1, recent=tuple(T.decode_recent_record(item) for item in raw.get("recent", ())))


def client(args) -> LiveClient | LocalText:
    """The live client, with retries and long waits: the eval scores wording, and a 429 is not a bad line. The local
    renderer keeps the live timeouts, since a slow local line is a bad line."""
    if args.renderer == "local":
        return LocalText()
    model = LiveClient(cache_dir=args.dir / "cache", timeout_s=20.0, max_attempts=3)
    model.cooldown_s = 0.0
    speech.RENDER_TIMEOUT_S = speech.SUMMARY_TIMEOUT_S = 150.0
    return model


def save_ledger(args, model, command: str) -> None:
    if isinstance(model, LocalText):
        (args.dir / f"ledger-{command}.json").write_text(json.dumps(model.ledger, indent=1), encoding="utf-8")


def decider(args) -> Decider:
    """The live line check over Liquid's d1.2, with retries: a 429 is not a bad line."""
    return Decider(LiquidClient(max_attempts=3))


def verdict_of(rendered) -> dict | None:
    verdict = rendered.verdict
    return None if verdict is None else {"passed": verdict.passed, "ms": verdict.ms, "failed": list(verdict.failed),
                                         "answers": verdict.answers, "error": verdict.error}


def run(args) -> int:
    states = json.loads((args.dir / "states.json").read_text(encoding="utf-8"))
    line_check = decider(args)
    labels = {label["thread_id"]: label for label in json.loads((args.dir / "labels.json").read_text(encoding="utf-8"))}
    model = client(args)
    lane = Lane(model)
    out = []
    for raw in states:
        label = labels[raw["thread_id"]]
        state = thread_state(raw)
        speech = Speech(lane, lambda thread_id, state=state: state, line_check)
        speech.remember(state)
        asked = status_act(state, label["ask"])
        started = time.perf_counter()
        acts, _ = speech.fill((asked,), state.thread_id, (state.as_of_sequence,))
        summary_s = time.perf_counter() - started
        turns = [{"user": label["ask"]}]
        rendered = speech.render(acts, turns)
        out.append({"thread_id": raw["thread_id"], "kind": raw["kind"], "ask": label["ask"],
                    "summary": acts[0].slots.get("summary"), "line": rendered.text, "raw": rendered.raw,
                    "decider": verdict_of(rendered),
                    "problems": rendered.problems, "error": rendered.error, "summary_s": round(summary_s, 2),
                    "render_s": round(time.perf_counter() - started - summary_s, 2)})
        print(json.dumps(out[-1])[:300], flush=True)
    (args.dir / "summaries.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    save_ledger(args, model, "run")
    print(json.dumps({"http_calls": model.calls, "lane_requests": lane.calls}))
    return 0


def acts_run(args) -> int:
    planned = json.loads((args.dir / "acts.json").read_text(encoding="utf-8"))
    model = client(args)
    lane = Lane(model)
    speech = Speech(lane, lambda thread_id: None, decider(args))
    out = []
    for item in planned:
        acts = [Act.from_json(a) for a in item["acts"]]
        rendered = speech.render(acts, item.get("turns", ()))
        out.append({"acts": item["acts"], "turns": item.get("turns", []), "line": rendered.text, "raw": rendered.raw,
                    "decider": verdict_of(rendered),
                    "problems": rendered.problems, "error": rendered.error,
                    "check_on_raw": check(rendered.raw or "", acts) if rendered.raw else None})
        print(json.dumps({"kinds": [a["kind"] for a in item["acts"]], "line": rendered.text,
                          "problems": rendered.problems})[:300], flush=True)
    (args.dir / "rendered.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    save_ledger(args, model, "acts")
    passed = sum(1 for r in out if r["line"] is not None)
    print(json.dumps({"passed": passed, "of": len(out), "http_calls": model.calls, "lane_requests": lane.calls}))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    frozen = sub.add_parser("freeze", help="freeze thread states from a read-only copy of a T3 database")
    frozen.add_argument("--db", type=Path, required=True)
    frozen.add_argument("--out", type=Path, required=True)
    for name, text in (("run", "summarise each frozen state and word its answer"),
                       ("acts", "render the planned acts and check each line")):
        command = sub.add_parser(name, help=text)
        command.add_argument("--dir", type=Path, required=True)
        command.add_argument("--renderer", choices=RENDERERS, default="particle-1")
    args = parser.parse_args(argv)
    return {"freeze": freeze, "run": run, "acts": acts_run}[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
