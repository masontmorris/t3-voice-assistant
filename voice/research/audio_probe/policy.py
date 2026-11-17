"""End-of-turn policy replay: check P(done) at 150, 300, 500 ms of silence, fire at the first check at or above theta,
else fire at 700 ms (the shipped silence rule). Score on the clips: reply time saved on finished turns, and mid-thought
pauses cut (a fire before the speaker resumed). The baseline cuts a pause only if it lasts 700 ms or more.
"""
import json
import sys
from collections import defaultdict

from voice.research.audio_probe import liquid as L

CHECKS = (150, 300, 500)


def replay(path, theta):
    rows = [json.loads(l) for l in open(path)]
    groups = defaultdict(dict)
    for r in rows:
        key = (r["src"], r["pause_kind"] == "turn_end", r["file"].split("_")[2] if r["pause_kind"] == "mid" else "end")
        groups[key][r["pause_ms"]] = r
    saved, cut, turns, pauses, base_cut = [], 0, 0, 0, 0
    for (src, is_end, _), by in groups.items():
        fired = next((t for t in CHECKS if t in by and by[t]["probs"]["done"] >= theta), None)
        if is_end:
            turns += 1
            saved.append(700 - fired if fired else 0)
        else:
            pauses += 1
            cut += fired is not None
            base_cut += max(by) >= 700
    return {"theta": theta, "turns": turns, "mean_saved_ms": sum(saved) / turns,
            "fired_early": sum(s > 0 for s in saved), "pauses": pauses, "pauses_cut_by_policy": cut,
            "pauses_cut_by_700ms_rule": base_cut}


if __name__ == "__main__":
    for name in sys.argv[1:]:
        print(name)
        for th in (0.5, 0.6, 0.7, 0.8, 0.9):
            print(" ", replay(L.OUT / "results" / name, th))
