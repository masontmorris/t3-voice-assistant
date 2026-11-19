"""Record the hosted decider's pick of a thread, so voice/t3/test_picks.py replays real answers with no live call.

    VOICE_ENV_FILE=<env file> python -m voice.t3.eval.record_picks

Each case is what a person said, the thread the call last spoke about when there is one, and what the pick must be:
`sure` of one thread (it is named plainly, so a question about it is answered at once), `leans` to one thread (it is
confirmed), `asks` (the words fit several of the named threads, or none in particular), or `none` (the thread is not
listed). Every thread, project and request here is made up. The cases run over a short listing and over one of 40.

The first set was looked at while the thresholds in voice/t3/verify.py were chosen. The second, `HELD`, was written
before any of it was run and has never been used to change anything: report both.
"""

from __future__ import annotations

import json
from pathlib import Path

from voice.liquid_decider.client import LiquidClient
from voice.t3.verify import Listed, pick_request

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "thread_picks.json"

SHORT = (  # newest first
    ("Build a Recipe App", "Voice sandbox", "working"),
    ("Add Checkout Button", "Voice sandbox", "finished"),
    ("Add Clamp Helper", "Voice sandbox", "finished"),
    ("Add a Sum Helper", "Voice sandbox", "failed"),
    ("Fix the flaky ASR test", "Voice server", "waiting_on_you"),
    ("Fix the flaky UI test", "Docs site", "working"),
    ("Add a wrap helper", "Voice sandbox", "idle"),
    ("Fork environment proof", "Voice server", "finished"),
    ("Migrate billing to the new ledger", "Storefront", "interrupted"),
    ("Dark mode for the settings page", "Docs site", "finished"),
    ("Upgrade the websocket library", "Voice server", "working"),
    ("Write the onboarding guide", "Docs site", "idle"),
)
MORE = tuple((title, project, "finished") for title, project in (
    ("Refactor the auth middleware", "Storefront"), ("Speed up the search index build", "Storefront"),
    ("Add CSV export to reports", "Storefront"), ("Fix the timezone bug in the scheduler", "Storefront"),
    ("Rename the config loader", "Voice server"), ("Add retry to the upload client", "Storefront"),
    ("Remove the legacy cron jobs", "Storefront"), ("Document the release process", "Docs site"),
    ("Add a rate limiter", "Voice server"), ("Fix the memory leak in the cache", "Voice server"),
    ("Update the dependency lockfile", "Docs site"), ("Add a keyboard shortcuts page", "Docs site"),
    ("Tune the endpoint detector", "Voice server"), ("Add unit tests for the parser", "Storefront"),
    ("Fix the broken image links", "Docs site"), ("Add an audit log", "Storefront"),
    ("Clean up the feature flags", "Storefront"), ("Add a health check route", "Voice server"),
    ("Fix the login redirect loop", "Storefront"), ("Add pagination to the orders table", "Storefront"),
    ("Profile the startup time", "Voice server"), ("Add search to the docs", "Docs site"),
    ("Fix the flaky deploy script", "Storefront"), ("Add an invoice PDF generator", "Storefront"),
    ("Sort the imports in the server", "Voice server"), ("Add a changelog page", "Docs site"),
    ("Fix the cart total rounding", "Storefront"), ("Add a metrics dashboard", "Voice server")))
LISTINGS = {"short": SHORT, "long": SHORT + MORE}

