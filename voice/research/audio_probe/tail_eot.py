"""End-of-turn on d1.2 with only the last N seconds of each clip, to see whether a short tail keeps accuracy and cuts the
upload. python -m voice.research.audio_probe.tail_eot 3.0
"""
import json
import sys

from voice.research.audio_probe import liquid as L, questions as Q

CLIPS = L.OUT / "clips"


def main(tail_s):
    rows = [json.loads(l) for l in (CLIPS / "manifest.jsonl").read_text().splitlines() if l]
    rows = [r for r in rows if r["set"] == "eot"]
    c = L.Client()
    out = L.OUT / "results" / f"eot_audio_tail{tail_s:g}.jsonl"
    with out.open("w") as f:
        for r in rows:
            x, rate = L.read_wav(CLIPS / r["file"])
            x = x[-int(tail_s * rate):]
            rec = c.call({"audio": [L.wav_b64(x, rate)], "state": None, "questions": Q.EOT}, f"eot-tail{tail_s:g}")
            a = rec["response"]["answers"]["finished"]
            f.write(json.dumps({**r, "mode": "audio_tail", "ms": rec["ms"], "bytes": rec["bytes"],
                                "tokens": rec["usage"]["input_tokens"], "cost": rec["cost"], "probs": a["probabilities"],
                                "choice": a["choice"], "text": None}) + "\n")


if __name__ == "__main__":
    main(float(sys.argv[1]))
