"""Prompt variants for the end-of-turn question on d1.2 audio, scored on the T=300 and T=500 clips."""
import json
import sys

from voice.audio_probe import liquid as L, stats

CLIPS = L.OUT / "clips"
V = {
    "v2_noul_complete": {"q": {"type": "noul", "instructions": "Is the last sentence the speaker said complete, so that they are done talking?",
                               "criteria": {"true": "The speaker finished a complete sentence and is done talking.",
                                            "false": "The speaker stopped in the middle of a sentence or thought, or is clearly about to continue."}}},
    "v3_choice_ends": {"q": {"type": "choice", "instructions": "Listen to how the speech ends.",
                             "criteria": {"done": "It ends on a falling, final tone, a finished sentence.",
                                          "continuing": "It ends on an unfinished, rising or hanging tone, a word that needs more after it, or a filler such as uhm."}}},
    "v4_state_hint": {"q": {"type": "choice", "instructions": "Did the speaker finish?",
                            "criteria": {"done": "Finished.", "continuing": "Not finished; will keep talking."}},
                      "state": {"context": "A voice assistant listens to a person. The person is about to be answered unless they are still mid-thought."}},
    "v5_task_text": {"q": {"type": "choice", "instructions": "Is what the speaker said so far a complete message that can be answered, or cut off before the point?",
                           "criteria": {"done": "Complete message that can be answered now.",
                                        "continuing": "Cut off, trailing, or still setting up the point."}}},
}


def run(name, subset):
    spec = V[name]
    c = L.Client()
    scores, labels, rows = [], [], []
    for r in subset:
        x, rate = L.read_wav(CLIPS / r["file"])
        body = {"audio": [L.wav_b64(x, rate)], "state": spec.get("state"),
                "questions": {"q": spec["q"]}}
        rec = c.call(body, f"eotv-{name}")
        a = rec["response"]["answers"]["q"]
        s = a["noul"] if a["type"] == "noul" else a["probabilities"]["done"]
        scores.append(s), labels.append(r["label"])
        rows.append({"file": r["file"], "label": r["label"], "score": s, "ms": rec["ms"]})
    return scores, labels, rows


if __name__ == "__main__":
    man = [json.loads(l) for l in (CLIPS / "manifest.jsonl").read_text().splitlines() if l]
    eot = [r for r in man if r["set"] == "eot" and r["pause_ms"] in (300, 500)]
    for name in (sys.argv[1:] or V):
        s, y, rows = run(name, eot)
        (L.OUT / "results" / f"eotv_{name}.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
        print(name, "n", len(y), "auc", round(stats.auc(s, y), 3), "pos med", round(sorted(a for a, b in zip(s, y) if b)[len(y) // 3], 2))
