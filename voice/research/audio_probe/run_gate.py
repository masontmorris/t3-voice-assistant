"""The 70 live-gate utterances as synthetic speech: kind from audio, ASR text and true text, clean and noisy.

python -m voice.research.audio_probe.run_gate <hosted|PORT NAME>
Each row is one (item, condition, mode). Modes: audio (clip + prior turns as text), asr (LFM3 transcript of the clip),
oracle (the true utterance, clean text). Conditions: clean, noisy (white noise, 5 dB SNR).
"""
import json
import sys
from collections import Counter

from voice.research.audio_probe import liquid as L, questions as Q
from voice.research.audio_probe.run_api import Local

TTS = L.OUT / "tts"
RESULTS = L.OUT / "results"
MAP = {"dispatch": "request", "follow_up": "request", "correction": "correction", "conversation": "conversation"}


def fold(c):
    return {"request": "request", "correction": "correction"}.get(c, "conversation")


def run(backend, tag):
    manifest = [json.loads(l) for l in (TTS / "manifest.jsonl").read_text().splitlines()]
    asr = json.loads((L.OUT / "transcripts_asr.json").read_text())
    client = Local(backend[0]) if backend else L.Client()
    out = RESULTS / f"gate_{tag}.jsonl"
    done = {(json.loads(l)["file"], json.loads(l)["mode"]) for l in out.read_text().splitlines()} if out.exists() else set()
    with out.open("a") as f:
        for m in manifest:
            prior = m["context"].get("turns", [])
            for mode in ("audio", "asr", "oracle"):
                if mode == "oracle" and m["cond"] == "noisy":
                    continue
                if (m["file"], mode) in done:
                    continue
                text = asr.get(m["file"]) if mode == "asr" else m["text"] if mode == "oracle" else None
                if mode != "audio" and not text:
                    text = ""
                body = {"questions": Q.kind(text if mode != "audio" else None)}
                state = {"previous_turns": prior} if prior else {}
                if mode == "audio":
                    x, r = L.read_wav(TTS / m["file"])
                    body["audio"] = [L.wav_b64(x, r)]
                else:
                    state["transcript"] = text
                body["state"] = state or None
                rec = client.call(body, f"gate-{mode}")
                a = rec["response"]["answers"]["s1_kind"] if rec["status"] == 200 else None
                f.write(json.dumps({"file": m["file"], "id": m["id"], "cond": m["cond"], "mode": mode,
                                    "expect": m["expect"], "gold": MAP[m["expect"]], "category": m["category"],
                                    "ms": rec["ms"], "text": text, "choice": a and a["choice"],
                                    "probs": a and a["probabilities"]}) + "\n")
                f.flush()


def report(tag):
    rows = [json.loads(l) for l in (RESULTS / f"gate_{tag}.jsonl").read_text().splitlines()]
    for cond, mode in (("clean", "oracle"), ("clean", "asr"), ("clean", "audio"), ("noisy", "asr"), ("noisy", "audio")):
        sub = [r for r in rows if r["cond"] == cond and r["mode"] == mode and r["choice"]]
        if not sub:
            continue
        acc = sum(fold(r["choice"]) == r["gold"] for r in sub) / len(sub)
        req = [(r["gold"] == "request", r["probs"]["request"]) for r in sub]
        from voice.research.audio_probe import stats
        auc = stats.auc([s for _, s in req], [int(y) for y, _ in req])
        print(f"gate {tag} {cond}/{mode}: n={len(sub)} 3-way acc={acc:.3f} request AUC={auc:.3f}",
              dict(Counter((r["gold"], fold(r["choice"])) for r in sub if fold(r["choice"]) != r["gold"])))


if __name__ == "__main__":
    if sys.argv[1] == "report":
        report(sys.argv[2])
    elif sys.argv[1] == "hosted":
        run(None, "hosted")
    else:
        run((int(sys.argv[1]), sys.argv[2]), sys.argv[2])
