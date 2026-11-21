"""Fail when a T3 speaking path holds a string literal that reads as something the voice would say.

The voice speaks about T3 work only from typed acts that its text model words (voice/t3/acts.py). This check reads
the files and functions on that path and reports every literal that looks like speech: two or more words that end
in sentence punctuation or start with a capital, including the literal parts of an f-string. It also reports an act
built with a literal slot value that has a space in it, since a slot carries a fact, never wording.

What it skips, because none of it is spoken: docstrings, log and print text, exception messages, assert messages,
argparse text, the lifecycle table's row descriptions, the decider's tool descriptions, and the CLI drivers whose
literals are what the person says to the voice.

The T3 speaking entry points take only a RenderedLine and raise on a plain string (voice/t3/speech.py). As a second
net, the check also reads every module under voice/, tests aside, for a string literal handed to one of them.
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path

# File, and the functions in it that speak. None means the whole module.
SPEAKING = {
    "voice/live/t3_tasks.py": None,
    "voice/t3/lifecycle.py": None,
    "voice/t3/acts.py": None,
    "voice/prototype/run.py": ("say_acts", "say_t3"),
    "voice/live/server.py": ("run_idle", "speak_t3"),
}
# Top-level names whose literals are not the voice's: the decider's tool text and the person's side of the CLI.
NOT_SPOKEN_NAMES = frozenset({"T3_TOOLS", "WAITING_ASKS", "DESCRIBE_REQUEST", "WAITING_NOW", "ASKS", "drive", "smoke", "main", "transcript"})
NOT_SPOKEN_CALLS = frozenset({"log", "print", "add_argument", "add_parser", "ArgumentParser", "warn"})
# A lifecycle row's first argument says when the row applies; what the row says comes after it.
DESCRIBED_CALLS = frozenset({"Row"})
ACT_BUILDERS = frozenset({"act", "Act", "with_slots"})

# The calls that put a T3 line on the speaker.
ENTRY_POINTS = frozenset({"relay_draft", "speak_t3", "send_audio", "send_checked"})

TWO_WORDS = re.compile(r"[A-Za-z][A-Za-z']*[,;:]?\s+[A-Za-z']+")
NOT_PROSE = re.compile(r"\\[a-zA-Z]|\(\?|\[\^|\{\d")


def reads_as_speech(text: str) -> bool:
    text = text.strip()
    if not TWO_WORDS.search(text) or NOT_PROSE.search(text):
        return False
    return text[-1] in ".?!" or text[0].isupper()


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    text: str
    why: str


def _call_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _literal_text(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(part.value if isinstance(part, ast.Constant) else "x" for part in node.values)
    return None


class Scan(ast.NodeVisitor):
    def __init__(self, path: str, functions: tuple[str, ...] | None):
        self.path, self.functions = path, functions
        self.findings: list[Finding] = []
        self.depth_in_scope = 0 if functions else 1
        self.skipping = 0

    def _skip(self, node: ast.AST) -> None:
        self.skipping += 1
        self.generic_visit(node)
        self.skipping -= 1

    def _scope(self, node, name: str) -> None:
        if self.functions is None and self.depth_in_scope == 1 and name in NOT_SPOKEN_NAMES:
            return
        entering = self.functions is not None and name in self.functions
        self.depth_in_scope += entering
        self._docstring(node)
        self.generic_visit(node)
        self.depth_in_scope -= entering

    def _docstring(self, node) -> None:
        body = getattr(node, "body", None)
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            body[0].value.docstring = True

    def visit_Module(self, node: ast.Module) -> None:
        self._docstring(node)
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._scope(node, node.name)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._scope(node, node.name)

    def visit_Assign(self, node: ast.Assign) -> None:
        names = {t.id for t in node.targets if isinstance(t, ast.Name)}
        if self.functions is None and names & NOT_SPOKEN_NAMES:
            return
        self.generic_visit(node)

    def visit_Raise(self, node: ast.Raise) -> None:
        self._skip(node)

    def visit_Assert(self, node: ast.Assert) -> None:
        self._skip(node)

    def visit_Call(self, node: ast.Call) -> None:
        name = _call_name(node)
        if name in NOT_SPOKEN_CALLS:
            return self._skip(node)
        if name in DESCRIBED_CALLS and node.args:
            self.skipping += 1
            self.visit(node.args[0])
            self.skipping -= 1
            for child in [*node.args[1:], *node.keywords]:
                self.visit(child)
            return None
        if name in ACT_BUILDERS and self.depth_in_scope and not self.skipping:
            for keyword in node.keywords:
                text = _literal_text(keyword.value)
                if text is not None and " " in text.strip():
                    self.findings.append(Finding(self.path, keyword.value.lineno, text,
                                                 f"literal wording in the {keyword.arg} slot of an act"))
        self.generic_visit(node)

    def visit_JoinedStr(self, node: ast.JoinedStr) -> None:
        self._check(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str) and not getattr(node, "docstring", False):
            self._check(node)

    def _check(self, node) -> None:
        if not self.depth_in_scope or self.skipping:
            return
        text = _literal_text(node)
        if text is not None and reads_as_speech(text):
            self.findings.append(Finding(self.path, node.lineno, text, "a literal that reads as speech"))


def scan(repo: Path, speaking=None) -> list[Finding]:
    findings = []
    for path, functions in (speaking or SPEAKING).items():
        source = (repo / path).read_text(encoding="utf-8")
        visitor = Scan(path, functions)
        visitor.visit(ast.parse(source, filename=path))
        findings.extend(visitor.findings)
    return findings


def _holds_literal(node: ast.AST) -> bool:
    if _literal_text(node) is not None:
        return True
    return isinstance(node, (ast.Tuple, ast.List)) and any(_holds_literal(item) for item in node.elts)


def is_test(path: str) -> bool:
    return "/tests/" in path or path.rsplit("/", 1)[-1].startswith("test_")


def literal_lines(repo: Path) -> list[Finding]:
    """Every string literal handed to a T3 speaking entry point anywhere under voice/, tests aside."""
    findings = []
    for file in sorted((repo / "voice").rglob("*.py")):
        path = file.relative_to(repo).as_posix()
        if is_test(path):
            continue
        for node in ast.walk(ast.parse(file.read_text(encoding="utf-8"), filename=path)):
            if isinstance(node, ast.Call) and _call_name(node) in ENTRY_POINTS:
                for arg in [*node.args, *(keyword.value for keyword in node.keywords)]:
                    if _holds_literal(arg):
                        findings.append(Finding(path, arg.lineno, ast.unparse(arg),
                                                f"a literal handed to {_call_name(node)}"))
    return findings


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[2])
    args = parser.parse_args(argv)
    findings = scan(args.repo) + literal_lines(args.repo)
    for finding in findings:
        print(f"{finding.path}:{finding.line}: {finding.why}: {finding.text!r}")
    if findings:
        print(f"check_spoken: {len(findings)} spoken string literal(s) on the T3 speaking path", file=sys.stderr)
        return 1
    print(f"check_spoken: no spoken string literal in {len(SPEAKING)} T3 speaking files, and none handed to a T3 "
          "speaking entry point under voice/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
