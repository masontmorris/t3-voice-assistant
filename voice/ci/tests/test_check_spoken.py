"""The spoken-literal check passes the T3 speaking path as it is and fails on each kind of planted line."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from voice.ci.check_spoken import SPEAKING, literal_lines, scan

REPO = Path(__file__).resolve().parents[3]

PLANTS = {
    "a module constant": ("voice/t3/lifecycle.py", "LOST = act(Say.LOST_TRACK)",
                          'LOST = act(Say.LOST_TRACK)\nHELD_LINE = "It\'s still working."'),
    "an f-string": ("voice/live/t3_tasks.py", "def which_project(candidates",
                    'def started(title):\n    return f"{title} finished."\n\n\ndef which_project(candidates'),
    "wording in a slot": ("voice/live/t3_tasks.py", 'act(Say.CANCELLED, op="start")',
                          'act(Say.CANCELLED, op="start", thread="Okay, I won\'t start it")'),
    "a line handed to the speaker": ("voice/prototype/run.py", "since, rendered.line, log,",
                                     'since, "It finished.", log,'),
    "a row's line": ("voice/t3/lifecycle.py", "Ends.GIVE_UP, act(Say.LAUNCH_UNCONFIRMED)),",
                     'Ends.GIVE_UP, "I couldn\'t confirm it started."),'),
}


class CheckSpoken(unittest.TestCase):
    def test_the_speaking_path_holds_no_spoken_literal(self):
        self.assertEqual(scan(REPO), [])

    def test_each_planted_line_is_caught(self):
        for label, (path, anchor, planted) in PLANTS.items():
            with self.subTest(label), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                for name in SPEAKING:
                    (root / name).parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy(REPO / name, root / name)
                source = (root / path).read_text(encoding="utf-8")
                self.assertIn(anchor, source, "the plant's anchor moved")
                (root / path).write_text(source.replace(anchor, planted, 1), encoding="utf-8")
                found = scan(root)
                self.assertEqual({f.path for f in found}, {path}, found)

    def test_no_literal_reaches_a_speaking_entry_point_anywhere_in_voice(self):
        self.assertEqual(literal_lines(REPO), [])

    def test_a_literal_planted_at_any_entry_point_is_caught(self):
        plants = {"say_verbatim": 'await say_verbatim(session, 24_000, 0.0, "It finished.", log)',
                  "speak_t3": 'await self.speak_t3(turn, f"{title} is done.", pcm)',
                  "send_audio": 'await send_audio(("It", "finished."), pcm)',
                  "send_verbatim": 'send_verbatim(line="It finished.", pcm=b"")'}
        for name, call in plants.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / "voice/tools/tests").mkdir(parents=True)
                source = f"async def go(session, log, self, turn, pcm, title):\n    {call}\n"
                (root / "voice/tools/helper.py").write_text(source, encoding="utf-8")
                (root / "voice/tools/tests/test_helper.py").write_text(source, encoding="utf-8")
                found = literal_lines(root)
                self.assertEqual([(f.path, f.why) for f in found],
                                 [("voice/tools/helper.py", f"a literal handed to {name}")])

    def test_text_that_is_not_spoken_is_left_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "voice").mkdir()
            (root / "voice/sample.py").write_text(
                '"""A docstring. It reads as prose."""\n'
                'import argparse\n'
                'def go(log):\n'
                '    log("t3_words", note="This is a log line.")\n'
                '    raise ValueError("That was not allowed.")\n'
                'ASKS = ("How is it going?",)\n'
                'PATTERN = r"\\b(?:Stop the thread)"\n'
                'ROWS = {"ask": Row("Twin B asks about a thread", 1)}\n'
                'argparse.ArgumentParser().add_argument("--x", help="Says what x does.")\n',
                encoding="utf-8")
            sample = {"voice/sample.py": None}
            self.assertEqual(scan(root, sample), [])


if __name__ == "__main__":
    unittest.main()
