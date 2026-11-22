"""Record the hosted decider's answers to the line check's kind-only question (`verify.asks`, `a1_kind_only`) on
made-up lines for a request with no description, for voice/t3/test_verify.py.

    VOICE_ENV_FILE=<env file> python -m voice.t3.eval.record_kind_only

Each line is checked as a turn checks it (`verify.request`), with the shipped question and with the one before
2026-11-22 (`BEFORE`), `REPEATS` times. `kind_only` says the line names only the kind of request; the others say what
the request does. Every title, project and account is made up.
"""

from __future__ import annotations

import json
from pathlib import Path

from voice.liquid_decider.client import LiquidClient
from voice.t3.acts import Kind, act
from voice.t3.verify import request

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "kind_only.json"
REPEATS = 3
BEFORE = ("Not counting the thread's name, does the line say what the request or command would do or what it is for, "
          "instead of only what kind of request the thread waits on?")
BADGE = 'Make the "On Hold" badge readable'
FLEET = dict(begun_in="Example Fleet Notes", begun_for="sorting the fleet log by date")
CASES = [
    (act(Kind.NEEDS_THE_APP, thread=BADGE, decision="approve", need="file_change"),
     f"{BADGE} is waiting for your approval to change files, and that has to be given in the app.", True),
    (act(Kind.NEEDS_THE_APP, thread="Tidy the Save Slots", decision="approve", need="command_approval"),
     "Tidy the Save Slots is waiting for your approval to run a command, and that has to be given in the app.", True),
    (act(Kind.NEEDS_THE_APP, decision="approve", need="command_approval", **FLEET),
     "The thread you started in Example Fleet Notes for sorting the fleet log by date is waiting for your approval "
     "to run a command, and that has to be given in the app.", True),
    (act(Kind.NEEDS_YOU, need="command_approval", **FLEET),
     "The thread you started in Example Fleet Notes for sorting the fleet log by date needs your approval to run a "
     "command.", True),
    # The shape a gate run refused twice, copied from the permission example: "approval to go on".
    (act(Kind.NEEDS_THE_APP, decision="approve", need="command_approval", **FLEET),
     "The thread you started in Example Fleet Notes for sorting the fleet log by date is waiting for command approval "
     "to go on, and that has to be approved in the app.", True),
    (act(Kind.NEEDS_THE_APP, thread=BADGE, decision="approve", need="file_change"),
     f"{BADGE} is waiting for your approval to rewrite the badge styles file, and that has to be given in the app.",
     False),
    (act(Kind.NEEDS_THE_APP, thread="Tidy the Save Slots", decision="approve", need="command_approval"),
     "Tidy the Save Slots wants to run a command that deletes the old save files, and that has to be approved in the "
     "app.", False),
    (act(Kind.NEEDS_THE_APP, decision="approve", need="command_approval", **FLEET),
     "The thread you started in Example Fleet Notes for sorting the fleet log by date wants to run a script that "
     "uploads the log, and that has to be approved in the app.", False),
    (act(Kind.NEEDS_YOU, need="command_approval", **FLEET),
     "The thread you started in Example Fleet Notes for sorting the fleet log by date needs your approval to run a "
     "command that clears the cache.", False),
]


def bodies(said, line) -> dict:
    shipped = request((said,), line)
    before = json.loads(json.dumps(shipped))
    before["questions"]["a1_kind_only"]["instructions"] = BEFORE
    return {"shipped": shipped, "before": before}


def main() -> int:
    client = LiquidClient(max_attempts=3)
    rows = []
    for said, line, kind_only in CASES:
        row = {"act": said.to_json(), "line": line, "kind_only": kind_only}
        for name, body in bodies(said, line).items():
            samples = []
            for _ in range(REPEATS):
                record = client.call("decisions", body, label="t3_kind_only_fixture", fresh=True, cache_record=False)
                answer = ((record.get("response") or {}).get("answers") or {}).get("a1_kind_only")
                if record["status"] != 200 or not isinstance(answer, dict):
                    raise RuntimeError(f"Liquid gave an incomplete answer for {line!r}")
                samples.append(round(answer["noul"], 3))
            row[name] = samples
        rows.append(row)
        print(json.dumps({"line": line[:60], "kind_only": kind_only, "before": row["before"],
                          "shipped": row["shipped"]}), flush=True)
    FIXTURE.write_bytes((json.dumps({"rows": rows}, separators=(",", ":")) + "\n").encode())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
