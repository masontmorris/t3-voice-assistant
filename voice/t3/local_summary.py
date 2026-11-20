"""LFM3-1.7B as the thread summariser: the prompt measured as S11, its JSON output, and the local d1-2B's gate.

Every piece here is what docs/voice/local-renderer.md measured on threads not read before: the last reply as JSON,
worked examples on made-up threads, at most 20 words asked for, the model card's sampling, and a JSON schema whose
`gist` cannot hold markdown or code characters. The examples teach how to read a record; none is ever spoken. The
gate asks the local d1-2B whether the last reply and status support the gist; when they may not, the gist is
dropped and the voice says the thread's status with no summary.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any

from voice.t3 import types as T
from voice.t3.lifecycle import need_of
from voice.t3.verify import noul

MAX_WORDS = 25


# S2: SUMMARY_SYSTEM without its conflicts. It told the model not to say what state the thread is in, then to say
# what a thread with no reply waits on; it allowed code "unless asked"; and its limits sat among six other duties.
SYSTEM = (
    "You write one sentence that a voice assistant says out loud about a coding thread, an AI coding agent's "
    "conversation in an app called T3. Answer the person's question from the thread's records. When there is no "
    "question, or the records do not answer it, say the main thing the records show. Credit any work or result to "
    "the thread, as in \"it says\" or \"it reports\". Use only what the records say. The assistant says the thread's "
    "name and status itself, so do not repeat them. Write plain spoken words: one sentence, at most 25 words, no "
    "markdown, no code, no file names, paths or commands."
)
FIELDS_HEAD = " The records are JSON. `thread` is the thread's name, only a name and not proof of what was done."
STATUS_FIELD = " `status` is where its run stands."
NEED_FIELDS = (" When it waits on the person, `waiting_for` says for what and `pending` is its request, which you "
               "describe in your own words.")
MESSAGE_FIELDS = (" `messages` are the thread's messages, oldest first: `from` is `request` for a message sent to the "
                  "thread and `thread` for the thread's own reply. The last `thread` message is its latest reply. "
                  "`left_out` counts earlier messages not shown. Long messages are cut.")
VIEW_FIELDS = FIELDS_HEAD + STATUS_FIELD + NEED_FIELDS + MESSAGE_FIELDS
ADJACENT = "Answer in one plain spoken sentence of at most 25 words, crediting the thread."

# Made-up threads. Each teaches how to read a view, never wording for the live path.
EXAMPLES = (
    ({"thread": "Example Flaky Upload Test", "status": "finished", "waiting_for": None, "pending": None,
      "messages": [{"from": "request", "text": "Fix the flaky upload test and open a PR when it passes."},
                   {"from": "thread", "text": "The failure is a race: the mock server starts after the first "
                                              "request. Awaiting its ready promise now."},
                   {"from": "thread", "text": "Fixed the race in `upload.test.ts` by awaiting `server.ready()`. "
                                              "Ran the suite 20 times: **48/48 pass** every run.\n\nOpened PR #212."}],
      "left_out": 0},
     "Did it open the PR?", "Yes, it says it opened PR 212 after fixing a race in the upload test, and all 48 tests "
                            "now pass."),
    ({"thread": "Example Nightly Build", "status": "failed", "waiting_for": None, "pending": None,
      "messages": [{"from": "request", "text": "Run the nightly build and report."},
                   {"from": "thread", "text": "Build failed at the bundling step:\n```\nnpm ERR! ENOSPC: no space "
                                              "left on device\n```"}], "left_out": 0},
     None, "It reports the build stopped at the bundling step because the disk ran out of space."),
    ({"thread": "Example Clean Dist Folder", "status": "waiting_on_you", "waiting_for": "command_approval",
      "pending": "rm -rf dist && pnpm build",
      "messages": [{"from": "request", "text": "Clear the old build output and rebuild."}], "left_out": 0},
     "What does it need from me?", "It wants your approval to run a command that deletes the old build folder and "
                                   "rebuilds."),
    ({"thread": "Example Orders Migration", "status": "working", "waiting_for": None, "pending": None,
      "messages": [{"from": "request", "text": "Migrate the seven billing tables to the new schema."},
                   {"from": "thread", "text": "Migrated `invoices`, `payments` and `refunds` (3 of 7). Next: "
                                              "`orders`."}], "left_out": 4},
     "Is the migration done?", "Not yet: it says three of the seven tables are migrated and it is moving on to the "
                               "orders table."),
)

# A run that ended with its work undone, in the last-reply view: the 1.7B said such work was finished (2 of 6 false
# gists per seed on the first set).
UNDONE_EXAMPLE = (
    {"thread": "Example Retry Payment Job", "status": "finished", "waiting_for": None, "pending": None,
     "messages": [{"from": "thread", "text": "I couldn't run the payment tests because the sandbox has no network "
                                             "access. I stopped before changing any files, so the retries and the "
                                             "PR are still to do."}], "left_out": 0},
    "Is the retry change in?", "No, it says it stopped before changing any files because it could not run the tests, "
                               "so the retries and the PR are still to do.")

# The same answers in at most 20 words, for a step that asks for 20.
SHORT_ANSWERS = {
    EXAMPLES[0][2]: "Yes, it says it opened PR 212 after fixing a race in the upload test, and all tests pass.",
    EXAMPLES[3][2]: "Not yet: it says three of seven tables are migrated, and orders is next.",
    UNDONE_EXAMPLE[2]: "No, it stopped before changing anything because it could not run the tests, so the retries are "
                       "still to do.",
}
NOT_SPOKEN = "`*#\\[\\]{}|<>\\\\_~\\n"

GIST_SCHEMA = {"type": "object", "properties": {"gist": {"type": "string", "pattern": "^[^`*#\\[\\]{}|<>\\\\_~\\n]+$",
                                                         "maxLength": 220}},
               "required": ["gist"], "additionalProperties": False}


WORDS = 20
SAMPLING = {"temperature": 0.1, "top_k": 50, "repeat_penalty": 1.1}
MAX_TOKENS = 60
# A summary is worth waiting for only while the status line waits (`speech.SUMMARY_TIMEOUT_S`), so the model and the
# gate together stay well inside it. Measured with all three models loaded: model p95 about 0.4 s, gate p95 0.07 s.
CHAT_TIMEOUT_S = 4.0
GATE_TIMEOUT_S = 2.0
_SPOKEN = re.compile(GIST_SCHEMA["properties"]["gist"]["pattern"])
_MAX_CHARS = GIST_SCHEMA["properties"]["gist"]["maxLength"]


def asks_for(text: str, words: int) -> str:
    return text.replace(f"at most {MAX_WORDS} words", f"at most {words} words")


def view(state: T.ThreadState) -> dict:
    """The thread as the summariser reads it: the seam's read, with its last reply as the one message."""
    pending = state.pending
    return {"thread": state.title, "status": state.activity.value,
            "waiting_for": need_of(pending.kind, pending.source) if pending is not None else None,
            "pending": pending.prompt if pending is not None else None,
            "messages": [{"from": "thread", "text": state.last_reply.text}] if state.last_reply is not None else [],
            "left_out": 0}


