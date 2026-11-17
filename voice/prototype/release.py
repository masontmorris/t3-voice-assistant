"""Releasing a reply's audio while it is generated, never ahead of the screens on its text.

LFM3-Audio answers in blocks: a run of text deltas, then the audio frames that speak them, then the next run of text
(measured: 6 text tokens, then 12 frames of 80 ms, on every reply in the 2026-11-16 replays). The text of a block
arrives before its audio, and the model emits text faster than it speaks it, so the audio of block j says words from
the text runs up to j and no later (measured, docs/voice/stream-release.md).

`Release` is one response's gate. It records where each text run ends and how much audio had arrived by then. When
run m is complete, it screens the text so far, cut back to its last whole word, and if the screens pass it releases
the audio of every block up to m - 1: one block of lookahead, so the words the released audio speaks were screened
with the next block's words after them. A prefix that fails releases nothing more; a later, longer prefix that passes
again may release. At `response.done` the whole text is screened as a finished reply: a pass releases the rest of
the audio, and a fail leaves the reply stopped where it was.
"""

import re
from dataclasses import dataclass, field

LOOKAHEAD_BLOCKS = 1
WHOLE_WORDS = re.compile(r"^.*[^\w'%]", re.DOTALL)


def whole_words(text):
    """`text` up to its last character that cannot be the middle of a word, so a word still arriving is not read.

    "The weather in Ral" reads "The weather in ": a place screen would otherwise see "Ral" as a city nothing gave."""
    match = WHOLE_WORDS.match(text)
    return match.group(0) if match else ""


@dataclass
class Release:
    """One response's audio gate. `passes(text, final)` is the screens; `out(text, pcm)` receives released audio.

    `text` handed to `out` is the screened text the release was justified by, which can run ahead of the words the
    audio itself says."""

    passes: object
    out: object
    text: str = ""
    pcm: bytearray = field(default_factory=bytearray)
    run_ends: list = field(default_factory=list)
    sent: int = 0
    covered: int = 0
    releases: list = field(default_factory=list)
    in_run: bool = False
    passed: bool | None = None

    def on_text(self, delta):
        self.in_run = True
        self.text += delta

    def on_audio(self, chunk, t):
        if self.in_run:
            self.in_run = False
            self.run_ends.append((len(self.text), len(self.pcm)))
            self._advance(t)
        self.pcm += chunk

    def _advance(self, t):
        prefix = whole_words(self.text)
        if not self.passes(prefix, False):
            return
        blocks = [j for j in range(len(self.run_ends) - LOOKAHEAD_BLOCKS)
                  if self.run_ends[j + LOOKAHEAD_BLOCKS - 1][0] <= len(prefix)]
        if not blocks:
            return
        last = blocks[-1]
        self._send(self.run_ends[last + 1][1], prefix, self.run_ends[last][0], t)

    def finish(self, t):
        """The response is done: release the rest if the whole reply passes. Returns whether it passed."""
        self.passed = bool(self.passes(self.text, True))
        if self.passed:
            self._send(len(self.pcm), self.text, len(self.text), t)
        return self.passed

    def _send(self, upto, screened, covered, t):
        if upto <= self.sent:
            return
        self.out(screened, bytes(self.pcm[self.sent:upto]))
        self.releases.append({"t": round(t, 4), "bytes": upto, "screened": screened})
        self.sent, self.covered = upto, covered

    def heard(self):
        """The reply's text the released audio covers: all of it once it passed, otherwise the whole words of the
        text runs whose blocks were released."""
        if self.passed:
            return self.text.strip()
        return whole_words(self.text[:self.covered + 1]).strip() if self.sent else ""
