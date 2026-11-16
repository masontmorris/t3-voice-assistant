"""Turn one recorded live run into a loop archive (see schema.py).

    python -m voice.archive.export --server SERVER_RUN_DIR --client CLIENT_SESSION_DIR [--out ARCHIVE_DIR]

The server writes one record per turn, each on its own clock. The client recording is the replay clock. The exporter
places each turn by finding the end of the turn's input WAV inside the client's microphone stem, then shifts the
turn's events by that offset. Replies are placed where their WAV sounds in the playback stem, searched near the time
the client logged. The default output is $VOICE_HOME/archives/<server run name>, outside git, because archives hold
real voices.

Live hooks, for later: this reads records after the fact because voice/live/server.py is changing on other branches.
Once those land, the server can stamp each MicTurn with its replay-clock start and the client can log reply playback
start, which removes the correlation search.
"""

import argparse
import json
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

import numpy as np

from voice.archive.audio import PEAK_FPS, duration_s, files_under, locate, peaks, place, read_wav, sha256
from voice.archive.schema import MATCH_SCORE, SCHEMA

TAIL_S = 1.0
VOICED = 1e-3
REPLY_FILES = {"snapshot": "reply", "update": "update"}
QUEUE_S = 12.0
"""How long a reply can wait in the client's queue behind the one still playing."""


def export(server_dir, client_dir, out_dir, created_at=None):
    server_dir, client_dir, out_dir = Path(server_dir), Path(client_dir), Path(out_dir)
    client_manifest = json.loads((client_dir / "manifest.json").read_text(encoding="utf-8"))
    origin = client_manifest["origin"]["unix_s"]
    log = [json.loads(line) for line in (client_dir / "client.jsonl").read_text(encoding="utf-8").splitlines()
           if line.strip()]
    for entry in log:
        entry["replay_t"] = entry["t"] - origin
    records = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(server_dir.glob("turn_*.json"))]
    mic, mic_rate = read_wav(client_dir / "microphone.wav")
    stems = _stems(client_manifest)
    playback = [(read_wav(client_dir / s["source"]), s["offset_s"]) for s in stems if s["role"] == "assistant_playback"]

    events, turns, replies, warnings = [], [], [], []
    for record in records:
        n = int(record["clip"].split("_")[1])
        turn_log = [e for e in log if e.get("turn") == n]
        placed = _place_turn(record, n, server_dir, turn_log, mic, mic_rate)
        turns.append(placed["row"])
        if placed["warning"]:
            warnings.append(placed["warning"])
            continue
        events += _turn_events(record, n, placed)
        heard, rows, notes = _replies(record, n, server_dir, turn_log, log, playback)
        events += heard
        replies += rows
        warnings += notes
    events += [_event(e["replay_t"], "barge_in", e["turn"], "client") for e in log if e["kind"] == "barge_in"]

    events.sort(key=lambda e: (e["t"], e["_order"]))
    for seq, event in enumerate(events):
        del event["_order"]
        event["seq"] = seq
    if out_dir.exists():
        shutil.rmtree(out_dir)
    (out_dir / "stems").mkdir(parents=True)
    (out_dir / "clips").mkdir()
    for stem in stems:
        shutil.copy2(client_dir / stem["source"], out_dir / stem["file"])
    for wav in sorted(server_dir.glob("turn_*.wav")):
        shutil.copy2(wav, out_dir / "clips" / wav.name)
    for event in events:
        if "audio" in event:
            event["audio"]["clip"] = "clips/" + event["audio"]["clip"]
    duration = client_manifest["conversation"]["duration_s"]
    write_archive(out_dir, events, _waveform(mic, mic_rate, playback, duration), {
        "created_at": created_at,
        "run": server_dir.name,
        "duration_s": duration,
        "clock": "seconds from the client recording's first frame",
        "sources": {"server": server_dir.name, "client": client_dir.name, "client_schema": client_manifest["schema"],
                    "input_source": client_manifest.get("input_source")},
        "stems": [{k: v for k, v in s.items() if k != "source"} for s in stems],
        "turns": turns,
        "replies": replies,
        "warnings": warnings,
    })
    return out_dir


