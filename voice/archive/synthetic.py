"""Build the synthetic example archive: a scripted three-turn run with synthetic audio, through the real exporter.

    python -m voice.archive.synthetic [--out voice/archive/example]

No real voice is involved. Speech is a buzz at a speaking pitch, shaped into one burst per word, so the waveform
looks like talk and the validator's cross-correlation has something unique to lock onto. The script writes a server
run and a client session the way the live system lays them out, then calls export(). It covers every event type the
server can log today: partials, decision points with a split and a merge, gate verdicts that pass and hold, the
hand-off, LFM's tool calls, tool results, task status, two kinds of reply and a barge-in.
"""

import argparse
import json
import tempfile
from pathlib import Path

import numpy as np

from voice.archive.audio import place, write_wav
from voice.archive.export import export

RATE = 8_000
ORIGIN_UNIX = 1_794_831_000.0
CREATED_AT = "2026-11-16T12:00:00-05:00"


def speech(text, pitch, seed, rate=RATE):
    """One burst per word, with a short gap between words, at a speaking pitch with some jitter."""
    rng = np.random.default_rng(seed)
    parts = []
    for word in text.split():
        length = 0.12 + 0.045 * len(word.strip(",.?!"))
        t = np.arange(int(length * rate)) / rate
        f0 = pitch * (1 + 0.08 * np.sin(2 * np.pi * rng.uniform(2, 5) * t) + rng.uniform(-0.05, 0.05))
        phase = 2 * np.pi * np.cumsum(f0) / rate
        voice = sum(np.sin(k * phase) / k for k in range(1, 6)) + 0.15 * rng.standard_normal(t.size)
        envelope = np.sin(np.pi * t / length) ** 0.6
        parts += [0.25 * voice * envelope, np.zeros(int(rng.uniform(0.05, 0.11) * rate))]
    return np.concatenate(parts).astype(np.float32)


def noise(seconds, seed, rate=RATE):
    return (0.002 * np.random.default_rng(seed).standard_normal(int(seconds * rate))).astype(np.float32)


TURNS = [
    {"gap": 0.6, "text": "What's the weather in Boston, and set a timer for ten minutes.",
     "partials": ["What's the", "What's the weather in Boston,", "What's the weather in Boston, and set a timer",
                  "What's the weather in Boston, and set a timer for ten minutes."],
     "decisions": [
         (1, ["What's the weather in Boston,"], [("s1", "request", "ready", False, 0.82)]),
         (2, ["What's the weather in Boston,", "and set a timer"],
          [("s1", "request", "ready", False, 0.86), ("s2", "request", "wait", True, 0.48)]),
     ],
     "tasks": [("task_1", "What's the weather in Boston", 1, 0.82, 0.40, True, "dispatched"),
               ("task_2", "set a timer for ten minutes", 3, 0.61, 0.80, False, "held")],
     "tools": [("weather", {"place": "Boston"}, {"ok": True, "summary": "Boston: 41°F, light rain until 6 pm"},
                "task_1", 1.9)],
     "replies": [("snapshot", "Checking Boston now. Want me to start that ten-minute timer?", 0.45),
                 ("update", "Boston is 41 degrees with light rain until six.", 2.4)]},
    {"gap": 0.9, "text": "Yes, but make it fifteen.",
     "partials": ["Yes,", "Yes, but make it", "Yes, but make it fifteen."],
     "decisions": [
         (1, ["Yes,", "but make it"], [("s1", "confirm", "ready", False, 0.71), ("s2", "correction", "wait", True,
                                                                               0.55)]),
         (2, ["Yes, but make it fifteen."], [("s1", "correction", "ready", True, 0.88)]),
     ],
     "tasks": [("task_1", "set a timer for fifteen minutes", 2, 0.88, 0.80, True, "dispatched")],
     "tools": [("set_timer", {"minutes": 15}, {"ok": True, "summary": "Timer set for 15 minutes"}, "task_1", 0.6)],
     "replies": [("snapshot", "Done. Fifteen minutes, starting now. Anything else while it runs?", 0.35)],
     "barged_after": 1.6},
    {"gap": None, "text": "No, thanks!", "partials": ["No,", "No, thanks!"], "decisions": [],
     "tasks": [], "tools": [], "replies": [("snapshot", "Anytime.", 0.3)]},
]