FLAKY = ["Fix the flaky ASR test", "Fix the flaky UI test"]
HELPERS = ["Add Clamp Helper", "Add a Sum Helper", "Add a wrap helper"]
# (request, the thread last spoken about, what the pick must be, the title or the titles it may ask about)
SEEN = [
    ("How is the recipe app thread going?", None, "sure", "Build a Recipe App"),
    ("Could you look at the checkout button thread and tell me where it stands?", None, "sure",
     "Add Checkout Button"),
    ("Where is the clamp helper thread at?", None, "sure", "Add Clamp Helper"),
    ("Look at the check out buton thread for me.", None, "sure", "Add Checkout Button"),
    ("Tell me about the threat for the recipe up.", None, "leans", "Build a Recipe App"),
    ("How's the billing migration coming along?", None, "sure", "Migrate billing to the new ledger"),
    ("Is the dark mode one done?", None, "sure", "Dark mode for the settings page"),
    ("What happened with the websocket upgrade?", None, "sure", "Upgrade the websocket library"),
    ("How's the onboarding doc?", None, "sure", "Write the onboarding guide"),
    ("What about the sum thing?", None, "sure", "Add a Sum Helper"),
    ("How is the one that failed doing?", None, "sure", "Add a Sum Helper"),
    ("Which one is waiting on me? Check that one.", None, "leans", "Fix the flaky ASR test"),
    ("What's going on with the interrupted one?", None, "sure", "Migrate billing to the new ledger"),
    ("How's the flaky test thread doing?", None, "asks", FLAKY),
    ("How is the helper thread going?", None, "asks", HELPERS),
    ("How's the flaky test in the docs site?", None, "sure", "Fix the flaky UI test"),
    ("How is that thread going?", "Add Checkout Button", "leans", "Add Checkout Button"),
    ("Is it done yet?", "Build a Recipe App", "leans", "Build a Recipe App"),
    ("How is that thread going?", None, "asks", None),
    ("How's it going?", None, "asks", None),
    ("What's up with the thread about the game?", None, "none", None),
    ("Check on the payments webhook thread.", None, "none", None),
    ("How's the kubernetes migration?", None, "none", None),
    ("Not that one, the clamp one.", "Add a Sum Helper", "sure", "Add Clamp Helper"),
    ("How's the other flaky test?", "Fix the flaky ASR test", "leans", "Fix the flaky UI test"),
]
HELD = [
    ("Did the fork environment proof finish?", None, "sure", "Fork environment proof"),
    ("Give me an update on the ledger migration.", None, "sure", "Migrate billing to the new ledger"),
    ("How's the wrap helper?", None, "sure", "Add a wrap helper"),
    ("Is the web socket library upgrade still running?", None, "sure", "Upgrade the websocket library"),
    ("What's happening with the resipe app?", None, "leans", "Build a Recipe App"),
    ("How's the settings page dark mode going?", None, "sure", "Dark mode for the settings page"),
    ("Check on the check out button.", None, "leans", "Add Checkout Button"),
    ("Any news on the guide for onboarding?", None, "sure", "Write the onboarding guide"),
    ("How's the ASR test fix?", None, "sure", "Fix the flaky ASR test"),
    ("What did the UI test thread say?", None, "sure", "Fix the flaky UI test"),
    ("How's the one that's idle in the docs site?", None, "leans", "Write the onboarding guide"),
    ("What's the latest on the helper I asked for?", None, "asks", HELPERS),
    ("How are the tests going?", None, "asks", FLAKY),
    ("What about the other one?", None, "asks", None),
    ("And that one?", "Upgrade the websocket library", "leans", "Upgrade the websocket library"),
    ("Has it finished?", "Add a Sum Helper", "leans", "Add a Sum Helper"),
    ("How's the database index thread?", None, "none", None),
    ("Check on the mobile app redesign.", None, "none", None),
    ("What's going on with the translation work?", None, "none", None),
    ("No, I meant the sum helper.", "Add Clamp Helper", "sure", "Add a Sum Helper"),
]


def listed(name: str) -> tuple[Listed, ...]:
    return tuple(Listed(f"t{i}", title, project, activity)
                 for i, (title, project, activity) in enumerate(LISTINGS[name], 1))


def cases():
    for held, batch in ((False, SEEN), (True, HELD)):
        for request, recent, want, about in batch:
            for name in LISTINGS:
                yield {"request": request, "recent": recent, "listing": name, "want": want, "about": about,
                       "held": held}


def main() -> int:
    client = LiquidClient(max_attempts=3)
    criteria = {name: pick_request("", listed(name))["questions"]["pick"] for name in LISTINGS}
    kept = json.loads(FIXTURE.read_text(encoding="utf-8")) if FIXTURE.exists() else {}
    old = ({(row["request"], row["recent"], row["listing"]): row for row in kept.get("rows", [])}
           if kept.get("criteria") == criteria else {})
    rows, took = [], []
    for case in cases():
        key = (case["request"], case["recent"], case["listing"])
        if key in old:
            rows.append({**old[key], **case})
            continue
        body = pick_request(case["request"], listed(case["listing"]), case["recent"])
        record = client.call("decisions", body, label="t3_pick_fixture", fresh=True, cache_record=False)
        answer = ((record.get("response") or {}).get("answers") or {}).get("pick")
        if record["status"] != 200 or not isinstance(answer, dict):
            raise RuntimeError(f"Liquid gave an incomplete answer for {case['request']!r}: status {record['status']}")
        rows.append({**case, "choice": answer.get("choice"),
                     "p": {k: round(v, 3) for k, v in answer["probabilities"].items() if v >= 0.001}})
        took.append(record["timing"]["ms"])
        print(json.dumps({"request": case["request"], "listing": case["listing"], "choice": rows[-1]["choice"],
                          "ms": round(took[-1])}), flush=True)
    if took:
        took.sort()
        print(json.dumps({"calls": len(took), "median_ms": round(took[len(took) // 2]),
                          "p95_ms": round(took[int(len(took) * 0.95)])}), flush=True)
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_bytes((json.dumps({"criteria": criteria, "rows": rows}, separators=(",", ":")) + "\n").encode())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
