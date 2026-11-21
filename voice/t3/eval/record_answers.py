"""Record the hosted decider's choice among the options of a question a thread asked, so voice/t3/test_answers.py replays
real answers with no live call.

    VOICE_ENV_FILE=<env file> python -m voice.t3.eval.record_answers [--only seen|held]

Each case is a question as the voice told it, with its options, what the person said back, and what the choice must be:
`option:N` (the reply picks the Nth option, by name, by place or in other words), `own_words` (it answers in words no
option says, where the question takes them), `none` (it answers nothing: it asks something else or talks about something
else) or `not option` (it picks no option and is no answer to send: it fits none, or none in particular). `either` is a reply
whose reading as an option or as words is a fair call, and is scored only for being one of the two. Every question,
option and reply here is made up.

`SEEN` and `HELD` were both written before the first run, and neither was used to change anything. `LATE` was written
after a rehearsal showed a plain answer to a question with no options below the first bar for the caller's own words
(WORDS_SURE in voice/t3/verify.py); the bar was set from that one case, and `LATE` was written before it was run on
those cases. Report all three.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from voice.liquid_decider.client import LiquidClient
from voice.t3.verify import choice_request

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "answer_choices.json"

# name: (question, ((label, description), ...), whether its own words are an answer)
QUESTIONS = {
    "storage": ("Which storage should it use?", (("Postgres", "A hosted relational database."),
                                                  ("SQLite", "A single local file."),
                                                  ("Keep the current store", "Leave the storage as it is.")), False),
    "storage_words": ("Which storage should it use?", (("Postgres", "A hosted relational database."),
                                                        ("SQLite", "A single local file."),
                                                        ("Keep the current store", "Leave the storage as it is.")), True),
    "access": ("Should the new endpoint be public or require login?", (("Public", "Anyone can call it."),
                                                                         ("Require login", "Only signed-in users can.")), False),
    "naming": ("How should the export be named?", (("By date", "The file is named for the day it was made."),
                                                    ("By project name", "The file is named for the project.")), True),
    "runner": ("Which test runner should it set up?", (("Vitest", "Fast and built on Vite."),
                                                        ("Jest", "The long-standing default.")), False),
    "table": ("What should the table be called?", (), True),
    "commits": ("Do you want the changes in one commit or several?", (("One commit", "Everything together."),
                                                                         ("Several commits", "One per step.")), False),
}

SEEN = [
    ("storage", "Postgres.", "option:1"), ("storage", "The second one.", "option:2"),
    ("storage", "I'd say keep the current store.", "option:3"), ("storage", "Whichever it's already using.", "option:3"),
    ("storage", "Let's go with the first.", "option:1"), ("storage", "Last one.", "option:3"),
    ("storage", "SQLite, I think.", "option:2"), ("storage", "Use Mongo.", "not option"),
    ("storage", "What's the weather like?", "none"), ("storage", "Which one do you recommend?", "none"),
    ("storage", "Hmm, either of the first two.", "not option"),
    ("access", "Make it public.", "option:1"), ("access", "Require login for sure.", "option:2"),
    ("access", "Behind a login.", "option:2"), ("access", "Anyone can hit it.", "option:1"),
    ("naming", "By date.", "option:1"), ("naming", "Name it after the project.", "option:2"),
    ("naming", "Call it export underscore final.", "own_words"),
    ("table", "Call it orders.", "own_words"), ("table", "Orders table.", "own_words"), ("table", "Never mind.", "none"),
    ("commits", "One commit.", "option:1"), ("commits", "Split it up.", "option:2"), ("commits", "Several.", "option:2"),
    ("commits", "Doesn't matter.", "not option"),
]
HELD = [
    ("runner", "Vitest please.", "option:1"), ("runner", "The Jest one.", "option:2"),
    ("runner", "Let's stay with what we have.", "not option"), ("runner", "I prefer the first one.", "option:1"),
    ("runner", "Number two.", "option:2"), ("runner", "Mocha.", "not option"),
    ("storage_words", "Use Postgres.", "option:1"), ("storage_words", "A plain JSON file is fine.", "own_words"),
    ("storage_words", "Can you remind me what the options were?", "none"), ("storage_words", "Yeah, go ahead.", "not option"),
    ("access", "I think it should need a login.", "option:2"),
    ("commits", "Let's do it in one go.", "option:1"), ("commits", "I'd rather have a few smaller ones.", "option:2"),
    ("commits", "Can you put it in a pull request?", "not option"),
    ("table", "Let's name it customer orders.", "own_words"),
    ("table", "Actually, wait, how's the other thread going?", "none"),
    ("naming", "By the project name, thanks.", "option:2"), ("naming", "Do it by date.", "option:1"),
]


LATE = [
    ("table", "Call it level cache.", "own_words"), ("table", "Let's call it level cache.", "own_words"),
    ("table", "The invoices table.", "own_words"), ("table", "Name it audit log.", "own_words"),
    ("table", "Let's go with sessions.", "own_words"), ("table", "customers_archive", "own_words"),
    ("table", "Yes.", "none"), ("table", "I don't know, what do you think?", "none"),
    ("table", "What are my options?", "none"), ("table", "Hmm, let me think.", "not option"),
    ("naming", "Both, I guess.", "not option"), ("naming", "Call it something with the date in it.", "either"),
]


def request_for(name: str, reply: str) -> dict:
    question, options, own_words = QUESTIONS[name]
    return choice_request(question, options, reply, own_words)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--only", choices=["seen", "held", "late"])
    args = parser.parse_args(argv)
    previous = json.loads(FIXTURE.read_text(encoding="utf-8"))["rows"] if FIXTURE.exists() else []
    client = LiquidClient(timeout_s=30.0, max_attempts=3)
    rows = []
    for kind, cases in (("seen", SEEN), ("held", HELD), ("late", LATE)):
        for name, reply, want in cases:
            if args.only and args.only != kind:
                rows.extend(r for r in previous if r["question"] == name and r["reply"] == reply)
                continue
            record = client.call("decisions", request_for(name, reply), label="t3_answer_record")
            answer = ((record.get("response") or {}).get("answers") or {}).get("choice") or {}
            rows.append({"question": name, "reply": reply, "want": want, "set": kind, "choice": answer.get("choice"),
                         "p": answer.get("probabilities")})
            print(json.dumps(rows[-1]), flush=True)
    FIXTURE.write_text(json.dumps({"questions": {k: [q, [list(o) for o in opts], words]
                                                 for k, (q, opts, words) in QUESTIONS.items()}, "rows": rows},
                                  indent=1) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
