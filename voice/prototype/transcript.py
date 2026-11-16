"""Turns the transcription session's events into the segmenter's whole-utterance transcript updates.

Partials from llama.cpp's live ASR only ever append (Stage 1, measured), so a correction arrives as more text. Each
update still carries the whole utterance, so the segmenter never has to know that.
"""

from dataclasses import dataclass, field

from voice.segmentation.contract import TranscriptUpdate

DELTA = "conversation.item.input_audio_transcription.delta"
COMPLETED = "conversation.item.input_audio_transcription.completed"


@dataclass
class TranscriptAdapter:
    """Items in arrival order. The live ASR starts a new item after a long pause, and the utterance is all of them."""

    items: dict[str, str] = field(default_factory=dict)
    completed: set[str] = field(default_factory=set)
    revision: int = 0
    input_ended: bool = False
    final_sent: bool = False

    def end_input(self):
        """Call when the client sends the final commit. The next completed item ends the utterance."""
        self.input_ended = True

    def text(self):
        return " ".join(t.strip() for t in self.items.values() if t.strip())

    def feed(self, event):
        """Returns the update this server event produces, or None."""
        kind, item = event.get("type"), event.get("item_id", "")
        if self.final_sent or kind not in (DELTA, COMPLETED):
            return None
        if kind == DELTA:
            if not event.get("delta"):
                return None
            self.items[item] = self.items.get(item, "") + event["delta"]
        else:
            self.items[item] = event.get("transcript") or self.items.get(item, "")
            self.completed.add(item)
        return self._update(self.input_ended and kind == COMPLETED and self.completed >= set(self.items))

    def finish(self):
        """The final update, for when the input ended but an item never completed. None if it was already sent."""
        return None if self.final_sent else self._update(True)

    def _update(self, is_final):
        self.final_sent = is_final
        self.revision += 1
        return TranscriptUpdate(self.revision, self.text(), is_final)
