"""The upstream sync, exercised on throwaway repo pairs.

Each test builds two bare remotes (a fake origin and a fake upstream) and a clone with both remotes. The sync runs
for real against those paths. The test.sh inside the fixture is a stand-in, because these tests cover the sync's
control flow; the real suite runs in the real repo.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

CI_DIR = Path(__file__).resolve().parents[1]
SCRIPT = CI_DIR / "upstream_sync.sh"
CHECK_SEAM = CI_DIR / "check_seam.py"

SEAM_MD = "# Upstream files\n\n| Path | Reason |\n| --- | --- |\n"
PASSING_TEST = '#!/usr/bin/env bash\necho "voice tests OK: 0 python tests ran in 0.0s, 0 known failure(s)."\nexit 0\n'
FAILING_TEST = '#!/usr/bin/env bash\necho "voice tests FAILED: 1 failure(s) not in the baseline."\necho "  test_a_thing (fail)"\nexit 1\n'
SLOW_TEST = (
    '#!/usr/bin/env bash\n'
    'printf \'in the test step\\n\' >"$VOICE_SYNC_TEST_STARTED"\n'
    'read -r _release\n'
    'echo "voice tests OK: 0 python tests ran in 0.0s, 0 known failure(s)."\n'
    'exit 0\n'
)


def git(*args: str, cwd: Path | None = None) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if completed.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {completed.stderr.strip()}")
    return completed.stdout.strip()


def posix(path: Path) -> str:
    return path.resolve().as_posix()


def find_bash() -> str | None:
    candidates = [
        os.environ.get("VOICE_BASH"),
        shutil.which("bash"),
        r"C:\Program Files\Git\bin\bash.exe",
        "/bin/bash",
    ]
    probe = Path(tempfile.gettempdir())
    for candidate in candidates:
        if not candidate:
            continue
        result = subprocess.run(
            [candidate, "-c", 'test -d "$1"', "-", posix(probe)],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return candidate
    return None


class Fixture:
    def __init__(self, root: Path, test_script: str = PASSING_TEST) -> None:
        self.root = root
        self.origin = root / "origin.git"
        self.upstream = root / "upstream.git"
        self.source = root / "source"
        self.worktree = root / "sync-worktree"
        self.voice_home = root / "voice-home"
        self.test_script = test_script

        git("init", "--bare", "-q", "--initial-branch=main", str(self.origin))
        git("init", "--bare", "-q", "--initial-branch=main", str(self.upstream))
        git("clone", "-q", str(self.origin), str(self.source))
        self.git("checkout", "-q", "-B", "main")
        self.git("config", "user.email", "sync@example.test")
        self.git("config", "user.name", "Sync Test")
        self.git("config", "commit.gpgsign", "false")
        self.git("config", "core.autocrlf", "false")
        self.git("remote", "add", "upstream", str(self.upstream))

    def git(self, *args: str) -> str:
        return git(*args, cwd=self.source)

    def write(self, relative: str, text: str) -> None:
        path = self.source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")

    def commit(self, message: str) -> None:
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)

    def publish_base(self) -> None:
        self.write("README.md", "base readme\n")
        self.write("docs/voice/seam.md", SEAM_MD)
        self.commit("base")
        self.git("push", "-q", "origin", "main")
        self.git("push", "-q", "upstream", "main")

    def publish_voice_branch(self) -> None:
        self.git("checkout", "-q", "-B", "voice/main")
        self.write("voice/app.py", "VOICE = 1\n")
        self.write("voice/ci/test.sh", self.test_script)
        self.write("voice/ci/check_seam.py", CHECK_SEAM.read_text(encoding="utf-8"))
        self.commit("voice work")
        self.git("push", "-q", "origin", "voice/main")
        self.git("checkout", "-q", "main")

    def advance_upstream(self, relative: str = "CHANGELOG.md", text: str = "upstream change\n") -> str:
        work = self.root / "upstream-work"
        git("clone", "-q", str(self.upstream), str(work))
        git("config", "user.email", "sync@example.test", cwd=work)
        git("config", "user.name", "Sync Test", cwd=work)
        git("config", "commit.gpgsign", "false", cwd=work)
        target = work / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="\n")
        git("add", "-A", cwd=work)
        git("commit", "-q", "-m", "upstream change", cwd=work)
        git("push", "-q", "origin", "main", cwd=work)
        return git("rev-parse", "HEAD", cwd=work)

    def on_voice_branch(self, relative: str, text: str) -> None:
        self.git("checkout", "-q", "voice/main")
        self.write(relative, text)
        self.commit("voice edit")
        self.git("push", "-q", "origin", "voice/main")
        self.git("checkout", "-q", "main")

    def remote_sha(self, repo: Path, ref: str) -> str:
        return git("-C", str(repo), "rev-parse", ref)


class UpstreamSyncTest(unittest.TestCase):
    bash: str

    @classmethod
    def setUpClass(cls) -> None:
        bash = find_bash()
        if not bash:
            raise unittest.SkipTest("no bash that can see this checkout")
        cls.bash = bash

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.fixture = Fixture(Path(self._temp.name) / "fixture")

    def run_env(self, env: dict[str, str] | None = None) -> dict[str, str]:
        result = dict(os.environ, VOICE_PYTHON=sys.executable, VOICE_SYNC_VOICE_BRANCH="voice/main")
        result.update(env or {})
        return result

    def command(self, *args: str, worktree: str | None = None) -> list[str]:
        return [
            self.bash,
            posix(SCRIPT),
            *args,
            "--repo",
            posix(self.fixture.source),
            "--worktree",
            worktree or posix(self.fixture.worktree),
            "--log",
            posix(self.fixture.voice_home / "sync" / "sync.log"),
        ]

    def sync(
        self,
        *args: str,
        worktree: str | None = None,
        env: dict[str, str] | None = None,
        cwd: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            self.command(*args, worktree=worktree),
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=self.run_env(env),
            stdin=subprocess.DEVNULL,
            cwd=cwd,
            timeout=300,
        )

    def start_sync(
        self, *args: str, env: dict[str, str] | None = None
    ) -> subprocess.Popen[str]:
        return subprocess.Popen(
            self.command(*args),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            env=self.run_env(env),
        )

    @staticmethod
    def stop(process: subprocess.Popen[str]) -> None:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=120)

    @staticmethod
    def wait_for(path: Path, timeout: float = 120.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if path.exists():
                return True
            time.sleep(0.05)
        return False

    def lock(self, pid: int | None, started_seconds_ago: float | None) -> Path:
        lock = self.fixture.voice_home / "sync" / "run.lock"
        lock.mkdir(parents=True)
        if pid is not None:
            (lock / "pid").write_text(f"{pid}\n", encoding="utf-8")
        if started_seconds_ago is not None:
            (lock / "started").write_text(f"{int(time.time() - started_seconds_ago)}\n", encoding="utf-8")
        return lock

    def log_lines(self) -> list[str]:
        log = self.fixture.voice_home / "sync" / "sync.log"
        if not log.is_file():
            return []
        return [line for line in log.read_text(encoding="utf-8").splitlines() if line]

    def test_a_clean_dry_run_merges_and_leaves_both_remotes_alone(self) -> None:
        self.fixture.publish_base()
        self.fixture.publish_voice_branch()
        upstream_sha = self.fixture.advance_upstream()
        origin_before = self.fixture.remote_sha(self.fixture.origin, "main")
        voice_before = self.fixture.remote_sha(self.fixture.origin, "voice/main")

        completed = self.sync("--dry-run")

        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn("dry run passed", completed.stdout)
        self.assertEqual(self.fixture.remote_sha(self.fixture.origin, "main"), origin_before)
        self.assertEqual(self.fixture.remote_sha(self.fixture.origin, "voice/main"), voice_before)
        head = git("-C", str(self.fixture.worktree), "rev-parse", "HEAD")
        upstream_short = git("-C", str(self.fixture.worktree), "rev-parse", "--short=10", upstream_sha)
        self.assertIn(
            f"would push {upstream_short} to origin/main and {head[:10]} to origin/voice/main",
            completed.stdout,
        )
        git("-C", str(self.fixture.worktree), "merge-base", "--is-ancestor", upstream_sha, head)
        self.assertIn("dry_run=1", self.log_lines()[-1])
        self.assertIn("code=0", self.log_lines()[-1])

    def test_a_live_run_pushes_both_branches_and_the_next_run_is_up_to_date(self) -> None:
        self.fixture.publish_base()
        self.fixture.publish_voice_branch()
        upstream_sha = self.fixture.advance_upstream()

        first = self.sync()

        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertIn("upstream merged and pushed", first.stdout)
        self.assertEqual(self.fixture.remote_sha(self.fixture.origin, "main"), upstream_sha)
        merged = self.fixture.remote_sha(self.fixture.origin, "voice/main")
        git("-C", str(self.fixture.origin), "merge-base", "--is-ancestor", upstream_sha, merged)

        second = self.sync()

        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertIn("up to date", second.stdout)

    def test_a_second_run_with_nothing_new_says_up_to_date(self) -> None:
        self.fixture.publish_base()
        self.fixture.publish_voice_branch()

        completed = self.sync("--dry-run")

        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn("up to date", completed.stdout)
        self.assertIn("nothing new", completed.stdout)

    def test_a_fork_commit_on_main_is_an_alarm(self) -> None:
        self.fixture.publish_base()
        self.fixture.publish_voice_branch()
        self.fixture.write("README.md", "a fork-only edit\n")
        self.fixture.commit("fork edit on main")
        self.fixture.git("push", "-q", "origin", "main")

        completed = self.sync("--dry-run")

        self.assertEqual(completed.returncode, 11, completed.stdout + completed.stderr)
        self.assertIn("not a pure mirror", completed.stdout)
        self.assertIn("code=11", self.log_lines()[-1])

    def test_a_conflicting_merge_stops_and_names_the_file(self) -> None:
        self.fixture.publish_base()
        self.fixture.publish_voice_branch()
        self.fixture.advance_upstream("README.md", "upstream readme\n")
        self.fixture.on_voice_branch("README.md", "voice readme\n")
        voice_before = self.fixture.remote_sha(self.fixture.origin, "voice/main")

        completed = self.sync("--dry-run")

        self.assertEqual(completed.returncode, 12, completed.stdout + completed.stderr)
        self.assertIn("README.md", completed.stdout)
        self.assertIn("CONFLICT", completed.stdout)
        self.assertEqual(self.fixture.remote_sha(self.fixture.origin, "voice/main"), voice_before)
        status = git("-C", str(self.fixture.worktree), "status", "--porcelain")
        self.assertEqual(status, "", "the abort left the worktree dirty")

    def test_a_failing_test_stops_before_the_push(self) -> None:
        self.fixture = Fixture(Path(self._temp.name) / "failing-fixture", test_script=FAILING_TEST)
        self.fixture.publish_base()
        self.fixture.publish_voice_branch()
        self.fixture.advance_upstream()
        origin_before = self.fixture.remote_sha(self.fixture.origin, "main")

        completed = self.sync("--dry-run")

        self.assertEqual(completed.returncode, 13, completed.stdout + completed.stderr)
        self.assertIn("voice tests FAILED", completed.stdout + completed.stderr)
        self.assertEqual(self.fixture.remote_sha(self.fixture.origin, "main"), origin_before)
        self.assertIn("code=13", self.log_lines()[-1])

    def test_an_upstream_file_the_allowlist_does_not_cover_fails_the_seam_check(self) -> None:
        self.fixture.publish_base()
        self.fixture.publish_voice_branch()
        self.fixture.advance_upstream()
        self.fixture.on_voice_branch("README.md", "voice readme\n")

        completed = self.sync("--dry-run")

        self.assertEqual(completed.returncode, 14, completed.stdout + completed.stderr)
        self.assertIn("README.md", completed.stdout)
        self.assertIn("not covered", completed.stdout)

    def test_a_running_sync_holds_a_lock(self) -> None:
        self.fixture.publish_base()
        self.fixture.publish_voice_branch()
        lock = self.fixture.voice_home / "sync" / "run.lock"
        lock.mkdir(parents=True)

        completed = self.sync("--dry-run")

        self.assertEqual(completed.returncode, 17, completed.stdout + completed.stderr)
        self.assertIn("already running", completed.stdout)
        lock.rmdir()

    def test_the_lock_is_released_after_a_run(self) -> None:
        self.fixture.publish_base()
        self.fixture.publish_voice_branch()

        completed = self.sync("--dry-run")

        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertFalse((self.fixture.voice_home / "sync" / "run.lock").exists())

    def test_a_case_differing_path_to_the_source_repo_is_refused(self) -> None:
        self.fixture.publish_base()
        self.fixture.publish_voice_branch()
        self.fixture.advance_upstream()
        keep = self.fixture.source / "keep-me.txt"
        keep.write_text("untracked\n", encoding="utf-8")
        head_before = git("-C", str(self.fixture.source), "rev-parse", "HEAD")
        upper = Path(str(self.fixture.source).upper())
        alias = str(upper if upper.exists() and upper.resolve() == self.fixture.source.resolve()
                    else self.fixture.source / ".." / self.fixture.source.name)

        completed = self.sync("--dry-run", worktree=alias)

        self.assertEqual(completed.returncode, 16, completed.stdout + completed.stderr)
        self.assertIn("refusing", completed.stdout)
        self.assertTrue(keep.is_file(), "the run deleted an untracked file in the source repo")
        self.assertEqual(git("-C", str(self.fixture.source), "rev-parse", "HEAD"), head_before)

    def test_a_subfolder_of_the_source_repo_is_refused(self) -> None:
        self.fixture.publish_base()
        self.fixture.publish_voice_branch()
        self.fixture.advance_upstream()
        docs = self.fixture.source / "docs"
        keep = docs / "keep-me.txt"
        keep.write_text("untracked\n", encoding="utf-8")
        head_before = git("-C", str(self.fixture.source), "rev-parse", "HEAD")

        completed = self.sync("--dry-run", worktree=posix(docs))

        self.assertEqual(completed.returncode, 16, completed.stdout + completed.stderr)
        self.assertIn("inside the source repo's work tree", completed.stdout)
        self.assertTrue(keep.is_file(), "the run deleted an untracked file under the source repo")
        self.assertEqual(git("-C", str(self.fixture.source), "rev-parse", "HEAD"), head_before)

    def test_a_worktree_inside_the_source_repo_is_refused_before_anything_is_created(self) -> None:
        self.fixture.publish_base()
        self.fixture.publish_voice_branch()
        self.fixture.advance_upstream()
        nested = self.fixture.source / "nested" / "sync-worktree"

        completed = self.sync("--dry-run", worktree=posix(nested))

        self.assertEqual(completed.returncode, 16, completed.stdout + completed.stderr)
        self.assertIn("inside the source repo's work tree", completed.stdout)
        self.assertFalse(nested.parent.exists(), "the run created a worktree inside the source repo")
        worktrees = git("-C", str(self.fixture.source), "worktree", "list", "--porcelain")
        self.assertNotIn(nested.as_posix(), worktrees)

    def test_a_worktree_where_git_cannot_name_a_top_level_is_refused(self) -> None:
        self.fixture.publish_base()
        self.fixture.publish_voice_branch()
        self.fixture.advance_upstream()
        bare = self.fixture.root / "bare.git"
        git("init", "--bare", "-q", str(bare))

        completed = self.sync("--dry-run", worktree=posix(bare), cwd=bare)

        self.assertEqual(completed.returncode, 16, completed.stdout + completed.stderr)
        self.assertIn("cannot name its top level", completed.stdout)
        self.assertEqual(git("-C", str(bare), "rev-parse", "--is-bare-repository"), "true")

    def test_another_linked_worktree_is_refused(self) -> None:
        self.fixture.publish_base()
        self.fixture.publish_voice_branch()
        self.fixture.advance_upstream()
        other = self.fixture.root / "other-worktree"
        git("-C", str(self.fixture.source), "worktree", "add", "--detach", str(other), "main")
        keep = other / "keep-me.txt"
        keep.write_text("untracked\n", encoding="utf-8")

        completed = self.sync("--dry-run", worktree=posix(other))

        self.assertEqual(completed.returncode, 16, completed.stdout + completed.stderr)
        self.assertIn("voice-sync-worktree", completed.stdout)
        self.assertTrue(keep.is_file(), "the run deleted an untracked file in another worktree")

    def test_a_live_lock_refuses_and_prints_its_pid_and_age(self) -> None:
        self.fixture.publish_base()
        self.fixture.publish_voice_branch()
        self.lock(os.getpid(), 0)

        completed = self.sync("--dry-run")

        self.assertEqual(completed.returncode, 17, completed.stdout + completed.stderr)
        self.assertIn("already running", completed.stdout)
        self.assertRegex(completed.stderr, rf"pid {os.getpid()}, started \d+ s ago")

    def test_a_second_live_run_refuses_while_the_first_is_in_its_test_step(self) -> None:
        root = Path(self._temp.name) / "overlap-fixture"
        self.fixture = Fixture(root, test_script=SLOW_TEST)
        self.fixture.publish_base()
        self.fixture.publish_voice_branch()
        self.fixture.advance_upstream()
        voice_before = self.fixture.remote_sha(self.fixture.origin, "voice/main")
        started = self.fixture.worktree / "first-run-test-step"
        env = {"VOICE_SYNC_TEST_STARTED": posix(started)}

        first = self.start_sync("--dry-run", env=env)
        self.addCleanup(self.stop, first)
        self.assertTrue(self.wait_for(started), "the first run never reached its test step")
        head_during_first = git("-C", str(self.fixture.worktree), "rev-parse", "HEAD")

        second = self.sync("--dry-run", env=env)

        self.assertEqual(second.returncode, 17, second.stdout + second.stderr)
        self.assertIn("already running", second.stdout)
        self.assertNotIn("taking over", second.stderr)
        self.assertTrue(started.is_file(), "the second run reset the first run's worktree mid-test")
        self.assertEqual(git("-C", str(self.fixture.worktree), "rev-parse", "HEAD"), head_during_first)
        self.assertEqual(self.fixture.remote_sha(self.fixture.origin, "voice/main"), voice_before)

        first.stdin.write("\n")
        first.stdin.flush()
        stdout, stderr = first.communicate(timeout=300)
        self.assertEqual(first.returncode, 0, stdout + stderr)
        self.assertIn("dry run passed", stdout)

    def test_a_lock_whose_pid_is_gone_is_taken_over(self) -> None:
        self.fixture.publish_base()
        self.fixture.publish_voice_branch()
        self.fixture.advance_upstream()
        lock = self.lock(99999999, 0)

        completed = self.sync("--dry-run")

        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn("taking over a lock whose pid 99999999 is gone", completed.stderr)
        self.assertFalse(lock.exists())

    def test_a_takeover_that_finds_a_fresh_lock_puts_it_back_and_refuses(self) -> None:
        self.fixture.publish_base()
        self.fixture.publish_voice_branch()
        self.fixture.advance_upstream()
        lock = self.lock(99999999, 0)
        hook = self.fixture.root / "bash-env.sh"
        hook.write_text(
            'mv() {\n'
            '  case "$1" in\n'
            '    */run.lock)\n'
            '      printf "%s\\n" "$VOICE_SYNC_RIVAL_PID" >"$1/pid"\n'
            '      date +%s >"$1/started"\n'
            '      ;;\n'
            '  esac\n'
            '  command mv "$@"\n'
            '}\n',
            encoding="utf-8",
            newline="\n",
        )

        completed = self.sync(
            "--dry-run", env={"BASH_ENV": posix(hook), "VOICE_SYNC_RIVAL_PID": str(os.getpid())}
        )

        self.assertEqual(completed.returncode, 17, completed.stdout + completed.stderr)
        self.assertEqual((lock / "pid").read_text(encoding="utf-8").strip(), str(os.getpid()))
        self.assertEqual(list(lock.parent.glob("run.lock.stale.*")), [])
        self.assertFalse(self.fixture.worktree.exists(), "the run went on without the lock")

    def test_a_lock_older_than_three_hours_is_taken_over(self) -> None:
        self.fixture.publish_base()
        self.fixture.publish_voice_branch()
        self.fixture.advance_upstream()
        lock = self.lock(os.getpid(), 4 * 60 * 60)

        completed = self.sync("--dry-run")

        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn("taking over a stale lock", completed.stderr)
        self.assertFalse(lock.exists())

    def test_a_merge_that_fails_without_a_conflict_prints_the_git_output(self) -> None:
        self.fixture.publish_base()
        self.fixture.publish_voice_branch()
        self.fixture.advance_upstream()

        completed = self.sync("--dry-run", env={"GIT_COMMITTER_NAME": "", "GIT_COMMITTER_EMAIL": ""})

        self.assertEqual(completed.returncode, 12, completed.stdout + completed.stderr)
        self.assertIn("MERGE FAILED", completed.stdout)
        self.assertIn("empty ident name", completed.stdout, "the git output was swallowed")

    def test_a_rejected_voice_branch_push_moves_neither_branch(self) -> None:
        self.fixture.publish_base()
        self.fixture.publish_voice_branch()
        self.fixture.advance_upstream()
        hook = self.fixture.origin / "hooks" / "pre-receive"
        hook.write_text(
            "#!/bin/sh\n"
            "while read old new ref; do\n"
            "  if [ \"$ref\" = refs/heads/voice/main ]; then\n"
            "    echo 'voice/main is locked in this test' >&2\n"
            "    exit 1\n"
            "  fi\n"
            "done\n"
            "exit 0\n",
            encoding="utf-8",
            newline="\n",
        )
        subprocess.run([self.bash, "-c", 'chmod +x "$1"', "-", posix(hook)], check=True)
        main_before = self.fixture.remote_sha(self.fixture.origin, "main")
        voice_before = self.fixture.remote_sha(self.fixture.origin, "voice/main")

        completed = self.sync()

        self.assertEqual(completed.returncode, 15, completed.stdout + completed.stderr)
        self.assertIn("push failed", completed.stdout)
        self.assertEqual(self.fixture.remote_sha(self.fixture.origin, "main"), main_before)
        self.assertEqual(self.fixture.remote_sha(self.fixture.origin, "voice/main"), voice_before)

    def test_git_children_cannot_wait_on_a_credential_prompt(self) -> None:
        self.fixture.publish_base()
        self.fixture.publish_voice_branch()
        self.fixture.advance_upstream()
        log = self.fixture.root / "git-env.log"
        hook = self.fixture.root / "bash-env.sh"
        hook.write_text(
            'git() {\n'
            '  printf "%s %s\\n" "${GIT_TERMINAL_PROMPT-unset}" "${GCM_INTERACTIVE-unset}" >>"$VOICE_SYNC_GIT_LOG"\n'
            '  command git "$@"\n'
            '}\n',
            encoding="utf-8",
            newline="\n",
        )

        completed = self.sync("--dry-run", env={"BASH_ENV": posix(hook), "VOICE_SYNC_GIT_LOG": posix(log)})

        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        seen = log.read_text(encoding="utf-8").splitlines()
        self.assertTrue(seen, "no git child ran")
        self.assertEqual(set(seen), {"0 never"})


if __name__ == "__main__":
    unittest.main()
