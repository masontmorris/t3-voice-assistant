"""The summary eval, run on recorded model answers: a thread's title is never evidence of what it did.

The fixture's two threads are named for fixing tests, and their last replies say nothing about tests. Before the
summary prompt named the title as a name only, the free model answered "The thread fixed failing tests" for both
(measured, one call each). The recorded answers are replayed, so the test makes no live call. A changed summary or
render prompt misses the recording and fails here. Re-record with

    VOICE_ENV_FILE=<env file> python -m voice.t3.eval.summary_eval run --dir voice/t3/eval/fixtures/title_not_evidence

then delete the summaries.json it writes.
"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from voice.t3.eval import summary_eval
from voice.t3.pick.model import ModelClient

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "title_not_evidence"
FIXED = re.compile(r"\bfix(?:ed|es|ing)?\b", re.IGNORECASE)


class Replay(ModelClient):
    """The recorded answers only: a request with no recording fails instead of calling the model."""

    def complete(self, messages):
        path = self._path(self._body(messages))
        if not path.exists():
            raise AssertionError(f"no recorded answer for this request ({path.name}); re-record the fixture")
        return json.loads(path.read_text(encoding="utf-8"))


class TitleIsNotEvidence(unittest.TestCase):
    def test_a_title_about_fixing_tests_yields_no_fixed_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            shutil.copytree(FIXTURE, run_dir)
            with mock.patch.object(summary_eval, "client", lambda args: Replay(cache_dir=args.dir / "cache")):
                summary_eval.run(SimpleNamespace(dir=run_dir))
            results = json.loads((run_dir / "summaries.json").read_text(encoding="utf-8"))
        self.assertEqual(len(results), 2)
        for result in results:
            with self.subTest(result["thread_id"]):
                self.assertTrue(result["summary"], result)
                self.assertIsNone(FIXED.search(result["summary"]), result["summary"])


if __name__ == "__main__":
    unittest.main()