def write_archive(out_dir, events, waveform, manifest):
    """Write timeline.jsonl, waveform.json and manifest.json, hashing every other file. Shared with synthetic.py."""
    out_dir = Path(out_dir)
    with open(out_dir / "timeline.jsonl", "w", encoding="utf-8", newline="\n") as f:
        for event in events:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    (out_dir / "waveform.json").write_text(json.dumps(waveform) + "\n", encoding="utf-8", newline="\n")
    counts = {}
    for event in events:
        counts[event["type"]] = counts.get(event["type"], 0) + 1
    files = {p.relative_to(out_dir).as_posix(): sha256(p) for p in files_under(out_dir) if p.name != "manifest.json"}
    full = {"schema": SCHEMA, "created_at": manifest.pop("created_at", None) or
            datetime.now().astimezone().isoformat(timespec="seconds"), **manifest, "counts": counts, "files": files}
    (out_dir / "manifest.json").write_text(json.dumps(full, indent=1, ensure_ascii=False) + "\n", encoding="utf-8",
                                           newline="\n")
    return full


def _stems(client_manifest):
    stems = []
    for track in client_manifest["tracks"]:
        role = "microphone" if track["kind"] == "microphone" else "assistant_playback"
        stems.append({"role": role, "file": f"stems/{track['file']}", "source": track["file"], "rate": track["rate"],
                      "offset_s": track["offset_s"], "duration_s": track["duration_s"]})
    conversation = client_manifest["conversation"]
    stems.append({"role": "conversation", "file": f"stems/{conversation['file']}", "source": conversation["file"],
                  "rate": conversation["rate"], "offset_s": 0.0, "duration_s": conversation["duration_s"]})
    return stems


def _place_turn(record, n, server_dir, turn_log, mic, mic_rate):
    """Find where the turn's input ended on the replay clock, and the offset from the turn's own clock."""
    row = {"turn": n, "cancelled": record.get("cancelled"), "offset_s": None, "input_end_s": None, "score": None,
           "turn_end_log_ms": None}
    ended = next((e for e in turn_log if e["kind"] == "turn_end"), None)
    audio_end = next((e["t"] for e in record.get("timeline", []) if e["kind"] == "audio_end"), None)
    wav = server_dir / f"{record['clip']}_input.wav"
    if ended is None or audio_end is None or not wav.is_file():
        return {"row": row, "warning": f"turn {n}: no turn_end in the client log or no audio_end in the record"}
    clip, rate = read_wav(wav)
    voiced = np.flatnonzero(np.abs(clip) > VOICED)
    stop = int(voiced[-1]) + 1 if voiced.size else clip.size
    start = max(0, stop - int(TAIL_S * rate))
    found, score = locate(clip[start:stop], mic, mic_rate, ended["replay_t"] - clip.size / rate - 1.0,
                          ended["replay_t"] + 0.5)
    row["score"] = round(score, 3)
    if found is None or score < MATCH_SCORE:
        return {"row": row, "warning": f"turn {n}: input audio not found in the microphone stem (score {score:.2f})"}
    input_end = found + (clip.size - start) / rate
    carried = record.get("live", {}).get("carried_audio_s", 0.0) or 0.0
    row.update(offset_s=round(input_end - audio_end, 4), input_end_s=round(input_end, 4),
               turn_end_log_ms=round((ended["replay_t"] - input_end) * 1000, 1))
    return {"row": row, "warning": None, "offset": input_end - audio_end, "input_end": input_end,
            "input_start": input_end - (clip.size / rate - carried), "carried": carried, "clip": wav.name,
            "trailing_ms": ended.get("trailing_ms", 0), "forced": ended.get("forced", False),
            "preroll_s": record.get("live", {}).get("endpointer", {}).get("preroll_ms", 0) / 1000}


_ORDER = {"speech_start": 0, "transcript_partial": 1, "transcript_final": 2, "speech_end": 3, "turn_end": 4,
          "decision_point": 5, "task_split": 6, "task_merged": 6, "handoff": 7, "lfm_action": 8, "tool_call": 9,
          "task_created": 10, "gate_verdict": 11, "task_status": 12, "reply_start": 13, "reply_end": 14,
          "barge_in": 15, "backlog_reprompt": 16}


def _event(t, kind, turn, src, **payload):
    return {"t": round(max(0.0, t), 4), "type": kind, "turn": turn, "src": src, **payload, "_order": _ORDER[kind]}


