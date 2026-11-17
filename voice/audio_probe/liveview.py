"""What the live transcript held at each end-of-turn cut: the paced partials that had arrived by the cut's audio time.

The paced run streams a whole turn at real time, so a partial that arrived at time t used only audio up to t. The text
the decider would have seen at cut c is therefore the partials with arrival <= c. Writes transcripts_live.json.
"""
import json

from voice.audio_probe import liquid as L

timing = {t["file"].removeprefix("kind_").removesuffix(".wav"): t for t in json.loads((L.OUT / "asr_timing.json").read_text())}
manifest = [json.loads(l) for l in (L.OUT / "clips" / "manifest.jsonl").read_text().splitlines()]
out, lag = {}, []
for r in manifest:
    if r["set"] != "eot":
        continue
    t = timing[r["src"]]
    out[r["file"]] = "".join(d for at, d in t["deltas"] if at <= r["cut_s"]).strip()
    if r["label"] == 1:
        final = t["text"].split()
        seen = out[r["file"]].split()
        lag.append((r["pause_ms"], len(seen) / max(len(final), 1)))
(L.OUT / "transcripts_live.json").write_text(json.dumps(out, indent=0))
for T in (150, 300, 500, 700):
    v = [f for ms, f in lag if ms == T]
    print(f"T={T} ms: share of the final transcript's words already visible at the cut: mean {sum(v) / len(v):.2f}, complete in {sum(f >= 0.999 for f in v)}/{len(v)}")
