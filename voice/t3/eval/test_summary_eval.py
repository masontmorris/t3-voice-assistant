"""The summary eval, run on recorded model answers: a thread's title is never evidence of what it did.

The fixture's two threads are named for fixing tests, and their last replies say nothing about tests. Before the
summary prompt named the title as a name only, the free model answered "The thread fixed failing tests" for both
(measured, one call each). Replay uses only the recording for the exact request.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from voice.t3 import fakes as F
from voice.t3.eval import summary_eval
from voice.t3.pick.model import ModelClient
from voice.t3.speech import RENDER_SYSTEM, summary_messages

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "title_not_evidence"
FIXED = re.compile(r"\bfix(?:ed|es|ing)?\b", re.IGNORECASE)
STALE_SUMMARIES = {"19af260b985bd1ec27b595236e1796f2", "5f40001a834029ab3fc3a2c104d79200"}
STALE_RENDER_PROMPT = "38151655b88d9de8ff1362579b63ad55e7d65d1292a940121e3234a8d875744b"


class Replay(ModelClient):
    """The recorded answers only: a request with no recording fails instead of calling the model."""

    def complete(self, messages):
        path = self._path(self._body(messages))
        if not path.exists():
            raise AssertionError(f"no recorded answer for this request ({path.name}); re-record the fixture")
        return json.loads(path.read_text(encoding="utf-8"))


class TitleIsNotEvidence(unittest.TestCase):
    def test_fixing_tests_title_yields_no_fixed_claim(self):
        self.check_replay("thread-fix-failing-tests")

    def test_fixing_tests_and_snapshot_title_yields_no_fixed_claim(self):
        self.check_replay("thread-fix-and-snapshot")

    def check_replay(self, thread_id):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            shutil.copytree(FIXTURE, run_dir)
            states_file = run_dir / "states.json"
            state, = (r for r in json.loads(states_file.read_text(encoding="utf-8")) if r["thread_id"] == thread_id)
            labels = json.loads((run_dir / "labels.json").read_text(encoding="utf-8"))
            label, = (r for r in labels if r["thread_id"] == thread_id)
            states_file.write_text(json.dumps([state]), encoding="utf-8")
            replay = Replay(cache_dir=run_dir / "cache")
            messages = summary_messages(summary_eval.thread_state(state), label["ask"])
            path = replay._path(replay._body(messages))
            if (not path.exists() and path.stem in STALE_SUMMARIES
                    and hashlib.sha256(RENDER_SYSTEM.encode()).hexdigest() == STALE_RENDER_PROMPT):
                # #65: python -m voice.t3.eval.summary_eval run --dir voice/t3/eval/fixtures/title_not_evidence
                self.skipTest("#65: the new summary and render prompts need fresh renderer recordings")
            self.assertTrue(path.is_file(), f"no recorded answer for this request ({path.name}); re-record the fixture")
            with mock.patch.object(summary_eval, "client", lambda args: replay), \
                    mock.patch.object(summary_eval, "decider", lambda args: F.Agree()):
                summary_eval.run(SimpleNamespace(dir=run_dir))
            result, = json.loads((run_dir / "summaries.json").read_text(encoding="utf-8"))
        self.assertTrue(result["summary"], result)
        self.assertTrue(result["line"], result)
        self.assertIsNone(FIXED.search(result["summary"]), result["summary"])


if __name__ == "__main__":
    unittest.main()
