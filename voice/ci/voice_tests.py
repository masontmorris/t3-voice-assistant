"""Run every voice test suite and fail only on failures the recorded baseline does not cover.

Discovery is by file: every test_*.py under voice/ (so a moved package under voice/research/ still runs), plus the
bot's Node tests in voice/bot/test. A missing optional audio dependency is reported as a skip with its reason, not
counted as a failure.
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

BASELINE = "voice/ci/known_failures.txt"
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


def test_modules(repo: Path) -> list[str]:
    modules = []
    for path in sorted((repo / "voice").rglob("test_*.py")):
        relative = path.relative_to(repo).with_suffix("")
        modules.append(".".join(relative.parts))
    return modules


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
    files = sorted((repo / "voice").rglob("*.test.mjs"))
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
    if not passed or not failed:
        return "0 passed, 1 failed", f"node --test printed no summary, exit {completed.returncode}"
    names = re.findall(r"^\s*not ok \d+ - (.+)$", completed.stdout, re.M)
    return f"{passed.group(1)} passed, {failed.group(1)} failed", "\n".join(names)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run every voice test suite against the recorded baseline.")
    parser.add_argument("--repo", default=None, help="repo root (default: the repo that contains this file)")
    parser.add_argument("--baseline", default=None, help=f"known-failure list, relative to the repo (default {BASELINE})")
    parser.add_argument("--no-baseline", action="store_true", help="treat every failure as a regression")
    args = parser.parse_args(argv)

    repo = Path(args.repo).resolve() if args.repo else repo_root()
    baseline_path = repo / (args.baseline or BASELINE)
    baseline = set() if args.no_baseline else load_baseline(baseline_path)

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
        counters["passed"] += result.testsRun - len(result.errors) - len(result.failures) - len(result.skipped)

    node_result = run_node_suite(repo)
    if node_result is not None:
        summary, note = node_result
        if summary is None:
            skipped.append(("voice/bot (node)", note))
        else:
            counters = group("voice/bot (node)")
            passed, failed = (int(count) for count in re.findall(r"\d+", summary))
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

    elapsed = time.monotonic() - started
    if regressions:
        print(f"voice tests FAILED: {len(regressions)} failure(s) not in the baseline.")
        for name, kind, detail in regressions:
            print(f"  {name} ({kind}) {detail}")
        print(f"{total_run} python tests ran in {elapsed:.1f}s.")
        return EXIT_REGRESSION

    print(f"voice tests OK: {total_run} python tests ran in {elapsed:.1f}s, {len(known)} known failure(s).")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
