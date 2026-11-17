"""Transcripts for the barge-in set's text baseline.

Real turn openings get the causal live view: the paced partials that had arrived by onset + window. Every other clip is a
short standalone recording with no paced run, so it gets its final ASR transcript, which is not causal and flatters text.
"""
import json

from voice.research.audio_probe import liquid as L


def build():
    final = json.loads((L.OUT / "transcripts_asr.json").read_text())
    timing = {t["file"]: t for t in json.loads((L.OUT / "asr_timing.json").read_text())}
    out = {}
    for line in (L.OUT / "clips" / "manifest.jsonl").read_text().splitlines():
        r = json.loads(line)
        if r["set"] != "bc":
            continue
        t = timing.get(f"kind_{r['src']}.wav")
        if r["kind"] in ("real_turn_opening", "real_backchannel") and t:
            cut = t["onset_s"] + r["window_s"]
            out[r["file"]] = "".join(d for at, d in t["deltas"] if at <= cut).strip()
        else:
            out[r["file"]] = final.get(r["file"], "")
    return out
