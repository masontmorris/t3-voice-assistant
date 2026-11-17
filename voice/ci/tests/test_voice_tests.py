"""The voice test gate, run as a script against a throwaway repo tree.

The gate is spawned with the fixture repo on PYTHONPATH, so its discovery, imports, and exit code are the ones a real
run produces.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from voice.ci import voice_tests

SCRIPT = Path(voice_tests.__file__).resolve()


def write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text), encoding="utf-8", newline="\n")


def run_gate(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ, PYTHONPATH=str(repo))
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--repo", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=300,
    )


class GateTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.repo = Path(self._temp.name) / "repo"
        (self.repo / "voice").mkdir(parents=True)
        (self.repo / "voice" / "__init__.py").write_text("", encoding="utf-8")

    def test_a_cancelled_node_suite_fails_the_gate(self) -> None:
        if not shutil.which("node"):
            self.skipTest("node is not on PATH")
        write(
            self.repo,
            "voice/bot/cancelled.test.mjs",
            """\
            import { describe, it, before } from "node:test";
            describe("suite", () => {
              before(() => { throw new Error("boom"); });
              it("one", () => {});
            });
            """,
        )

        completed = run_gate(self.repo)

        self.assertEqual(completed.returncode, 1, completed.stdout + completed.stderr)
        self.assertIn("cancelled", completed.stdout)
        self.assertIn("hook threw", completed.stdout)

    def test_a_skip_not_in_the_skip_baseline_fails_the_gate(self) -> None:
        self.skipping_module()

        completed = run_gate(self.repo)

        self.assertEqual(completed.returncode, 1, completed.stdout + completed.stderr)
        self.assertIn("not in the skip baseline", completed.stdout)
        self.assertIn("voice.thing.tests.test_thing.Thing.test_it", completed.stdout)

    def test_a_skip_listed_in_the_baseline_passes(self) -> None:
        self.skipping_module()
        write(
            self.repo,
            "voice/ci/known_skips.txt",
            "voice.thing.tests.test_thing.Thing.test_it  # expected in this fixture\n",
        )

        completed = run_gate(self.repo)

        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn("voice tests OK", completed.stdout)

    def test_a_module_level_optional_import_skip_is_counted(self) -> None:
        write(self.repo, "voice/thing/tests/test_audio.py", "import sounddevice.no_such_submodule\n")

        completed = run_gate(self.repo)

        self.assertEqual(completed.returncode, 1, completed.stdout + completed.stderr)
        self.assertIn("voice.thing.tests: 0 passed, 0 failed, 1 skipped", completed.stdout)
        self.assertIn("voice.thing.tests.test_audio", completed.stdout)

    def test_vendored_directories_are_not_discovered(self) -> None:
        write(self.repo, "voice/kept/tests/test_kept.py", "")
        write(self.repo, "voice/node_modules/pkg/test_vendored.py", "")
        write(self.repo, "voice/.venv/lib/test_vendored.py", "")
        write(self.repo, "voice/bot/kept.test.mjs", "")
        write(self.repo, "voice/node_modules/pkg/vendored.test.mjs", "")
        write(self.repo, "voice/.venv/lib/vendored.test.mjs", "")

        self.assertEqual(voice_tests.test_modules(self.repo), ["voice.kept.tests.test_kept"])
        self.assertEqual([path.name for path in voice_tests.node_test_files(self.repo)], ["kept.test.mjs"])

    def skipping_module(self) -> None:
        write(self.repo, "voice/thing/tests/__init__.py", "")
        write(
            self.repo,
            "voice/thing/tests/test_thing.py",
            """\
            import unittest

            class Thing(unittest.TestCase):
                @unittest.skip("needs a fixture")
                def test_it(self):
                    pass
            """,
        )


if __name__ == "__main__":
    unittest.main()
