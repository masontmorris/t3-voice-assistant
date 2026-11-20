"""The recording of a call made through the browser page: both sides on one timeline, as the person heard them.

The server knows what it sent, but not what the page played: the page buffers reply audio, ducks it, and drops what is
still queued on a barge-in. So the page reports what its output worklet actually played, as `played {t, pcm}` text
frames on the call's own socket, and the server writes it with the microphone frames it received. Nothing leaves the
connection the call already uses, and the files go under the server's records folder.

The timeline is the microphone stream: sample i of the 16 kHz stream the server received is at i / 16000 s. The page
maps each played block's position on its audio clock into that stream (`micIndexAt` in app.js), so both tracks stay
aligned however long the call runs and whatever the network did to the frames. `played.pcm` is base64 PCM16 mono at
16 kHz, the page's output after ducking.

A hangup sends `bye` after the last `played`. A call that ends without it, by a dropped connection, a closed tab or the
server stopping, is finalized from what arrived and marked partial in the manifest.
"""

import base64
import binascii
from pathlib import Path

from voice.live.recording import MIC_RATE, SessionRecorder

PLAYED_RATE = MIC_RATE
# The page plays only audio it received, so a block cannot start much later than the microphone frames it has sent.
MAX_AHEAD_S = 5.0
NOTES = ("Microphone is the PCM16 the server received from the page, including frames sent while PTT is up. "
         "Playback is the PCM16 the page's output worklet played, at 16 kHz and after ducking, stamped on the "
         "microphone stream's timeline; a reply the person cut off ends where it was cut. The timeline's zero is the "
         "first microphone frame received, and origin.unix_s is when the server opened the recording. "
         "A partial recording may lack the last moments the page played.")


def call_dir(out):
    """A fresh `call-NNN` folder under the records folder. A reconnect in the same server run gets the next one."""
    out = Path(out)
    for n in range(1, 1000):
        path = out / f"call-{n:03d}"
        try:
            path.mkdir()
            return path
        except FileExistsError:
            continue
    raise RuntimeError(f"no free call folder under {out}")


class BrowserRecording:
    """One call's recorder, fed from the connection's event loop. `mic` and `played` only enqueue."""

    def __init__(self, root, *, mode, first_turn):
        self.recorder = SessionRecorder(root, mode=mode, input_source="browser", server="live", origin=0.0,
                                        notes=NOTES)
        self.first_turn = first_turn
        self.mic_frames = 0
        self.hangup = False
        self.rejected = 0

    def mic(self, pcm):
        self.recorder.mic(pcm, self.mic_frames / MIC_RATE)
        self.mic_frames += len(pcm) // 2

    def played(self, message):
        try:
            t = float(message["t"])
            pcm = base64.b64decode(message["pcm"], validate=True)
        except (KeyError, TypeError, ValueError, binascii.Error):
            self.rejected += 1
            return
        if not 0 <= t <= self.mic_frames / MIC_RATE + MAX_AHEAD_S:
            self.rejected += 1
            return
        self.recorder.playback(pcm[:len(pcm) // 2 * 2], PLAYED_RATE, t)

    def finish(self, last_turn):
        """Close the recording. Without a `bye` the page may have played audio it never reported, so it is partial."""
        return self.recorder.finalize(
            end="hangup" if self.hangup else "connection_closed", partial=not self.hangup,
            extra={"turns": {"first": self.first_turn, "last": last_turn},
                   "played_messages_rejected": self.rejected})
