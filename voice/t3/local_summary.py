"""LFM3-1.7B as the thread summariser: the prompt measured as S11, its JSON output, and the local d1-2B's gate.

Every piece here is what docs/voice/local-renderer.md measured on threads not read before: the last reply as JSON,
worked examples on made-up threads, at most 20 words asked for, the model card's sampling, and a JSON schema whose
`gist` cannot hold markdown or code characters. The examples teach how to read a record; none is ever spoken. The
gate asks the local d1-2B whether the last reply and status support the gist; when they may not, the gist is
dropped and the voice says the thread's status with no summary.

An approval's description is a different job: the source is the request, not a reply, and an omission is the danger,
so it has its own prompt, its own gate question and its own bar (`request_messages`, `request_gate`). It is written
and gated for one request, by its id and its stored content, and it is spoken only after that gate passed it.
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
# A thread with no reply but a pending request: the request is what the gist describes, so it is what supports it.
GATE_CONTEXT_PENDING = ("A voice assistant is about to say `gist` out loud as what a coding thread's pending request "
                        "would do. `pending` is that request and `status` where the thread's run stands. They are "
                        "everything the assistant knows.")
GATE_QUESTION_PENDING = "Does `gist` state something about the thread that `pending` and `status` do not support?"
# Chosen on two sets of threads and held out on two more: 0.7 and 2.0 false gists spoken in 30, 65% and 79% of the
# true ones kept (docs/voice/local-renderer.md).
GATE_THRESHOLD = 0.10


def gate_request(state: T.ThreadState, gist: str) -> dict:
    pending = state.pending
    facts = {"thread": state.title, "status": state.activity.value,
             "pending": pending.prompt if pending is not None else None,
             "last_reply": state.last_reply.text if state.last_reply is not None else None, "gist": gist,
             "context": GATE_CONTEXT}
    pending_only = state.last_reply is None and pending is not None
    question = GATE_QUESTION_PENDING if pending_only else GATE_QUESTION
    if pending_only:
        facts["context"] = GATE_CONTEXT_PENDING
    return {"model": "d1.2", "state": facts,
            "questions": {"unsupported": {"type": "noul", "instructions": question,
                                          "criteria": {"true": "Yes.", "false": "No."}}}}


def unsupported(record: dict[str, Any]) -> float | None:
    """The gate's P(the gist is unsupported), or None when the decider gave no valid answer, which drops the gist."""
    return noul(record, "unsupported")


# What approving a request lets a thread do. The request is shown as JSON with its own field names and no thread
# title, which the summariser borrowed facts from; the last reply is left out. Examples are made-up requests as chat
# turns and teach how to read one, never wording to be spoken.
#
# Only a request whose stored prompt is the action itself is described: a command, or a change to a file. A tool
# permission stores only the tool's name, a file read only a path, and the others nothing the voice can describe, so
# those go to the app. The choice is made on the request's source, a typed field, never on its text.
REQUEST_KINDS = {T.NeedSource.COMMAND: "a shell command", T.NeedSource.FILE_CHANGE: "a change to a file"}
REQUEST_SYSTEM = ("You help a voice assistant tell a person what they would be approving. A coding agent asks "
                  "permission to do one thing. The request is JSON: `kind` is what sort of request it is and `text` "
                  "is the request as the agent wrote it. Say in one plain spoken sentence of at most 20 words what "
                  "approving it lets the agent do. Say every effect it has, and say plainly when it deletes, "
                  "overwrites or publishes something. Use only what the request says. Describe code, file names and "
                  "paths in words; never repeat them.")
REQUEST_ADJACENT = "Say what approving this request lets the agent do, in one plain spoken sentence of at most 20 words."
REQUEST_EXAMPLES = (
    ({"kind": "a shell command", "text": "rm -f session.lock && ./start-server.sh"},
     "It wants to delete a lock file and then start the server."),
    ({"kind": "a shell command", "text": "ls -la src | head"},
     "It wants to list the first few files in the source folder."),
    ({"kind": "a change to a file", "text": "Rename function getUser to fetchUser in lib/users.ts"},
     "It wants to rename one function in a users file."),
    ({"kind": "a change to a file", "text": "Add a section about caching to docs/guide.md"},
     "It wants to add a new section about caching to the guide."),
)
REQUEST_SCHEMA = {"type": "object", "properties": {"gist": {"type": "string", "maxLength": 220}},
                  "required": ["gist"], "additionalProperties": False}

REQUEST_GATE_CONTEXT = ("A voice assistant will tell a person what approving a coding agent's `request` would let the "
                        "agent do, in `description`. The person will say yes on the strength of it alone, so it must "
                        "not leave out or soften anything that deletes, overwrites, installs, publishes or sends.")
