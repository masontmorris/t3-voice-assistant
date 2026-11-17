"""Tables from asr_tick_bench.py JSON files: tick, first partial, word error rate against the script, drift from the
baseline, revisions in the merged live text, voice-session cost.

Usage: python asr_tick_report.py baseline.json variant.json [variant.json ...]
The first file is the drift baseline.
"""

import json
import statistics
import sys
from pathlib import Path

from asr_tick_bench import bursts, common_prefix, edit_distance, wer, words


def q(values, p):
    values = sorted(values)
    return values[int(p * (len(values) - 1))] if values else float("nan")


def merged_timeline(sessions):
    """Burst start times across sessions during speech, each with the text that session had then."""
    events = []
    for session in sessions:
        text = ""
        for burst in bursts(session["deltas"]):
            text += "".join(d for _, d in burst)
            events.append((burst[0][0], burst[-1][0], text.strip()))
    return sorted(events)


def summarize(path, base):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = data["results"]
    gaps, firsts, final_after, rewrites, steps, final_wer_ref, drift, session_wer = [], [], [], 0, 0, [], [], []
    ref_words = ref_err = 0
    lag_after_burst_start = []
    for name, row in rows.items():
        sessions = row["sessions"]
        audio_end = min(s["audio_end"] for s in sessions)
        events = [e for e in merged_timeline(sessions) if e[0] < audio_end and e[2]]
        for a, b in zip(events, events[1:]):
            gaps.append(b[0] - a[0])
        if events:
            firsts.append(events[0][0])
        prev = []
        for _, _, text in events:
            cur = words(text)
            steps += 1
            if prev and common_prefix(prev, cur) < len(prev):
                rewrites += 1
            prev = cur
        final_after.append(max(s["final_t"] for s in sessions) - audio_end)
        for s in sessions:
            session_wer.append(wer(base["results"][name]["sessions"][0]["final_text"], s["final_text"]))
            if row["ref"]:
                ref = words(row["ref"])
                ref_words += len(ref)
                ref_err += edit_distance(ref, words(s["final_text"]))
    voice = [r["voice"] for r in rows.values() if r.get("voice")]
    return {
        "file": Path(path).stem,
        "gap_med": statistics.median(gaps), "gap_p90": q(gaps, .9), "n_gaps": len(gaps),
        "first_med": statistics.median(firsts), "final_after_med": statistics.median(final_after),
        "wer_script": ref_err / max(1, ref_words), "drift_vs_base": statistics.mean(session_wer),
        "rewrite_rate": rewrites / max(1, steps), "gpu_util": data["gpu_util_mean"],
        "voice_first_audio": statistics.median(v["first_audio_after_end_s"] for v in voice) if voice else None,
        "voice_rtf": statistics.median(v["reply_rtf"] for v in voice if v["reply_rtf"]) if voice else None,
    }


if __name__ == "__main__":
    base = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    rows = [summarize(p, base) for p in sys.argv[1:]]
    keys = list(rows[0])
    print("\t".join(keys))
    for r in rows:
        print("\t".join(f"{r[k]:.3f}" if isinstance(r[k], float) else str(r[k]) for k in keys))
