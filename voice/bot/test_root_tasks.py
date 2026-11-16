"""Tests for RootTaskService against a fake bot API. Run from the repo root with the prototype checkout on the path:

    VOICE_PROTOTYPE_CHECKOUT=DIR python -I voice/bot/test_root_tasks.py
"""

import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path[:0] = [os.environ["VOICE_PROTOTYPE_CHECKOUT"], str(Path(__file__).resolve().parent)]
SECRET = "test-secret-for-the-fake-bot"
_secret_file = Path(tempfile.mkdtemp()) / "api-secret"
_secret_file.write_text(SECRET, encoding="utf-8")
os.environ["BOT_API_SECRET_FILE"] = str(_secret_file)

from root_tasks import RootTaskService  # noqa: E402
from voice.prototype.tasks import Run  # noqa: E402


class FakeBot(BaseHTTPRequestHandler):
    handoffs, outbox, acked = [], [], []

    def log_message(self, *args):
        pass

    def reply(self, body):
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(data)

    def refused(self):
        if self.headers.get("x-bot-secret") != SECRET:
            self.send_response(401)
            self.end_headers()
            return True
        return False

    def do_POST(self):
        if self.refused():
            return
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        if self.path == "/handoff":
            known = any(h["requestId"] == body["requestId"] for h in self.handoffs)
            if not known:
                self.handoffs.append(body)
            return self.reply({"status": "exists" if known else "created", "requestId": body["requestId"]})
        self.acked.extend(body["ids"])
        return self.reply({"ok": True})

    def do_GET(self):
        if self.refused():
            return
        after = self.path.split("after=")[1].split("&")[0]
        start = next((i + 1 for i, e in enumerate(self.outbox) if e["id"] == after), 0)
        return self.reply({"entries": self.outbox[start:]})


class FakeSegmenter:
    def __init__(self):
        self._records = [
            {"task_id": "task_1", "instruction": "Add milk to the shopping list.", "side_effect": True, "sources": ["orchestrator"], "status": "dispatched", "depends_on": []},
            {"task_id": "task_2", "instruction": "How long is the drive to Boulder?", "side_effect": False, "sources": ["orchestrator"], "status": "dispatched", "depends_on": []},
        ]
        self.dispatch_log = [{"task_id": "task_1", "instruction": "Add milk to the shopping list."},
                             {"task_id": "task_2", "instruction": "How long is the drive to Boulder?"}]

    def records(self):
        return self._records


class RootTaskServiceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), FakeBot)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"

    def setUp(self):
        FakeBot.handoffs.clear(), FakeBot.outbox.clear(), FakeBot.acked.clear()

    def test_read_only_goes_to_the_root_and_side_effect_is_held(self):
        service = RootTaskService(FakeSegmenter(), "conv_t", self.url)
        service.sync(0.0)
        self.assertEqual([h["requestId"] for h in FakeBot.handoffs], ["conv_t:task_2"])
        self.assertEqual(service.executions["task_1"].state, Run.HELD)
        self.assertEqual(service.executions["task_2"].state, Run.RUNNING)
        service.sync(1.0)
        self.assertEqual(len(FakeBot.handoffs), 1, "a second sync hands nothing off again")

    def test_result_from_the_outbox_completes_the_task_and_is_acked_once_told(self):
        service = RootTaskService(FakeSegmenter(), "conv_t", self.url)
        service.sync(0.0)
        FakeBot.outbox.extend([
            {"id": "o1", "source": "root_reply", "requestId": "conv_t:task_2", "text": "Passed it on."},
            {"id": "o2", "source": "task_result", "requestId": "conv_t:task_2", "text": "About 35 minutes."},
        ])
        self.assertEqual(service.poll(5.0), ["task_2"])
        self.assertEqual(service.executions["task_2"].result_summary, "About 35 minutes.")
        self.assertIsNone(service.next_due())
        self.assertEqual(service.take_updates()[0]["result_summary"], "About 35 minutes.")
        self.assertEqual(FakeBot.acked, ["o2"])


if __name__ == "__main__":
    unittest.main()