REQUEST_GATE_QUESTION = "Is `description` an accurate and complete account of what `request` would do?"
# Chosen on the seen made-up requests (docs/voice/live-reply-check.md): the lowest score of a misleading description
# there was 0.129, a harmless-sounding account of a deletion, so a bar above that lets it through.
REQUEST_THRESHOLD = 0.10


# What a thread the voice started was started for, to tell it from another started in the same project. It is written
# from the request the person spoke and nothing else, and passed by its own gate question against that request.
ACCOUNT_SYSTEM = ("You help a voice assistant tell two coding threads apart. A person spoke a request that started a "
                  "coding thread. The request is JSON: `request` is their words as heard, hesitant and with filler. "
                  "Say in one plain spoken phrase of at most 10 words what the thread was started for, as a noun "
                  "phrase that could follow the word for. Use only what the request says. Leave out filler, and never "
                  "repeat file names, paths or code.")
ACCOUNT_ADJACENT = "Say what the thread was started for, as a plain phrase of at most 10 words."
ACCOUNT_EXAMPLES = (
    ("Uhm, so I'd like, uh, a thread that goes through the bakery menu and, uh, fixes the prices. They're all off by a "
     "dollar.", "fixing the prices on the bakery menu"),
    ("Okay, um, can you start something on the reef card game? Um, the shuffle seems to deal the same hand twice.",
     "the reef card game dealing the same hand twice"),
)
ACCOUNT_GATE_CONTEXT = ("A voice assistant will tell a person which coding thread it means by saying what the thread was "
                        "started for, in `account`. `request` is what the person spoke to start it. They are everything "
                        "the assistant knows.")
ACCOUNT_GATE_QUESTION = ("Does `account` state something about what the thread was started for that `request` does not "
                         "support?")
ACCOUNT_THRESHOLD = 0.10


def account_messages(instruction: str) -> list[dict]:
    def asked(request: str) -> str:
        return f"{json.dumps({'request': request})}\n\n{ACCOUNT_ADJACENT}"

    out = [{"role": "system", "content": ACCOUNT_SYSTEM}]
    for request, account in ACCOUNT_EXAMPLES:
        out += [{"role": "user", "content": asked(request)},
                {"role": "assistant", "content": json.dumps({"gist": account})}]
    out.append({"role": "user", "content": asked(instruction)})
    return out


def account_gate(instruction: str, account: str) -> dict:
    return {"model": "d1.2", "state": {"context": ACCOUNT_GATE_CONTEXT, "request": instruction, "account": account},
            "questions": {"unsupported": {"type": "noul", "instructions": ACCOUNT_GATE_QUESTION,
                                          "criteria": {"true": "Yes.", "false": "No."}}}}


def describable(need: T.PendingNeed) -> bool:
    """Whether the voice can describe this request: an approval whose stored prompt is a command or a file change."""
    return need.kind is T.PendingKind.APPROVAL and need.source in REQUEST_KINDS


def request_view(need: T.PendingNeed) -> dict:
    return {"kind": REQUEST_KINDS[need.source], "text": need.prompt}


def request_messages(need: T.PendingNeed) -> list[dict]:
    def asked(request: dict) -> str:
        return f"{json.dumps({'request': request})}\n\n{REQUEST_ADJACENT}"

    out = [{"role": "system", "content": REQUEST_SYSTEM}]
    for request, description in REQUEST_EXAMPLES:
        out += [{"role": "user", "content": asked(request)},
                {"role": "assistant", "content": json.dumps({"gist": description})}]
    out.append({"role": "user", "content": asked(request_view(need))})
    return out


def read_description(text: str) -> str | None:
    """The `gist` of the model's JSON, or the text itself from a model that answered in prose. What it says is the
    gate's to judge: nothing here decides whether it is good."""
    try:
        record = json.loads(text)
    except ValueError:
        record = {"gist": text}
    gist = record.get("gist") if isinstance(record, dict) else None
    if not isinstance(gist, str) or len(gist) > _MAX_CHARS:
        return None
    return " ".join(gist.split()) or None


def request_gate(need: T.PendingNeed, description: str) -> dict:
    return {"model": "d1.2", "state": {"context": REQUEST_GATE_CONTEXT, "request": request_view(need),
                                       "description": description},
            "questions": {"accurate": {"type": "noul", "instructions": REQUEST_GATE_QUESTION,
                                       "criteria": {"true": "Yes.", "false": "No."}}}}


def doubt(record: dict[str, Any]) -> float | None:
    """The gate's P(the description is not accurate and complete), or None when the decider gave no valid answer."""
    accurate = noul(record, "accurate")
    return None if accurate is None else 1 - accurate