def build(work):
    work = Path(work)
    server, client = work / "server", work / "client"
    server.mkdir(parents=True)
    client.mkdir()
    mic_tracks, play_tracks, log = [], [], []
    now, barge_at = 0.0, None
    for n, spec in enumerate(TURNS, 1):
        spec = {**spec, "start": barge_at - 0.4 if barge_at is not None else now + spec["gap"]}
        barge_at = None
        clip = np.concatenate([noise(0.3, n), speech(spec["text"], 125, n), noise(0.7, 100 + n)])
        audio_s = clip.size / RATE
        write_wav(server / f"turn_{n:03d}_input.wav", clip, RATE)
        mic_tracks.append((clip, RATE, spec["start"]))
        end = spec["start"] + audio_s
        shift = spec["start"] - 0.2
        log.append({"t": ORIGIN_UNIX + end + 0.004, "kind": "turn_end", "type": "turn_end", "turn": n,
                    "trailing_ms": 700, "forced": False})
        timeline, history_tasks = [], []
        step = (audio_s - 1.0) / len(spec["partials"])
        for r, text in enumerate(spec["partials"], 1):
            timeline.append({"t": 0.2 + 0.3 + r * step, "kind": "transcript_update", "revision": r, "text": text,
                             "is_final": False})
        final_r = len(spec["partials"]) + 1
        timeline.append({"t": audio_s + 0.25, "kind": "transcript_update", "revision": final_r,
                         "text": spec["text"], "is_final": True})
        timeline.append({"t": audio_s + 0.2, "kind": "audio_end", "audio_s": round(audio_s, 3)})
        for r, segments, decisions in spec["decisions"]:
            timeline.append({"t": 0.2 + 0.3 + r * step + 0.3, "kind": "decision_point", "source": "update",
                             "revision": r, "is_final": False, "calls": [{"ms": 240.0}], "segments": segments,
                             "decisions": {sid: {"sid": sid, "kind": kind, "readiness": ready, "side_effect": side,
                                                 "confidence": {"kind": conf, "ready": min(1.0, conf + 0.08)}}
                                           for sid, kind, ready, side, conf in decisions}})
        close = audio_s + 0.27
        segments = spec["decisions"][-1][1] if spec["decisions"] else [spec["text"]]
        reply_cursor = close
        snapshot = {"type": "utterance_snapshot", "final_transcript": spec["text"],
                    "tasks": [{"task_id": t[0], "description": t[1], "status": "running" if t[5] else "waiting"}
                              for t in spec["tasks"]], "unreviewed_speech": ""}
        timeline.append({"t": close, "kind": "close", "revision": final_r, "unreviewed_speech": "",
                         "segments": segments})
        timeline.append({"t": close, "kind": "snapshot", "snapshot": snapshot})
        lfm_t = close + 0.3
        timeline.append({"t": lfm_t, "kind": "tool_turn", "text": "",
                         "calls": [{"name": name, "arguments": json.dumps(args)}
                                   for name, args, *_ in spec["tools"]]})
        for task_id, text, revision, score, threshold, passed, status in spec["tasks"]:
            history = [{"revision": revision, "is_final": False, "event": "created", "sources": ["orchestrator"],
                        "segment_text": text},
                       {"revision": revision, "is_final": False, "event": "gate", "passed": passed, "score": score,
                        "weakest": "kind", "threshold": threshold},
                       {"revision": revision, "is_final": False, "event": "status", "old": "proposed", "new": status,
                        "reason": f"gate: kind confidence {score:.2f} {'reached' if passed else 'fell short of'} "
                                  f"{threshold:.2f}"}]
            history_tasks.append({"task_id": task_id, "history": history})
        for name, args, result, task_id, after in spec["tools"]:
            timeline.append({"t": lfm_t + 0.01, "kind": "service", "task_id": task_id, "event": "running"})
            timeline.append({"t": lfm_t + after, "kind": "tool_call", "name": name, "arguments": args,
                             "result": result})
            timeline.append({"t": lfm_t + after, "kind": "task_updates", "updates": [
                {"task_id": task_id, "status": "completed", "result_summary": result["summary"]}]})
        for kind, text, after in spec["replies"]:
            reply = speech(text, 210, sum(map(ord, text)))
            name = "reply" if kind == "snapshot" else "update"
            write_wav(server / f"turn_{n:03d}_{name}.wav", reply, RATE)
            ready = close if kind == "snapshot" else lfm_t + spec["tools"][0][4]
            done = max(ready, reply_cursor) + after
            heard = shift + done
            played = reply
            if spec.get("barged_after"):
                barge_at = heard + spec["barged_after"]
                played = reply[:int(spec["barged_after"] * RATE)]
                log.append({"t": ORIGIN_UNIX + barge_at, "kind": "barge_in", "type": "barge_in", "turn": n})
            reply_cursor = done + played.size / RATE
            now = heard + played.size / RATE
            play_tracks.append((played, RATE, heard))
            timeline.append({"t": done + reply.size / RATE, "kind": "reply", "reply": kind, "text": text})
            log.append({"t": ORIGIN_UNIX + heard - 0.03, "kind": "reply_start", "turn": n, "rate": RATE})
            log.append({"t": ORIGIN_UNIX + heard + 0.4, "kind": "reply_end", "turn": n,
                        "first_audio_to_playback_s": 0.03, "underruns": 0})
        timeline.sort(key=lambda e: e["t"])
        record = {"clip": f"turn_{n:03d}", "audio_end": audio_s + 0.2, "timeline": timeline, "tasks": history_tasks,
                  "live": {"carried_audio_s": 0.0, "endpointer": {"preroll_ms": 300}}}
        (server / f"turn_{n:03d}.json").write_text(json.dumps(record, indent=1), encoding="utf-8")

    total = max(offset + samples.size / RATE for samples, _, offset in mic_tracks + play_tracks) + 0.8
    mic = place(mic_tracks, RATE, total)
    mic += 0.002 * np.random.default_rng(7).standard_normal(mic.size).astype(np.float32)
    playback = place(play_tracks, RATE, total)
    write_wav(client / "microphone.wav", mic, RATE)
    write_wav(client / "assistant-playback-001.wav", playback, RATE)
    write_wav(client / "conversation.wav", mic + playback, RATE)
    frames = mic.size
    manifest = {"schema": "voice.live.recording/1", "origin": {"monotonic_s": 0.0, "unix_s": ORIGIN_UNIX},
                "mic_rate": RATE, "input_source": "synthetic",
                "conversation": {"file": "conversation.wav", "rate": RATE, "frames": frames,
                                 "duration_s": round(frames / RATE, 3)},
                "tracks": [{"kind": "microphone", "file": "microphone.wav", "rate": RATE, "frames": frames,
                            "offset_s": 0.0, "duration_s": round(frames / RATE, 3)},
                           {"kind": "assistant_playback", "file": "assistant-playback-001.wav", "rate": RATE,
                            "frames": frames, "offset_s": 0.0, "duration_s": round(frames / RATE, 3)}]}
    (client / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    log.sort(key=lambda e: e["t"])
    (client / "client.jsonl").write_text("".join(json.dumps(e) + "\n" for e in log), encoding="utf-8")
    return server, client


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=Path(__file__).parent / "example")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as tmp:
        server, client = build(tmp)
        out = export(server, client, args.out, created_at=CREATED_AT)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
