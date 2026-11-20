"""Record each decider's answer to whether a summary written for one question answers another, so
voice/t3/test_reuse.py replays real answers.

    python -m voice.t3.eval.record_reuse --model local     # the local d1-2B, started here (GPU lease) or at VOICE_D1_URL
    VOICE_ENV_FILE=<env file> python -m voice.t3.eval.record_reuse --model hosted   # Liquid's d1.2, under its day cap

Each case is the words an early summary was written for (None: what the thread reported), the summary, the question
asked at the end, and whether the summary answers it. A wrong reuse speaks a true summary that answers another
question, so it is the costly error; a missed reuse only writes the summary again. Every thread, question and
summary here is made up. `HELD` was written before any run, but the bar was set after both sets were read, so
neither is a clean test of it. The fixture keeps each answer under a hash of the model and the request, so a changed
request text finds no answer and the test says to record again.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from voice.t3.verify import noul, reuse_request

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "summary_reuse.json"
TITLE = "Example Thread"
# (early words, summary written for them, final question, the summary answers it)
SEEN = [
    ("How's the clamp", "It says it added a clamp helper next to sum, and all 12 tests pass.",
     "How's the clamp thread going?", True),
    ("Did the", "It says it finished the migration and every table is moved.", "Did the tests pass?", False),
    ("Is the export", "It says the export split is done: one file per month, each with the CSV header.",
     "Is the export split done?", True),
    ("Is it done?", "Yes, it says the retry button is in and the PR is open.", "Did it open a PR?", True),
    ("Is it done?", "Not yet: it says two of five screens are converted.", "Did it open a PR?", False),
    ("What did the review", "It reports the review found two blocking bugs in the cache layer.",
     "What did the review find?", True),
    ("What did the review", "It reports the review is finished.", "What did the review find?", False),
    ("Why did it", "It says it stopped because the sandbox had no network access.", "Why did it stop?", True),
    ("Why did it", "It says it stopped because the sandbox had no network access.", "Why did it pick SQLite?", False),
    ("How long", "It says the full build now takes four minutes, down from nine.", "How long does the build take now?",
     True),
    ("Does it need", "It wants your approval to run a command that deletes the old cache.",
     "Does it need anything from me?", True),
    ("Does it need", "It says the cache cleanup finished and freed 3 GB.", "Does it need my approval for anything?",
     False),
    (None, "It reports the login fix is merged and the flaky test is gone.", "Is the login fix merged?", True),
    (None, "It reports the login fix is merged and the flaky test is gone.", "How much did the bundle size drop?",
     False),
    ("What's it", "It says it is rerunning the benchmark with the new cache settings.", "What's it doing now?", True),
    ("What's it doing", "It says it is rerunning the benchmark.", "What did the last benchmark show?", False),
    ("Is the", "It says the docs update is pushed to the docs branch.", "Is the docs update pushed?", True),
    ("Is the", "It says the docs update is pushed to the docs branch.", "Is the release out?", False),
    ("Did the build", "It reports the nightly build failed at the bundling step because the disk was full.",
     "Did the build pass?", True),
    ("How's the", "It says three of seven billing tables are migrated.", "How's the billing migration going?", True),
    ("How's the", "It says three of seven billing tables are migrated.", "Who asked for the migration?", False),
    ("What does it", "It wants your answer on whether to keep the old API.", "What does it need from me?", True),
]
HELD = [
    ("How's the upload", "It says it fixed the upload race and all tests pass.", "How's the upload fix going?", True),
    ("Did it", "It reports the image resizer is done and merged.", "Did it write a migration guide?", False),
    ("Is it", "Not yet: it says the API routes are half converted.", "Is it finished?", True),
    ("Is it", "Not yet: it says the API routes are half converted.", "Is it using the new auth library?", False),
    ("What happened", "It says the run failed when the GPU lease timed out.", "What happened with it?", True),
    ("What happened", "It says the run failed when the GPU lease timed out.", "What happened to the cost estimate?",
     False),
    ("Why is it", "It is waiting for your approval to delete the temp folder.", "Why is it stuck?", True),
    (None, "It reports the search page loads in half the time and the PR is merged.", "Is the search PR merged?", True),
    (None, "It reports the search page loads in half the time and the PR is merged.",
     "Did it add tests for the search page?", False),
    ("How many", "It says it found 14 broken links.", "How many broken links did it find?", True),
    ("How many", "It says it found 14 broken links.", "How many pages did it check?", False),
    ("What's the", "It says the bottom line is that the cache bug is real and fixed.", "What's the bottom line?", True),
    ("Can it", "It says it can't reach the staging server.", "Can it reach staging?", True),
    ("Can it", "It says it can't reach the staging server.", "Can it finish today?", False),
    ("Did the tests", "It says all 230 tests pass on the new branch.", "Did the tests pass?", True),
    ("Did the tests", "It says all 230 tests pass on the new branch.", "Did the tests catch the bug?", False),
    ("What is it waiting", "It is waiting for the build machine lease.", "What is it waiting for?", True),
    (None, "It reports the translation pass is done for all eight languages.", "Are the translations done?", True),
    ("Is the dark mode", "It says dark mode is done for settings but not the editor.", "Is the dark mode work done?",
     True),
    ("Is the dark mode", "It says dark mode is done for settings but not the editor.", "Is the light theme broken?",
     False),
]
CASES = [(*case, False) for case in SEEN] + [(*case, True) for case in HELD]


def key(model: str, body: dict) -> str:
    return hashlib.sha256(json.dumps([model, body], sort_keys=True).encode("utf-8")).hexdigest()


def body(case) -> dict:
    asked, gist, question = case[:3]
    return reuse_request(TITLE, gist, asked, question)


def client(model: str):
    if model == "hosted":
        from voice.liquid_decider.client import LiquidClient
        return LiquidClient(max_attempts=3)
    from voice.local_decider.client import D1Client
    return D1Client()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", choices=("local", "hosted"), required=True)
    args = parser.parse_args(argv)
    recorded = json.loads(FIXTURE.read_text(encoding="utf-8")) if FIXTURE.exists() else {"rows": [], "answers": {}}
    recorded["rows"] = [r for r in recorded["rows"] if r["model"] != args.model]
    decider = client(args.model)
    for i, case in enumerate(CASES):
        request = body(case)
        record = decider.call("decisions", request, label="t3_reuse")
        # Only the answers are kept: a local server also reports the path of the model file it loaded.
        response = record["response"]
        kept = {"answers": response.get("answers")} if isinstance(response, dict) else None
        recorded["answers"][key(args.model, request)] = {"status": record["status"], "response": kept}
        recorded["rows"].append({"model": args.model, "case": i, "held": case[4], "answers": case[3],
                                 "p_yes": noul(record, "answers"), "ms": round(record["timing"]["ms"], 1)})
        print(json.dumps(recorded["rows"][-1]), flush=True)
    FIXTURE.write_bytes((json.dumps(recorded, indent=1) + "\n").encode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
