"""Record the hosted reader on replies to a confirmation, so voice/t3/test_consent.py replays real answers.

    VOICE_ENV_FILE=<env file> python -m voice.t3.eval.record_consent [--before] [--samples N]

Each case is a confirmation the voice asks before it acts (start a thread, send a message, stop a run, approve or deny a
request) and a reply, sent as the live reader sends it (`reading_request`) with fresh calls. A case passes when the
reply authorises the action exactly when it should (`Reading.yes`). A bare "okay", "sure" or "alright" said straight
after a yes-or-no question is a yes: that is how people answer. A thanks, a goodbye, or words that acknowledge what was
said ("got it", "cool") are not consent, and "okay, thanks" is not.

`SEEN` was looked at while the reader's text was written; `HELD` was written before any run and never used to change
anything. `--before` keeps only the verdicts, under `before`, for the code it ran on. The default run keeps every answer
by case and sample, so the test replays them. Every thread, project and request here is made up.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from voice.t3.acts import Kind, act
from voice.t3.verify import judge_reading, reading_request

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "consent_reads.json"
ACTS = {
    "launch": act(Kind.CONFIRM_LAUNCH, project="Example Garden Planner"),
    "tell": act(Kind.CONFIRM_TELL, thread="Example Fix the Seed Sorter", message="also add a test for empty trays"),
    "stop": act(Kind.CONFIRM_STOP, thread="Example Fix the Seed Sorter"),
    "approve": act(Kind.CONFIRM_RESPOND, thread="Example Fix the Seed Sorter", decision="approve",
                   need="command_approval"),
    "deny": act(Kind.CONFIRM_RESPOND, thread="Example Fix the Seed Sorter", decision="deny"),
}
# (reply, whether it authorises the action)
SEEN = [
    ("Yes.", True), ("Yeah, go ahead.", True), ("Sure.", True), ("Do it.", True), ("Please.", True),
    ("Okay, do that.", True), ("Mm-hm.", True), ("Sounds good, do it.", True), ("Okay.", True), ("Alright.", True),
    ("Okay, thanks.", False), ("Thanks.", False), ("Got it.", False), ("Cool.", False), ("Okay, bye.", False),
    ("Never mind, thanks.", False), ("No.", False), ("Wait, what does it do?", False),
    ("Yes, but only the tests.", False),
]
HELD = [
    ("Yes please.", True), ("Go ahead.", True), ("Yeah, do it.", True), ("Sure thing.", True), ("Yup.", True),
    ("Okay, go ahead.", True), ("Uh-huh, yes.", True), ("Please do, thanks.", True),
    ("Thank you.", False), ("Okay, cool, thanks.", False), ("Great, thanks for letting me know.", False),
    ("Alright, bye.", False), ("Okay, never mind.", False), ("Got it, thanks.", False), ("No thanks.", False),
    ("Not now.", False), ("Hmm, what was that?", False), ("Yes, but use the other branch.", False),
]
CASES = [(kind, reply, must, held) for held, replies in ((False, SEEN), (True, HELD))
         for reply, must in replies for kind in ACTS]


def key(body: dict) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode("utf-8")).hexdigest()


def replayed(answers: list[dict], kind: str, reply: str) -> list[bool]:
    return [judge_reading(a).yes for a in answers]


def summary(rows: list[dict]) -> list[str]:
    lines = []
    for held in (False, True):
        for must in (True, False):
            chosen = [r for r in rows if r["held"] == held and r["must_act"] == must]
            accepted = sum(sum(r["yes"]) for r in chosen)
            lines.append(f"{'held' if held else 'seen'} {'real yeses' if must else 'non-consents'} accepted: "
                         f"{accepted} of {sum(len(r['yes']) for r in chosen)}")
    return lines


def main() -> int:
    from voice.liquid_decider.client import LiquidClient

    parser = argparse.ArgumentParser()
    parser.add_argument("--before", action="store_true", help="keep the verdicts only, under `before`")
    parser.add_argument("--samples", type=int, default=5)
    args = parser.parse_args()
    stored = json.loads(FIXTURE.read_text(encoding="utf-8")) if FIXTURE.exists() else {}
    client = LiquidClient(max_attempts=3)
    rows = []
    for kind, reply, must, held in CASES:
        body = reading_request(ACTS[kind], reply)
        answers = []
        for _ in range(args.samples):
            record = client.call("decisions", body, label="t3_consent", fresh=True, cache_record=False)
            if record["status"] != 200:
                raise RuntimeError(f"decider status {record['status']} for {reply!r}")
            answers.append((record.get("response") or {}).get("answers") or {})
        row = {"act": kind, "reply": reply, "must_act": must, "held": held, "request": key(body),
               "yes": [judge_reading(a).yes for a in answers]}
        if not args.before:
            row["answers"] = answers
        rows.append(row)
        print(json.dumps({k: row[k] for k in ("act", "reply", "must_act", "held", "yes")}), flush=True)
    stored["before" if args.before else "rows"] = rows
    FIXTURE.write_bytes((json.dumps(stored, separators=(",", ":")) + "\n").encode("utf-8"))
    print("\n".join(summary(rows)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
