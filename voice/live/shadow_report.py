"""The agreement table for one call directory's shadow decider records.

    python -m voice.live.shadow_report CALL_DIR [--rows] [--json]

Reads `shadow.jsonl`, which live mode writes beside its turn records with --shadow on (voice/live/shadow.py), and
prints one row per utterance: how many tasks LFM3 acted on, how many the decider would have, how the two agreed, and
the shadow call's own latency. A line whose call failed, timed out or was skipped counts under `state` and its
decisions are missing, not empty.

`lfm` and `dec` count the actions each side took, `agree`/`dis`/`dec-only`/`lfm-only` are the per-task verdicts,
`dec-corr`/`lfm-corr` count tasks one side alone would cancel or amend, and `ms` is the shadow call only, so no turn's
first-reply latency is in it. `state` is `ok`, `error`, `timeout`, `abandoned` (a new utterance or the hang-up ended
the call) or `skipped` (no call: a turn was running, the decider was busy, or the call ended before the reply played). The table's verdicts are derived from each
line's stored sides (`lfm.calls`, `decider.tasks`, `outcomes`), so a correction to the comparison applies to records
already written; the line also keeps the verdicts the call itself computed. The last line says which
end-of-utterance rule the run used: with `decider` authority LFM3's column is empty by design, because live mode
opens no tool session.
"""

import argparse
import json
import sys
from pathlib import Path

from voice.live.shadow import FILE, agreement

COLUMNS = ("turn", "auth", "lfm", "dec", "agree", "dis", "dec-only", "lfm-only", "dec-corr", "lfm-corr", "ms",
           "state")


def readings(entries):
    """Each line's per-task comparison, from the sides it stored."""
    return [agreement((entry.get("lfm") or {}).get("calls") or [],
                      (entry.get("decider") or {}).get("tasks") or [],
                      outcomes=entry.get("outcomes")) for entry in entries]


def rows(entries, found):
    """One row per utterance, in the order the calls returned."""
    for entry, reading in zip(entries, found):
        calls = (entry.get("lfm") or {}).get("calls") or []
        call = entry.get("call") or {}
        yield {"turn": entry.get("utterance"), "auth": entry.get("authority"),
               "lfm": len(calls), "dec": len((entry.get("decider") or {}).get("tasks") or []),
               "agree": reading["agree"], "dis": reading["disagree"],
               "dec-only": reading["decider_only"], "lfm-only": reading["lfm_only"],
               "dec-corr": reading["decider_correction"], "lfm-corr": reading["lfm_correction"],
               "ms": call.get("ms"), "state": state(entry)}


def state(entry):
    if entry.get("skipped"):
        return "skipped"
    if entry.get("abandoned"):
        return "abandoned"
    if (entry.get("call") or {}).get("timed_out"):
        return "timeout"
    return "error" if entry.get("error") else "ok"


def print_table(entries, detail=False):
    found = readings(entries)
    table = list(rows(entries, found))
    if not table:
        print(f"no {FILE} lines in this directory")
        return
    widths = {name: max(len(name), *(len(str(row[name])) for row in table)) for name in COLUMNS}
    print("  ".join(name.ljust(widths[name]) for name in COLUMNS))
    for entry, row, reading in zip(entries, table, found):
        print("  ".join(str(row[name]).ljust(widths[name]) for name in COLUMNS))
        if detail:
            for line in reading["rows"]:
                print(f"    {line['verdict']:<18} {line['task']:<9} decider {str(line['decider']):<7} "
                      f"lfm {str(line['lfm']):<7} turn {str(line.get('turn')):<10} {line['text']}")
    totals = {name: sum(row[name] for row in table) for name in COLUMNS[2:-2]}
    first = "  ".join("".ljust(widths[name]) for name in COLUMNS[:2])
    print(first + "  " + "  ".join(str(totals[name]).ljust(widths[name]) for name in COLUMNS[2:-2]) + "  totals")
    missed = [row["turn"] for row in table if row["state"] != "ok"]
    if missed:
        print(f"{len(missed)} of {len(table)} utterances have no shadow answer ({', '.join(missed)}); "
              f"see `error` on those lines")
    authorities = [entry.get("authority") for entry in entries]
    if authorities and all(authority == "decider" for authority in authorities):
        print("authority: decider in every turn, so LFM3 made no task call to compare against")
    elif authorities:
        print("authority: " + ", ".join(f"{authority} {authorities.count(authority)}" for authority in
                                        dict.fromkeys(authorities)))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("directory", type=Path, help="a call directory holding shadow.jsonl")
    parser.add_argument("--rows", action="store_true", help="print one line per task under each turn")
    parser.add_argument("--json", action="store_true", help="print the parsed lines instead of the table")
    args = parser.parse_args(argv)
    path = args.directory / FILE
    if not path.is_file():
        print(f"no {FILE} in {args.directory}", file=sys.stderr)
        return 1
    entries = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if args.json:
        print(json.dumps(entries, indent=1))
    else:
        print_table(entries, args.rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