def messages(state: T.ThreadState, question: str | None) -> list[dict]:
    adjacent = asks_for(ADJACENT, WORDS)

    def asked(record: dict, question: str | None) -> str:
        # Records before the question, so a second question about the same thread reuses the cached prompt.
        return f"{json.dumps({'records': record, 'question': question})}\n\n{adjacent}"

    out = [{"role": "system", "content": asks_for(SYSTEM, WORDS) + VIEW_FIELDS}]
    for example, question_asked, answer in (*EXAMPLES, UNDONE_EXAMPLE):
        out += [{"role": "user", "content": asked(example, question_asked)},
                {"role": "assistant", "content": json.dumps({"gist": SHORT_ANSWERS.get(answer, answer)})}]
    out.append({"role": "user", "content": asked(view(state), question)})
    return out


def read_gist(text: str) -> str | None:
    """The `gist` of the model's JSON, only when it is what the schema allows. The grammar should already hold it
    there, but this is the last check before the summary slot. Other keys are ignored: llama.cpp's grammar does not
    enforce `additionalProperties`. Anything else, such as output cut at `MAX_TOKENS`, is no gist."""
    try:
        record = json.loads(text)
    except ValueError:
        return None
    gist = record.get("gist") if isinstance(record, dict) else None
    if not isinstance(gist, str) or len(gist) > _MAX_CHARS or not _SPOKEN.fullmatch(gist):
        return None
    return " ".join(gist.split()) or None


GATE_CONTEXT = ("A voice assistant is about to say `gist` out loud as what a coding thread reported. `last_reply` is "
                "the thread's latest reply and `status` where its run stands. They are everything the assistant "
                "knows.")
GATE_QUESTION = "Does `gist` state something about the thread that `last_reply` and `status` do not support?"
# Chosen on two sets of threads and held out on two more: 0.7 and 2.0 false gists spoken in 30, 65% and 79% of the
# true ones kept (docs/voice/local-renderer.md).
GATE_THRESHOLD = 0.10


def gate_request(state: T.ThreadState, gist: str) -> dict:
    pending = state.pending
    facts = {"thread": state.title, "status": state.activity.value,
             "pending": pending.prompt if pending is not None else None,
             "last_reply": state.last_reply.text if state.last_reply is not None else None, "gist": gist,
             "context": GATE_CONTEXT}
    return {"model": "d1.2", "state": facts,
            "questions": {"unsupported": {"type": "noul", "instructions": GATE_QUESTION,
                                          "criteria": {"true": "Yes.", "false": "No."}}}}


def unsupported(record: dict[str, Any]) -> float | None:
    """The gate's P(the gist is unsupported), or None when the decider gave no valid answer, which drops the gist."""
    return noul(record, "unsupported")
