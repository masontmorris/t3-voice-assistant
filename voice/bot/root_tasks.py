"""A task service that hands the prototype's read-only tasks to the standing root, through the bot's localhost API.

RootTaskService has the interface of voice.prototype.tasks.MockTaskService, so the prototype can use it in place of
the mock: submit_delegate, sync, poll, next_due, snapshot, take_updates and executions. Only sync, poll and next_due
change. A dispatched task that the mock would run goes to the root as a handoff, keyed by
"<conversation_id>:<task_id>", so a retry makes no second task. Its result comes back as a task_result entry in the
bot's outbox. A side-effect task, or one the decider never cleared, is held for confirmation as before and never
reaches the root.

It needs only the standard library and voice.prototype.tasks, so either prototype branch can adopt it. Every request
carries the bot's shared secret in the x-bot-secret header. The secret is read from the bot's state dir, or from the file
named by BOT_API_SECRET_FILE, and is never printed.
"""

import json
import os
import urllib.parse
import urllib.request

from pathlib import Path

from voice.prototype.tasks import Execution, MockTaskService, Run, needs_hold

SECRET_FILE = Path(__file__).resolve().parent / "state" / "api-secret"


def read_secret():
    return Path(os.environ.get("BOT_API_SECRET_FILE") or SECRET_FILE).read_text(encoding="utf-8").strip()


class RootTaskService(MockTaskService):
    def __init__(self, segmenter, conversation_id, bot_url="http://127.0.0.1:8787", poll_s=1.0):
        super().__init__(segmenter, conversation_id)
        self.bot_url, self.poll_s = bot_url.rstrip("/"), poll_s
        self._cursor, self._last = None, 0.0
        self._outbox_ids = {}
        self._secret = read_secret()

    def _call(self, method, path, body=None, timeout=75):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.bot_url + path, data=data, method=method,
                                         headers={"content-type": "application/json", "x-bot-secret": self._secret})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read())

    def request_id(self, task_id):
        return f"{self.conversation_id}:{task_id}"

    def sync(self, now):
        """Hand new dispatches to the root, or hold them. Cancellations stay local: the root is not told."""
        events = []
        records = {r["task_id"]: r for r in self.segmenter.records()}
        for entry in self.segmenter.dispatch_log[self._seen:]:
            task_id = entry["task_id"]
            if needs_hold(records[task_id]):
                execution = Execution(task_id, Run.HELD, now, None)
            else:
                answer = self._call("POST", "/handoff", {"requestId": self.request_id(task_id),
                                                         "text": entry["instruction"], "utterance": entry.get("revision")})
                execution = Execution(task_id, Run.RUNNING, now, None)
                events.append({"task_id": task_id, "event": "handoff", "status": answer["status"]})
            self.executions[task_id] = execution
            events.append({"task_id": task_id, "event": execution.state.value, "instruction": entry["instruction"]})
        self._seen = len(self.segmenter.dispatch_log)
        for task_id, execution in self.executions.items():
            if records[task_id]["status"] == "cancel_requested" and execution.state in (Run.RUNNING, Run.HELD):
                execution.state, execution.finished_at = Run.CANCELLED, now
                events.append({"task_id": task_id, "event": Run.CANCELLED.value})
        return events

    def poll(self, now, wait_s=0.0):
        """Complete running tasks whose result is in the outbox. wait_s > 0 blocks until an entry arrives or time runs out."""
        query = urllib.parse.urlencode({"after": self._cursor or "", "waitMs": int(wait_s * 1000)})
        entries = self._call("GET", f"/outbox?{query}")["entries"]
        by_request = {self.request_id(t): t for t, e in self.executions.items() if e.state == Run.RUNNING}
        done = []
        for entry in entries:
            self._cursor = entry["id"]
            task_id = by_request.get(entry["requestId"])
            if entry["source"] == "task_result" and task_id:
                execution = self.executions[task_id]
                execution.state, execution.finished_at, execution.result_summary = Run.COMPLETED, now, entry["text"]
                self._outbox_ids[task_id] = entry["id"]
                done.append(task_id)
        self._last = now
        return done

    def next_due(self):
        running = any(e.state == Run.RUNNING for e in self.executions.values())
        return self._last + self.poll_s if running else None

    def _ack(self, task_ids):
        ids = [self._outbox_ids[t] for t in task_ids if t in self._outbox_ids]
        if ids:
            self._call("POST", "/outbox/ack", {"ids": ids})

    def snapshot(self, utterance_id, final_transcript):
        snapshot = super().snapshot(utterance_id, final_transcript)
        self._ack([t["task_id"] for t in snapshot["tasks"] if t["status"] == Run.COMPLETED])
        return snapshot

    def take_updates(self):
        updates = super().take_updates()
        self._ack([u["task_id"] for u in updates])
        return updates
