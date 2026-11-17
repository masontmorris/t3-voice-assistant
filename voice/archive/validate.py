"""Check a loop archive: schema, file hashes, event ordering, and that the audio sits where the timeline says.

    python -m voice.archive.validate ARCHIVE_DIR [--json]

The alignment check is independent of how the exporter placed the clips. For every event with an `audio` pointer it
cross-correlates one second of the named clip against the stem around `at`, and reports the measured lag. A lag over
20 ms, or a correlation too weak to call a match, is an error. Exit status 1 means at least one error.
"""

import argparse
import json
import math
import sys
from collections import Counter
from pathlib import Path

from voice.archive.audio import duration_s, locate, read_wav, resample, sha256
from voice.archive.schema import ALIGN_TOLERANCE_S, EVENTS, MATCH_SCORE, ORDER, SCHEMA, SOURCES

PROBE_S = 1.0
SEARCH_S = 0.25


def check(root):
    root = Path(root)
    errors, alignment = [], []
    try:
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"errors": [f"manifest.json: {exc}"], "alignment": [], "counts": {}}
    if manifest.get("schema") != SCHEMA:
        errors.append(f"manifest schema {manifest.get('schema')!r}, expected {SCHEMA!r}")
    for name, digest in manifest.get("files", {}).items():
        path = root / name
        if not path.is_file():
            errors.append(f"{name}: listed in the manifest but missing")
        elif sha256(path) != digest:
            errors.append(f"{name}: SHA-256 does not match the manifest")
    for stem in manifest.get("stems", []):
        path = root / stem["file"]
        if path.is_file() and abs(duration_s(path) - stem["duration_s"]) > 0.002:
            errors.append(f"{stem['file']}: duration {duration_s(path):.3f} s, manifest says {stem['duration_s']}")

    events = []
    for n, line in enumerate((root / "timeline.jsonl").read_text(encoding="utf-8").splitlines(), 1):
        try:
            events.append(json.loads(line))
        except ValueError as exc:
            errors.append(f"timeline line {n}: {exc}")
    errors += _check_events(events, manifest.get("duration_s", math.inf))
    stems = {}
    for event in events:
        pointer = event.get("audio")
        if not pointer:
            continue
        row = _check_alignment(root, pointer, stems)
        row.update(seq=event.get("seq"), type=event.get("type"), turn=event.get("turn"))
        alignment.append(row)
        if row["error"]:
            errors.append(f"seq {event.get('seq')} {event.get('type')}: {row['error']}")
    return {"errors": errors, "alignment": alignment, "counts": dict(Counter(e.get("type") for e in events))}


def _check_events(events, duration):
    errors, last_t = [], -math.inf
    for i, event in enumerate(events):
        where = f"seq {event.get('seq')}"
        if event.get("seq") != i:
            errors.append(f"{where}: expected seq {i}")
        kind = event.get("type")
        if kind not in EVENTS:
            errors.append(f"{where}: unknown type {kind!r}")
            continue
        if event.get("src") not in SOURCES:
            errors.append(f"{where}: src {event.get('src')!r} is not one of {SOURCES}")
        missing = [k for k in EVENTS[kind] if k not in event]
        if missing:
            errors.append(f"{where} {kind}: missing {missing}")
        t = event.get("t")
        if not isinstance(t, (int, float)) or t < 0:
            errors.append(f"{where}: bad t {t!r}")
            continue
        if t < last_t:
            errors.append(f"{where}: t {t} is before the previous event's {last_t}")
        if t > duration + 0.5:
            errors.append(f"{where}: t {t} is past the archive's {duration} s")
        last_t = max(last_t, t)
    for earlier, later, key in ORDER:
        first = {}
        for event in events:
            if event.get("type") in (earlier, later) and key in event:
                first.setdefault((event["type"], json.dumps(event[key])), event["t"])
        for (kind, value), t in first.items():
            if kind == later and (earlier, value) in first and first[(earlier, value)] > t + 1e-6:
                errors.append(f"{earlier} after {later} for {key} {value}")
    return errors


def _check_alignment(root, pointer, stems):
    row = {"clip": pointer["clip"], "stem": pointer["stem"], "at": pointer["at"], "lag_ms": None, "score": None,
           "error": None}
    if pointer["stem"] not in stems:
        stems[pointer["stem"]] = read_wav(root / pointer["stem"])
    stem, rate = stems[pointer["stem"]]
    clip, clip_rate = read_wav(root / pointer["clip"])
    clip = resample(clip, clip_rate, rate)
    begin = int(pointer.get("clip_offset_s", 0.0) * rate)
    probe = clip[begin:begin + int(pointer.get("probe_s", PROBE_S) * rate)]
    found, score = locate(probe, stem, rate, pointer["at"] - SEARCH_S, pointer["at"] + SEARCH_S)
    row["score"] = round(score, 3)
    if found is None or score < MATCH_SCORE:
        row["error"] = f"{pointer['clip']} not found in {pointer['stem']} near {pointer['at']:.3f} s (score {score:.2f})"
        return row
    row["lag_ms"] = round((found - pointer["at"]) * 1000, 1)
    if abs(found - pointer["at"]) > ALIGN_TOLERANCE_S:
        row["error"] = f"{pointer['clip']} sounds {row['lag_ms']} ms from where the timeline puts it"
    return row


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("archive", type=Path)
    parser.add_argument("--json", action="store_true", help="print the full report as JSON")
    args = parser.parse_args()
    report = check(args.archive)
    if args.json:
        print(json.dumps(report, indent=1))
    else:
        lags = [abs(r["lag_ms"]) for r in report["alignment"] if r["lag_ms"] is not None]
        print(f"{args.archive}: {sum(report['counts'].values())} events, {len(report['alignment'])} audio checks, "
              f"max |lag| {max(lags) if lags else 'n/a'} ms, {len(report['errors'])} errors")
        for error in report["errors"]:
            print(f"  error: {error}")
    sys.exit(1 if report["errors"] else 0)


if __name__ == "__main__":
    main()