def _turn_events(record, n, placed):
    """The turn's server-side events, moved onto the replay clock."""
    shift = placed["offset"]
    timeline = record.get("timeline", [])
    events = [
        _event(placed["input_start"] + placed["preroll_s"], "speech_start", n, "derived",
               audio={"clip": placed["clip"], "clip_offset_s": placed["carried"], "stem": "stems/microphone.wav",
                      "at": round(placed["input_start"], 4)}),
        _event(placed["input_end"] - placed["trailing_ms"] / 1000, "speech_end", n, "derived"),
        _event(placed["input_end"], "turn_end", n, "client", trailing_ms=placed["trailing_ms"],
               forced=placed["forced"]),
    ]
    revision_t, segments = {}, None
    snapshots = {e["kind"]: e["snapshot"] for e in timeline if e["kind"] in ("snapshot", "snapshot_after_tools")}
    tasks = {}

    def task_id(raw):
        return f"{n}/{raw}"

    def status(t, raw, value, src="server", **extra):
        key = (raw, value)
        if key in tasks:
            tasks[key].update({k: v for k, v in extra.items() if v is not None})
            return
        tasks[key] = _event(t, "task_status", n, src, task_id=task_id(raw), status=value,
                            **{k: v for k, v in extra.items() if v is not None})
        events.append(tasks[key])

    created, delegated = {}, {}

    def create(t, raw, text, sources):
        """The earliest sighting wins: the service, LFM's delegate call and the task history can each see it first."""
        known = created.get(raw)
        if known is None:
            created[raw] = _event(t, "task_created", n, "server", task_id=task_id(raw), text=text, sources=sources)
            events.append(created[raw])
        elif round(t, 4) < known["t"]:
            known.update(t=round(max(0.0, t), 4), text=text or known["text"], sources=sources)

    for e in timeline:
        t = shift + e["t"]
        kind = e["kind"]
        if kind == "transcript_update":
            revision_t.setdefault((e["revision"], e["is_final"]), t)
            events.append(_event(t, "transcript_final" if e["is_final"] else "transcript_partial", n, "server",
                                 revision=e["revision"], text=e["text"]))
        elif kind in ("decision_point", "close"):
            revision_t[(e["revision"], e.get("is_final", True), kind)] = t
            if segments is not None and len(e["segments"]) != len(segments):
                split = len(e["segments"]) > len(segments)
                events.append(_event(t, "task_split" if split else "task_merged", n, "derived", before=segments,
                                     after=e["segments"]))
            segments = e["segments"]
            if kind == "close":
                snapshot = snapshots.get("snapshot") or {}
                events.append(_event(t, "handoff", n, "server", final_transcript=snapshot.get("final_transcript"),
                                     unreviewed_speech=e.get("unreviewed_speech"), segments=e["segments"],
                                     snapshot=snapshot))
            else:
                calls = e.get("calls") or []
                events.append(_event(t, "decision_point", n, "server", revision=e["revision"],
                                     is_final=e["is_final"], source=e.get("source"), segments=e["segments"],
                                     decisions=list(e.get("decisions", {}).values()),
                                     latency_ms=round(sum(c.get("ms") or 0 for c in calls), 1)))
        elif kind == "tool_turn":
            calls = [{"name": c.get("name"), "arguments": _json(c.get("arguments"))} for c in e.get("calls", [])]
            events.append(_event(t, "lfm_action", n, "server", calls=calls, text=e.get("text", ""),
                                 snapshot_after=snapshots.get("snapshot_after_tools")))
        elif kind == "tool_call":
            events.append(_event(t, "tool_call", n, "server", name=e["name"], arguments=e.get("arguments"),
                                 result=e.get("result")))
            result = e.get("result") or {}
            if e["name"] == "delegate" and result.get("task_id"):
                delegated.setdefault(result["task_id"], t)
                create(t, result["task_id"], (e.get("arguments") or {}).get("instruction"), ["lfm"])
        elif kind == "service":
            create(t, e["task_id"], e.get("instruction"), ["service"])
            status(t, e["task_id"], e["event"])
        elif kind == "task_updates":
            for update in e["updates"]:
                status(t, update["task_id"], update["status"], result_summary=update.get("result_summary"))

    def at_revision(entry):
        key = (entry["revision"], entry["is_final"])
        for kind in ("decision_point", "close"):
            if (*key, kind) in revision_t:
                return revision_t[(*key, kind)]
        return revision_t.get(key, placed["input_end"])

    for task in record.get("tasks", []):
        raw = task["task_id"]
        if raw in created and task.get("sources"):
            created[raw]["sources"] = task["sources"]
        for entry in task.get("history", []):
            t = delegated.get(raw) if "lfm" in task.get("sources", []) else None
            t = at_revision(entry) if t is None else t
            if entry["event"] == "created":
                if "lfm" not in entry.get("sources", []):
                    create(t, raw, task.get("instruction") or entry.get("segment_text"), entry.get("sources", []))
            elif entry["event"] == "gate":
                events.append(_event(t, "gate_verdict", n, "server", task_id=task_id(raw), score=entry["score"],
                                     threshold=entry["threshold"], passed=entry["passed"], weakest=entry["weakest"]))
            elif entry["event"] == "status":
                status(t, raw, entry["new"], reason=entry.get("reason"))
    return events


