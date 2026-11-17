"""Does the batched state bias the hybrid's break answers? Resend a recorded hybrid request with only its break questions.

    python -m voice.research.segmentation.probe_breaks CACHE_FILE [CACHE_FILE ...] [--cache-dir DIR]

Each CACHE_FILE is a recorded hybrid decisions request. The probe keeps its `finished` and `at` questions, drops the
classification questions, and drops every state field but `new_speech` and `transcript_is_final`, so the decider no
longer sees the current pieces listed as segments. It prints both answers per piece. --cache-dir points the client's
cache and ledger at another directory, such as the prototype's under $VOICE_HOME for Mason's clips.
"""

import argparse
import json
from pathlib import Path

from voice.segmentation import client as api


def breaks_only(body):
    questions = {k: v for k, v in body["questions"].items() if k.endswith(("_finished", "_at"))}
    state = {"new_speech": body["state"]["new_speech"], "transcript_is_final": body["state"]["transcript_is_final"]}
    return {"model": body["model"], "state": state, "questions": questions}


def summary(answers, name):
    a = answers.get(name) or {}
    return a.get("choice", a.get("noul")), a.get("confidence")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("files", nargs="+")
    parser.add_argument("--cache-dir", type=Path)
    args = parser.parse_args()
    if args.cache_dir:
        api.CACHE, api.LEDGER = args.cache_dir, args.cache_dir / "calls.jsonl"
    client = api.Client()
    for name in args.files:
        recorded = json.loads((api.CACHE / name).read_text(encoding="utf-8"))
        probe = client.call("decisions", breaks_only(recorded["request"]), label=f"probe:{recorded['label']}")
        batched, alone = recorded["response"]["answers"], probe["response"]["answers"]
        for q in sorted(k for k in probe["request"]["questions"] if k.endswith("_at")):
            print(f"{recorded['label']} {q}: batched {summary(batched, q)} alone {summary(alone, q)} "
                  f"(options {sorted(probe['request']['questions'][q]['criteria'])})")


if __name__ == "__main__":
    main()
