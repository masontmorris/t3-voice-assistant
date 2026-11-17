"""The touched-files check, exercised on throwaway repos."""

from __future__ import annotations

import contextlib
import io
import subprocess
import tempfile
import unittest
from pathlib import Path

from voice.ci import check_seam

ALLOWLIST = "docs/voice/seam.md"
EMPTY_TABLE = "# Upstream files the fork changes\n\n| Path | Reason |\n| --- | --- |\n"


class Fixture:
    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.git("init", "-q")
        self.git("checkout", "-q", "-b", "main")
        self.git("config", "user.email", "seam-check@example.test")
        self.git("config", "user.name", "Seam Check")
        self.git("config", "commit.gpgsign", "false")
        self.git("config", "core.autocrlf", "false")

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(self.root), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        ).stdout

    def write(self, relative: str, text: str) -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")

    def commit(self, message: str) -> None:
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)

    def allow(self, *rows: str) -> None:
        body = "".join(f"| {row} |\n" for row in rows)
        self.write(ALLOWLIST, EMPTY_TABLE + body)
        self.commit("update allowlist")

    def check(self, *extra: str) -> tuple[int, str]:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            code = check_seam.main(
                ["--repo", str(self.root), "--base", "main", "--head", "HEAD", *extra]
            )
        return code, buffer.getvalue()


class SeamCheckTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.repo = Fixture(Path(self._temp.name) / "repo")
        self.repo.write("app.ts", "export const answer = 1;\n")
        self.repo.write("voice/keep.py", "VALUE = 1\n")
        self.repo.write(ALLOWLIST, EMPTY_TABLE)
        self.repo.commit("base")
        self.repo.git("checkout", "-q", "-b", "voice")

    def touch_upstream_file(self, relative: str = "app.ts", text: str = "export const answer = 2;\n") -> None:
        self.repo.write(relative, text)
        self.repo.commit("change an upstream file")

    def test_a_listed_upstream_file_passes(self) -> None:
        self.touch_upstream_file()
        self.repo.allow("app.ts | the seam mount point")
        code, output = self.repo.check()
        self.assertEqual(code, check_seam.EXIT_OK, output)
        self.assertIn("each listed", output)

    def test_an_unlisted_upstream_file_fails_and_is_named(self) -> None:
        self.touch_upstream_file()
        code, output = self.repo.check()
        self.assertEqual(code, check_seam.EXIT_UNLISTED, output)
        self.assertIn("app.ts", output)
        self.assertIn("FAILED", output)

    def test_a_change_inside_voice_is_ignored(self) -> None:
        self.repo.write("voice/live/new_feature.py", "VALUE = 2\n")
        self.repo.commit("voice work")
        code, output = self.repo.check()
        self.assertEqual(code, check_seam.EXIT_OK, output)

    def test_a_change_inside_docs_voice_is_ignored(self) -> None:
        self.repo.write("docs/voice/notes.md", "notes\n")
        self.repo.commit("voice docs")
        code, output = self.repo.check()
        self.assertEqual(code, check_seam.EXIT_OK, output)

    def test_a_folder_that_only_looks_like_a_voice_folder_is_not_ignored(self) -> None:
        self.repo.write("voice-notes.md", "notes\n")
        self.repo.write("docs/voice-old/notes.md", "notes\n")
        self.repo.commit("lookalike folders")
        code, output = self.repo.check()
        self.assertEqual(code, check_seam.EXIT_UNLISTED, output)
        self.assertIn("voice-notes.md", output)
        self.assertIn("docs/voice-old/notes.md", output)

    def test_a_deleted_upstream_file_counts_as_touched(self) -> None:
        (self.repo.root / "app.ts").unlink()
        self.repo.commit("delete an upstream file")
        code, output = self.repo.check()
        self.assertEqual(code, check_seam.EXIT_UNLISTED, output)
        self.assertIn("app.ts", output)

    def test_a_renamed_upstream_file_counts_as_touched(self) -> None:
        self.repo.git("mv", "app.ts", "server.ts")
        self.repo.commit("rename an upstream file")
        code, output = self.repo.check()
        self.assertEqual(code, check_seam.EXIT_UNLISTED, output)
        self.assertIn("app.ts", output)
        self.assertIn("server.ts", output)

    def test_a_change_that_lands_only_on_the_mirror_branch_is_not_touched(self) -> None:
        self.repo.git("checkout", "-q", "main")
        self.touch_upstream_file()
        self.repo.git("checkout", "-q", "voice")
        code, output = self.repo.check()
        self.assertEqual(code, check_seam.EXIT_OK, output)

    def test_an_entry_without_a_reason_still_fails(self) -> None:
        self.touch_upstream_file()
        self.repo.allow("app.ts |")
        code, output = self.repo.check()
        self.assertEqual(code, check_seam.EXIT_UNLISTED, output)
        self.assertIn("no reason", output)

    def test_a_missing_allowlist_is_a_setup_error(self) -> None:
        self.touch_upstream_file()
        code, output = self.repo.check("--allowlist", "docs/voice/absent.md")
        self.assertEqual(code, check_seam.EXIT_SETUP, output)
        self.assertIn("no allowlist", output)

    def test_the_allowlist_ignores_its_header_and_separator_rows(self) -> None:
        entries = check_seam.read_allowlist(self.repo.root / ALLOWLIST)
        self.assertEqual(entries, {})

    def test_a_file_under_a_listed_dir_passes(self) -> None:
        self.touch_upstream_file("apps/server/src/voiceSeam/events.ts")
        self.repo.allow("apps/server/src/voiceSeam/ | voice-owned seam")
        code, output = self.repo.check()
        self.assertEqual(code, check_seam.EXIT_OK, output)
        self.assertIn("each listed", output)
        self.assertIn("apps/server/src/voiceSeam/events.ts", output)

    def test_a_file_under_an_unlisted_dir_fails(self) -> None:
        self.touch_upstream_file("apps/server/src/unlistedDir/events.ts")
        self.repo.allow("apps/server/src/voiceSeam/ | voice-owned seam")
        code, output = self.repo.check()
        self.assertEqual(code, check_seam.EXIT_UNLISTED, output)
        self.assertIn("FAILED", output)
        self.assertIn("apps/server/src/unlistedDir/events.ts", output)

    def test_a_listed_dir_that_upstream_also_has_files_in_fails(self) -> None:
        self.repo.git("checkout", "-q", "main")
        self.repo.write("apps/server/src/preview/existing.ts", "export const x = 1;\n")
        self.repo.commit("upstream preview file")
        self.repo.git("checkout", "-q", "voice")
        self.touch_upstream_file("apps/server/src/preview/new.ts")
        self.repo.allow("apps/server/src/preview/ | voice-owned seam")
        code, output = self.repo.check()
        self.assertEqual(code, check_seam.EXIT_UNLISTED, output)
        self.assertIn("FAILED", output)
        self.assertIn("apps/server/src/preview/existing.ts", output)

    def test_a_dir_row_with_no_reason_fails(self) -> None:
        self.touch_upstream_file("apps/server/src/voiceSeam/events.ts")
        self.repo.allow("apps/server/src/voiceSeam/ |")
        code, output = self.repo.check()
        self.assertEqual(code, check_seam.EXIT_UNLISTED, output)
        self.assertIn("no reason", output)

    def test_a_reason_of_dash_fails(self) -> None:
        self.touch_upstream_file()
        self.repo.allow("app.ts | -")
        code, output = self.repo.check()
        self.assertEqual(code, check_seam.EXIT_UNLISTED, output)
        self.assertIn("no reason", output)

    def test_rows_inside_fenced_code_blocks_are_ignored(self) -> None:
        body = (
            "# Code block with a table\n\n"
            "```markdown\n"
            "| app.ts | inside code block |\n"
            "```\n"
        )
        self.repo.write(ALLOWLIST, EMPTY_TABLE + body)
        self.repo.commit("add code block")
        entries = check_seam.read_allowlist(self.repo.root / ALLOWLIST)
        self.assertEqual(entries, {})


if __name__ == "__main__":
    unittest.main()
