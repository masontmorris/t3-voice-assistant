"""Re-pin `path:line` citations in a Markdown doc from one commit to another.

For each citation in backticks, read the cited lines at --from and look for the
same lines at --to. Print one row per citation:

  ok       the same lines sit at the same place at --to
  moved    the same lines sit elsewhere at --to; the row gives the new place
  changed  no exact match at --to; the row gives the closest window and its
           similarity, and a person must re-read the claim
  missing  the path does not exist at --to, or the lines are past its end

A citation written `name:path:line` is read at the commit given by
`--at name=<commit>` instead of --from. A citation with no line numbers only
checks that the path exists. With --write, the tool writes a copy of the doc
with every ok, moved, and changed citation replaced by its --to place and
every --at prefix removed.

Usage:
  python voice/tools/check_citations.py DOC --from A --to B [--at head=B] [--write OUT]
"""

import argparse
import difflib
import re
import subprocess
import sys
from functools import cache

SPAN = re.compile(r"`([^`\n]+)`")
CITE = re.compile(r"^(?:(?P<prefix>[a-z]+):)?(?P<path>[\w.\-/]+?)(?::(?P<a>\d+)(?:-(?P<b>\d+))?)?$")
CONT = re.compile(r"^:(?P<a>\d+)(?:-(?P<b>\d+))?$")


def git(*args):
    return subprocess.run(["git", *args], capture_output=True, check=False)


@cache
def object_type(commit, path):
    out = git("cat-file", "-t", f"{commit}:{path}")
    return out.stdout.decode().strip() if out.returncode == 0 else None


@cache
def file_lines(commit, path):
    out = git("show", f"{commit}:{path}")
    return out.stdout.decode("utf-8", errors="replace").replace("\r\n", "\n").split("\n")


def locate(block, lines):
    n = len(block)
    starts = [i for i in range(len(lines) - n + 1) if lines[i : i + n] == block]
    if starts:
        return starts, 1.0
    votes = {}
    for k, line in enumerate(block):
        if len(line.strip()) < 8:
            continue
        for i, other in enumerate(lines):
            if other == line:
                votes[i - k] = votes.get(i - k, 0) + 1
    if not votes:
        return [], 0.0
    start = max(0, min(max(votes, key=votes.get), len(lines) - n))
    ratio = difflib.SequenceMatcher(None, "\n".join(block), "\n".join(lines[start : start + n])).ratio()
    return [start], ratio


def fmt(path, a, b):
    if a is None:
        return path
    return f"{path}:{a}" if a == b else f"{path}:{a}-{b}"


def check(path, a, b, src, dst):
    if object_type(dst, path) is None:
        return "missing", None, ""
    if a is None:
        return "ok", (path, None, None), ""
    if b > len(file_lines(src, path)):
        return "missing", None, f"line {b} is past the end at the source commit"
    block = file_lines(src, path)[a - 1 : b]
    starts, ratio = locate(block, file_lines(dst, path))
    if not starts:
        return "changed", (path, a, b), "no anchor line found"
    if ratio < 1.0:
        s = starts[0] + 1
        return "changed", (path, s, s + b - a), f"closest window {ratio:.2f} similar"
    if a - 1 in starts:
        return "ok", (path, a, b), ""
    s = min(starts, key=lambda i: abs(i - (a - 1))) + 1
    note = f"{len(starts)} exact matches, nearest taken" if len(starts) > 1 else ""
    return "moved", (path, s, s + b - a), note


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("doc")
    ap.add_argument("--from", dest="src", required=True)
    ap.add_argument("--to", dest="dst", required=True)
    ap.add_argument("--at", action="append", default=[])
    ap.add_argument("--write")
    args = ap.parse_args()
    prefixes = dict(item.split("=", 1) for item in args.at)

    text = open(args.doc, encoding="utf-8").read()
    out, pos, last_path, last_src = [], 0, None, args.src
    counts = {}
    for m in SPAN.finditer(text):
        span = m.group(1)
        cont = CONT.match(span)
        cite = None if cont else CITE.match(span)
        if cont and last_path:
            path, src, raw = last_path, last_src, span
            a = int(cont["a"])
        elif cite and (cite["prefix"] is None or cite["prefix"] in prefixes):
            path = cite["path"]
            src = prefixes.get(cite["prefix"], args.src)
            if cite["a"] is None and "/" not in path:
                continue
            if object_type(src, path) is None:
                continue
            raw = span
            a = int(cite["a"]) if cite["a"] else None
            last_path, last_src = path, src
        else:
            continue
        group = cont if cont else cite
        b = int(group["b"]) if group["b"] else a
        status, new, note = check(path, a, b, src, args.dst)
        counts[status] = counts.get(status, 0) + 1
        target = fmt(*new) if new else "-"
        line = text.count("\n", 0, m.start()) + 1
        print(f"{status:8} L{line:<4} {raw}  ->  {target}" + (f"  ({note})" if note else ""))
        if new:
            replacement = (f":{new[1]}" if new[1] == new[2] else f":{new[1]}-{new[2]}") if cont else target
            out.append(text[pos : m.start(1)])
            out.append(replacement)
            pos = m.end(1)
    out.append(text[pos:])
    print("summary: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
    if args.write:
        with open(args.write, "w", encoding="utf-8", newline="\n") as f:
            f.write("".join(out))
    return 1 if counts.get("missing") else 0


if __name__ == "__main__":
    sys.exit(main())