def _replies(record, n, server_dir, turn_log, log, playback):
    """Replies the person heard: client playback times, refined to where the reply's WAV sounds."""
    said = [e for e in record.get("timeline", []) if e["kind"] == "reply"]
    starts = [e for e in turn_log if e["kind"] == "reply_start"]
    ends = [e for e in turn_log if e["kind"] == "reply_end"]
    barge = next((e["replay_t"] for e in log if e["kind"] == "barge_in" and e.get("turn") == n), None)
    later = [e["replay_t"] for e in log if e["kind"] == "reply_start" and e.get("turn", 0) > n]
    audible_end = max((offset + samples.size / rate for (samples, rate), offset in playback), default=0.0)
    events, rows, warnings = [], [], []
    if len(said) != len(starts):
        warnings.append(f"turn {n}: {len(said)} replies in the record, {len(starts)} reply starts in the client log")
    queued_until = 0.0
    for i, (reply, start) in enumerate(zip(said, starts)):
        end = ends[i] if i < len(ends) else {}
        heard_log = max(queued_until, start["replay_t"] + (end.get("first_audio_to_playback_s") or 0.0))
        wav = server_dir / f"{record['clip']}_{REPLY_FILES.get(reply['reply'], reply['reply'])}.wav"
        at, score, length = heard_log, None, 0.0
        if wav.is_file():
            clip, rate = read_wav(wav)
            length = clip.size / rate
            stems = [(samples, srate, offset) for (samples, srate), offset in playback]
            best = None
            for samples, srate, offset in stems:
                if srate != rate:
                    continue
                probe = max(0.2, min(1.0, length, audible_end - heard_log))
                found, s = locate(clip[:int(probe * rate)], samples, rate, heard_log - offset - 0.5,
                                  heard_log - offset + QUEUE_S)
                if found is not None and (best is None or s > best[1]):
                    best = (found + offset, s)
            if best is not None and best[1] >= MATCH_SCORE:
                at, score = best
        cuts = {"barge_in": barge, "next_turn": min(later, default=None), "recording_end": audible_end}
        stop, cut = at + length, None
        for reason, t in cuts.items():
            if t is not None and at < t < stop - 0.05:
                stop, cut = t, reason
        interrupted = cut in ("barge_in", "next_turn")
        queued_until = stop
        reply_id = f"{n}.{i + 1}"
        pointer = None
        if score is not None:
            pointer = {"clip": wav.name, "clip_offset_s": 0.0, "stem": "stems/conversation.wav", "at": round(at, 4),
                       "probe_s": round(min(1.0, stop - at), 3)}
        events.append(_event(at, "reply_start", n, "client", reply=reply["reply"], reply_id=reply_id,
                             text=reply.get("text", ""), **({"audio": pointer} if pointer else {})))
        events.append(_event(stop, "reply_end", n, "derived", reply=reply["reply"], reply_id=reply_id,
                             played_s=round(stop - at, 3), interrupted=interrupted, cut=cut))
        rows.append({"reply_id": reply_id, "kind": reply["reply"], "at_s": round(at, 4),
                     "score": None if score is None else round(score, 3),
                     "log_vs_audio_ms": round((heard_log - at) * 1000, 1), "played_s": round(stop - at, 3),
                     "interrupted": interrupted})
    return events, rows, warnings


def _waveform(mic, mic_rate, playback, duration):
    frames = int(np.ceil(duration * PEAK_FPS))
    assistant = place([(samples, rate, offset) for (samples, rate), offset in playback], mic_rate, duration)
    return {"fps": PEAK_FPS, "microphone": peaks(mic, mic_rate, frames=frames),
            "assistant": peaks(assistant, mic_rate, frames=frames)}


def _json(text):
    try:
        return json.loads(text) if isinstance(text, str) else text
    except ValueError:
        return text


def default_out(server_dir):
    from voice.prototype.report import VOICE_HOME
    return VOICE_HOME / "archives" / re.sub(r"[^\w.-]", "_", Path(server_dir).name)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--server", type=Path, required=True, help="server records directory with turn_*.json")
    parser.add_argument("--client", type=Path, required=True, help="client session directory with manifest.json")
    parser.add_argument("--out", type=Path, help="archive directory; default $VOICE_HOME/archives/<server run>")
    args = parser.parse_args()
    for path, name in ((args.server, "turn_001.json"), (args.client, "manifest.json")):
        if not (path / name).is_file():
            sys.exit(f"{path}: no {name}")
    out = export(args.server, args.client, args.out or default_out(args.server))
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    print(f"wrote {out}: {sum(manifest['counts'].values())} events, {manifest['duration_s']} s")
    for warning in manifest["warnings"]:
        print(f"  warning: {warning}")


if __name__ == "__main__":
    main()
