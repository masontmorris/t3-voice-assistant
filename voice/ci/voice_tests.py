"""Run every voice test suite and fail on failures or skips the recorded baselines do not cover.

Discovery is by file: every test_*.py under voice/ (so a moved package under voice/research/ still runs), plus the
bot's Node tests in voice/bot/test. A module that cannot import an optional audio dependency is reported as a skip
with its reason. Failures are checked against voice/ci/known_failures.txt and skips against voice/ci/known_skips.txt,
so a new skip fails the gate instead of hiding a regression.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib
import io
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import unittest
from pathlib import Path

OPTIONAL_MODULES = {
    "sounddevice": "sounddevice is not installed (live audio I/O)",
    "soundfile": "soundfile is not installed (audio files)",
}

ENV_SKIPS = {
    "voice.bot.test_root_tasks": (
        "VOICE_PROTOTYPE_CHECKOUT",
        "needs VOICE_PROTOTYPE_CHECKOUT, a separate prototype checkout",
    ),
}

IGNORED_DIRECTORIES = {"node_modules", ".venv", "__pycache__"}

BASELINE = "voice/ci/known_failures.txt"
SKIP_BASELINE = "voice/ci/known_skips.txt"
EXIT_OK = 0
EXIT_REGRESSION = 1
EXIT_SETUP = 2


def repo_root() -> Path:
    here = Path(__file__).resolve().parent
    result = subprocess.run(
        ["git", "-C", str(here), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0:
        raise SystemExit("voice_tests.py: this copy is not inside a git worktree")
    return Path(result.stdout.strip())


def head_summary(repo: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), "log", "-1", "--format=%h %s"],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout.strip() or "unknown"


def discovered(repo: Path, pattern: str) -> list[Path]:
    paths = []
    for path in sorted((repo / "voice").rglob(pattern)):
        if IGNORED_DIRECTORIES.intersection(path.relative_to(repo).parts):
            continue
        paths.append(path)
    return paths


def test_modules(repo: Path) -> list[str]:
    return [".".join(path.relative_to(repo).with_suffix("").parts) for path in discovered(repo, "test_*.py")]


def node_test_files(repo: Path) -> list[Path]:
    return discovered(repo, "*.test.mjs")


def missing_optional_module(error: BaseException) -> str | None:
    if isinstance(error, ModuleNotFoundError):
        root = (error.name or "").split(".")[0]
        if root in OPTIONAL_MODULES:
            return root
    return None


def optional_module_in_traceback(traceback: str) -> str | None:
    for name in re.findall(r"No module named '([A-Za-z0-9_.]+)'", traceback):
        if name in OPTIONAL_MODULES:
            return name
    return None


def last_line(text: str) -> str:
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    return lines[-1] if lines else ""


def load_baseline(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    known = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        entry = line.split("#", 1)[0].strip()
        if entry:
            known.add(entry)
    return known


def run_node_suite(repo: Path) -> tuple[str, str] | None:
    files = node_test_files(repo)
    if not files:
        return None
    node = shutil.which("node")
    if not node:
        return None, "node is not on PATH"
    completed = subprocess.run(
        [node, "--test", "--test-reporter=tap", *[str(path) for path in files]],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=900,
    )
    passed = re.search(r"^# pass (\d+)$", completed.stdout, re.M)
    failed = re.search(r"^# fail (\d+)$", completed.stdout, re.M)
    node_skipped = re.search(r"^# skipped (\d+)$", completed.stdout, re.M)
    cancelled = re.search(r"^# cancelled (\d+)$", completed.stdout, re.M)
    if not passed or not failed or not node_skipped:
        return "0 passed, 1 failed, 0 skipped", f"node --test printed no summary, exit {completed.returncode}"
    cancelled_count = int(cancelled.group(1)) if cancelled else 0
    problems = re.findall(r"^\s*not ok \d+ - (.+)$", completed.stdout, re.M)
    if cancelled_count:
        problems.append(f"{cancelled_count} test(s) cancelled, a hook threw before they ran")
    if completed.returncode != 0 and not problems:
        problems.append(f"node --test exited {completed.returncode}")
    if problems:
        failed_count = int(failed.group(1)) + cancelled_count
        if failed_count == 0:
            failed_count = 1
        return f"{passed.group(1)} passed, {failed_count} failed, {node_skipped.group(1)} skipped", "\n".join(problems)
    return f"{passed.group(1)} passed, 0 failed, {node_skipped.group(1)} skipped", ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run every voice test suite against the recorded baseline.")
    parser.add_argument("--repo", default=None, help="repo root (default: the repo that contains this file)")
    parser.add_argument("--baseline", default=None, help=f"known-failure list, relative to the repo (default {BASELINE})")
    parser.add_argument("--no-baseline", action="store_true", help="treat every failure as a regression")
    parser.add_argument("--skip-baseline", default=None, help=f"expected skips, relative to the repo (default {SKIP_BASELINE})")
    parser.add_argument("--no-skip-baseline", action="store_true", help="treat every skip as a regression")
    args = parser.parse_args(argv)

    repo = Path(args.repo).resolve() if args.repo else repo_root()
    baseline_path = repo / (args.baseline or BASELINE)
    baseline = set() if args.no_baseline else load_baseline(baseline_path)
    skip_baseline_path = repo / (args.skip_baseline or SKIP_BASELINE)
    skip_baseline = set() if args.no_skip_baseline else load_baseline(skip_baseline_path)

    print(f"voice tests: {sys.executable} (Python {platform.python_version()}) on {platform.platform()}")
    print(f"repo: {repo}")
    print(f"head: {head_summary(repo)}")
    print()

    started = time.monotonic()
    groups: dict[str, dict[str, int]] = {}
    failures: list[tuple[str, str, str]] = []
    skipped: list[tuple[str, str]] = []
    total_run = 0

    def group(name: str) -> dict[str, int]:
        return groups.setdefault(name, {"passed": 0, "failed": 0, "skipped": 0})

    for module_name in test_modules(repo):
        group_name = module_name.rsplit(".", 1)[0]
        env_skip = ENV_SKIPS.get(module_name)
        if env_skip and not os.environ.get(env_skip[0]):
            skipped.append((module_name, env_skip[1]))
            group(group_name)["skipped"] += 1
            continue
        try:
            module = importlib.import_module(module_name)
        except Exception as error:
            optional = missing_optional_module(error)
            if optional:
                skipped.append((module_name, OPTIONAL_MODULES[optional]))
                group(group_name)["skipped"] += 1
            else:
                failures.append((module_name, "import", f"{type(error).__name__}: {error}"))
                group(group_name)["failed"] += 1
            continue

        suite = unittest.defaultTestLoader.loadTestsFromModule(module)
        noise = io.StringIO()
        with contextlib.redirect_stdout(noise), contextlib.redirect_stderr(noise):
            result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(suite)
        total_run += result.testsRun
        counters = group(group_name)
        for test, traceback in result.errors:
            optional = optional_module_in_traceback(traceback)
            if optional:
                skipped.append((test.id(), OPTIONAL_MODULES[optional]))
                counters["skipped"] += 1
            else:
                failures.append((test.id(), "error", last_line(traceback)))
                counters["failed"] += 1
        for test, traceback in result.failures:
            optional = optional_module_in_traceback(traceback)
            if optional:
                skipped.append((test.id(), OPTIONAL_MODULES[optional]))
                counters["skipped"] += 1
            else:
                failures.append((test.id(), "fail", last_line(traceback)))
                counters["failed"] += 1
        for test, reason in result.skipped:
            skipped.append((test.id(), reason))
            counters["skipped"] += 1
        failed_ids = {test.id() for test, _ in result.errors} | {test.id() for test, _ in result.failures}
        counters["passed"] += max(0, result.testsRun - len(failed_ids) - len(result.skipped))

    node_result = run_node_suite(repo)
    if node_result is not None:
        summary, note = node_result
        if summary is None:
            skipped.append(("voice/bot (node)", note))
        else:
            counters = group("voice/bot (node)")
            passed, failed, node_skip_count = (int(count) for count in re.findall(r"\d+", summary))
            counters["skipped"] = node_skip_count
            skipped.extend((f"node: skip {i}", "Node test skipped") for i in range(1, node_skip_count + 1))
            counters["passed"], counters["failed"] = passed, failed
            if failed:
                names = [name for name in note.splitlines() if name] or ["voice/bot (node)"]
                for name in names:
                    failures.append((f"node: {name}", "node", summary))

    for name in sorted(groups):
        counters = groups[name]
        print(
            f"{name}: {counters['passed']} passed, {counters['failed']} failed, "
            f"{counters['skipped']} skipped"
        )
    print()

    if skipped:
        print(f"skipped ({len(skipped)}), each with its reason:")
        for name, reason in skipped:
            print(f"  {name}: {reason}")
        print()

    failing_ids = {name for name, _, _ in failures}
    known = [item for item in failures if item[0] in baseline]
    regressions = [item for item in failures if item[0] not in baseline]

    if known:
        print(f"known failures from {baseline_path.relative_to(repo).as_posix()} ({len(known)}):")
        for name, kind, detail in known:
            print(f"  {name} ({kind}) {detail}")
        print()

    stale = sorted(baseline - failing_ids)
    if stale:
        print("baseline entries that now pass, delete them:")
        for name in stale:
            print(f"  {name}")
        print()

    current_skips = {name for name, _ in skipped}
    new_skips = sorted(current_skips - skip_baseline)
    stale_skips = sorted(skip_baseline - current_skips)
    if new_skips:
        print(f"skips not in {skip_baseline_path.relative_to(repo).as_posix()} ({len(new_skips)}):")
        for name in new_skips:
            print(f"  {name}")
        print("Fix the cause, or add the id with its reason to that file if the skip is expected.")
        print()
    if stale_skips:
        print("skip baseline entries that no longer skip, delete them:")
        for name in stale_skips:
            print(f"  {name}")
        print()

    elapsed = time.monotonic() - started
    if regressions or new_skips:
        print(
            f"voice tests FAILED: {len(regressions)} failure(s) not in the baseline, "
            f"{len(new_skips)} skip(s) not in the skip baseline."
        )
        for name, kind, detail in regressions:
            print(f"  {name} ({kind}) {detail}")
        for name in new_skips:
            print(f"  skipped: {name}")
        print(f"{total_run} python tests ran in {elapsed:.1f}s.")
        return EXIT_REGRESSION

    print(f"voice tests OK: {total_run} python tests ran in {elapsed:.1f}s, {len(known)} known failure(s).")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
