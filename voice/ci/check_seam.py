"""Fail when the fork changes an upstream file that docs/voice/seam.md does not list.

Compares the head commit against the merge base with the mirror branch, ignores everything under voice/ and
docs/voice/, and requires a row with a reason in the allowlist for each remaining path.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

OWNED_PREFIXES = ("voice/", "docs/voice/")
DEFAULT_BASE = "origin/main"
DEFAULT_ALLOWLIST = "docs/voice/seam.md"

EXIT_OK = 0
EXIT_UNLISTED = 1
EXIT_SETUP = 2


class GitError(RuntimeError):
    pass


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0:
        raise GitError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout


def repo_root() -> Path:
    return Path(git(Path.cwd(), "rev-parse", "--show-toplevel").strip())


def is_owned(path: str) -> bool:
    return any(path.startswith(prefix) for prefix in OWNED_PREFIXES)


def changed_files(repo: Path, base: str, head: str) -> list[str]:
    merge_base = git(repo, "merge-base", base, head).strip()
    # --no-renames reports a rename as its old and new paths, so both count as touched.
    listing = git(repo, "diff", "--name-only", "--no-renames", "-z", merge_base, head)
    return [path for path in listing.split("\0") if path]


def read_allowlist(path: Path) -> dict[str, str]:
    entries: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) < 2:
            continue
        listed = cells[0].strip("` ")
        reason = cells[1].strip("` ")
        if not listed or listed.lower() == "path" or set(listed) <= set("-: "):
            continue
        entries[listed] = reason
    return entries


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fail when the fork changes an upstream file the seam allowlist does not cover."
    )
    parser.add_argument("--base", default=DEFAULT_BASE, help=f"mirror branch to compare against (default {DEFAULT_BASE})")
    parser.add_argument("--head", default="HEAD", help="commit to check (default HEAD)")
    parser.add_argument("--allowlist", default=DEFAULT_ALLOWLIST, help="allowlist path, relative to the repo root")
    parser.add_argument("--repo", default=None, help="repo root (default: the repo that contains this directory)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        repo = Path(args.repo).resolve() if args.repo else repo_root()
        allowlist_path = repo / args.allowlist
        if not allowlist_path.is_file():
            print(f"seam check ERROR: no allowlist at {args.allowlist}.", file=sys.stderr)
            return EXIT_SETUP
        touched = sorted(path for path in changed_files(repo, args.base, args.head) if not is_owned(path))
    except GitError as error:
        print(f"seam check ERROR: {error}", file=sys.stderr)
        return EXIT_SETUP

    allowed = read_allowlist(allowlist_path)
    unlisted = [path for path in touched if not allowed.get(path)]
    bare = [path for path in unlisted if path in allowed]

    if not unlisted:
        if not touched:
            print("seam check OK: no upstream files changed outside voice/ and docs/voice/.")
        else:
            print(f"seam check OK: {len(touched)} upstream file(s) changed, each listed in {args.allowlist}.")
            for path in touched:
                print(f"  {path}  {allowed[path]}")
        return EXIT_OK

    print(f"seam check FAILED: {len(unlisted)} upstream file(s) outside voice/ and docs/voice/ are not covered.")
    for path in unlisted:
        suffix = "  (listed with no reason)" if path in bare else ""
        print(f"  {path}{suffix}")
    print(f"Add each path to the table in {args.allowlist} with a one-line reason, or keep the change under voice/.")
    return EXIT_UNLISTED


if __name__ == "__main__":
    sys.exit(main())
