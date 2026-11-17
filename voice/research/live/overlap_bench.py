"""Replays recorded turns as speech over a playing reply, through the live endpointer and the overlap gate.

    python -m voice.research.live.overlap_bench CALL_DIR [CALL_DIR ...] --out DIR

Each CALL_DIR is a live server record directory: turn_NNN.json beside turn_NNN_input.wav. Every turn that has a final
transcript is played as if it started while a reply was audible, and the gate's verdict and its time from speech onset
are written out. The old rule, a barge-in on the endpointer's start, is scored beside it. A turn whose transcript
opens with a backchannel word and a comma ("Okay, and what about...") also gives a clip of that word alone, cut at the
first pause, written under DIR/clips, so the set holds real backchannels as well as real turns.

The transcript stage cannot run here: an overlap that ends unconfirmed is classified on the record's final transcript
(or the cut clip's first word) and its time is the endpointer's end, before the transcription's own time.
"""

import argparse
import json
import statistics
from pathlib import Path

import numpy as np
import soundfile as sf

from voice.live.endpoint import FRAME_BYTES, FRAME_MS, RATE, Audio, End, Endpointer, Settings, Start, frame_dbfs
from voice.live.overlap import Overlap, OverlapSettings, Verdict, classify, is_backchannel, words

LEAD_S = 0.5
PAUSE_MS = 200


def final_transcript(record):
    return next((e["text"] for e in record.get("timeline", [])
                 if e.get("kind") == "transcript_update" and e.get("is_final")), "")


def gate(pcm, transcript, settings=Settings(), overlap_settings=OverlapSettings()):
    """The verdict on `pcm` heard over a reply: how it was reached and when, in ms from the speech onset."""
    endpointer, overlap, onset = Endpointer(settings), None, None
    pcm = b"\x00\x00" * int(LEAD_S * RATE) + pcm + b"\x00\x00" * RATE
    for index in range(len(pcm) // FRAME_BYTES):
        frame = pcm[index * FRAME_BYTES:(index + 1) * FRAME_BYTES]
        if onset is None and endpointer.noise_dbfs is not None and frame_dbfs(frame) > endpointer.threshold():
            onset = index
        for event in endpointer.push(frame):
            now_ms = (index - (onset or index)) * FRAME_MS
            if isinstance(event, Start):
                overlap, start_ms = Overlap(overlap_settings, bytearray(event.pcm), 0), now_ms
            elif isinstance(event, Audio) and overlap.feed(event.pcm, event.level_dbfs, endpointer.noise_dbfs):
                return {"verdict": str(Verdict.BARGE_IN), "how": "voiced", "ms": now_ms, "old_ms": start_ms}
            elif isinstance(event, End):
                if overlap.voiced_ms < overlap_settings.min_voiced_ms:
                    return {"verdict": str(Verdict.NOISE), "how": "too short", "ms": now_ms, "old_ms": start_ms}
                return {"verdict": str(classify(transcript, "")), "how": "transcript", "ms": now_ms,
                        "old_ms": start_ms, "voiced_ms": overlap.voiced_ms}
    return {"verdict": "none", "how": "no start", "ms": None, "old_ms": None}


def first_word_clip(pcm, transcript):
    """The opening backchannel word alone, cut at its first pause, or None when the turn does not open with one."""
    head = (transcript or "").split(",", 1)[0]
    if "," not in (transcript or "") or len(words(head)) != 1 or not is_backchannel(head):
        return None, None
    samples = np.frombuffer(pcm, dtype="<i2")
    levels = [frame_dbfs(samples[i:i + FRAME_BYTES // 2].tobytes())
              for i in range(0, len(samples) - FRAME_BYTES // 2, FRAME_BYTES // 2)]
    floor = min(levels[:5])
    voiced = [level > max(-60.0, floor + 10) for level in levels]
    if True not in voiced:
        return None, None
    first, gap, end = voiced.index(True), 0, None
    for index in range(first, len(voiced)):
        gap = 0 if voiced[index] else gap + 1
        if gap * FRAME_MS >= PAUSE_MS:
            end = index - gap + 1
            break
    if end is None or (end - first) * FRAME_MS > 1000:
        return None, None
    stop = min(len(samples), (end + 5) * FRAME_BYTES // 2)
    return samples[:stop].tobytes(), head.strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("calls", type=Path, nargs="+")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--confirm-ms", type=int, default=OverlapSettings.confirm_ms)
    args = parser.parse_args()
    (args.out / "clips").mkdir(parents=True, exist_ok=True)
    overlap_settings = OverlapSettings(confirm_ms=args.confirm_ms)
    rows = []
    for call in args.calls:
        for path in sorted(call.glob("turn_*.json")):
            record = json.loads(path.read_text(encoding="utf-8"))
            text = final_transcript(record)
            wav = path.with_name(f"{path.stem}_input.wav")
            if record.get("cancelled") or not text or not wav.is_file():
                continue
            samples, rate = sf.read(str(wav), dtype="int16")
            assert rate == RATE, wav
            pcm = samples.tobytes()
            expect = "dismiss" if is_backchannel(text) else "barge_in"
            rows.append({"clip": f"{call.name}/{path.stem}", "kind": "turn", "transcript": text, "expect": expect,
                         **gate(pcm, text, overlap_settings=overlap_settings)})
            clip, head = first_word_clip(pcm, text)
            if clip is not None:
                name = f"{call.name}_{path.stem}_{words(head)[0]}.wav"
                sf.write(str(args.out / "clips" / name), np.frombuffer(clip, dtype="<i2"), RATE)
                rows.append({"clip": f"clips/{name}", "kind": "first word", "transcript": head, "expect": "dismiss",
                             **gate(clip, head, overlap_settings=overlap_settings)})
    for row in rows:
        row["new_ok"] = (row["verdict"] == "barge_in") == (row["expect"] == "barge_in")
        row["old_ok"] = row["expect"] == "barge_in" and row["old_ms"] is not None
    summary = {}
    for expect in ("barge_in", "dismiss"):
        group = [r for r in rows if r["expect"] == expect]
        voiced = [r["ms"] for r in group if r["how"] == "voiced"]
        summary[expect] = {
            "clips": len(group), "new_right": sum(r["new_ok"] for r in group),
            "old_right": sum(r["old_ok"] for r in group),
            "by_voice": len(voiced), "by_transcript": sum(r["how"] == "transcript" for r in group),
            "voiced_ms_median": statistics.median(voiced) if voiced else None,
            "voiced_ms_max": max(voiced) if voiced else None,
            "old_ms_median": statistics.median(r["old_ms"] for r in group if r["old_ms"] is not None)
            if group else None}
    (args.out / "overlap_bench.json").write_text(json.dumps({"summary": summary, "rows": rows}, indent=1) + "\n",
                                                 encoding="utf-8")
    for row in rows:
        print(f"{row['clip']:<52} {row['expect']:<9} {row['verdict']:<12} {row['how']:<11} "
              f"{row['ms']!s:>5} ms (old {row['old_ms']!s:>4})  {row['transcript'][:44]!r}")
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
