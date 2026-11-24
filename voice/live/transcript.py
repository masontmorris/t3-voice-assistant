"""The transcript of a call opened on a T3 thread, kept as the call goes, for recording on that thread at its end.

The caller's side is each turn's final transcript. The voice's side is the text of the reply pieces the server released
to the client with their audio, so a reply muted before it went out is not in it. A reply the caller cut off keeps the
text that was sent and is marked `interrupted`. With `--trim-unheard on`, a reply whose unheard part was taken out of
the voice session keeps only the words the server counts as heard (`server.hearing`: where the client says playback
stopped, placed in the reply's own words), still marked `interrupted`. A checked T3 line carries its act and the thread
events it rests on, from the update the line was spoken for.
Text only; nothing here holds audio, and nothing here is logged.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Line:
    speaker: str
    text: str
    turn: int
    relay: bool = False
    interrupted: bool = False
    acts: tuple[str, ...] = ()
    act: str | None = None
    sequences: tuple[int, ...] = ()

    def wire(self) -> dict:
        line = {"speaker": self.speaker, "text": self.text.strip(), "relay": self.relay}
        if self.interrupted:
            line["interrupted"] = True
        if self.act is not None:
            line["act"], line["sequences"] = self.act, list(self.sequences)
        return line


@dataclass
class CallTranscript:
    lines: list[Line] = field(default_factory=list)
    _replies: dict[int, Line] = field(default_factory=dict)

    def heard(self, turn: int, text: str) -> None:
        if text.strip():
            self.lines.append(Line("caller", text, turn))

    def said(self, reply: object, turn: int, text: str, *, relay: bool = False, acts: tuple[str, ...] = ()) -> None:
        """A piece of `reply` went out. Pieces of one reply are one line; each reply is its own line."""
        line = self._replies.get(id(reply))
        if line is None:
            line = self._replies[id(reply)] = Line("voice", "", turn, relay=relay, acts=acts)
            self.lines.append(line)
        line.text += text

    def interrupted(self, turn: int) -> None:
        """The caller spoke over the turn's reply, or a correction replaced it: its last line was not all heard."""
        line = next((line for line in reversed(self.lines) if line.turn == turn), None)
        if line is not None and line.speaker == "voice":
            line.interrupted = True

    def cut(self, reply: object, text: str) -> None:
        """The unheard part of `reply` was taken out of the call: its line keeps only `text`, what the caller heard,
        and is marked cut off. A line left with no text is not recorded."""
        line = self._replies.get(id(reply))
        if line is not None:
            line.text, line.interrupted = text, True

    def spoken(self, acts: tuple[str, ...], sequences: tuple[int, ...]) -> None:
        """A checked T3 line was heard: the newest relay line spoken for those acts gets its claim."""
        line = next((line for line in reversed(self.lines)
                     if line.relay and line.act is None and line.acts == acts), None)
        if line is not None and acts:
            line.act, line.sequences = acts[0], tuple(dict.fromkeys(sequences))

    def wire(self) -> list[dict]:
        return [line.wire() for line in self.lines if line.text.strip()]
