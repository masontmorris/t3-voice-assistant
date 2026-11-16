"""The draft author behind a swappable transport.

`author_request` turns a DraftInput into a provider-neutral request. A `Transport` sends it to one provider and
returns an `AuthorReply`. `CachedAuthor` wraps any transport with the brief's rules: a response cache that never
re-requests, an attempt budget, one request in flight, backoff on 429, a stop when more than 20% of attempts draw a
429, and a stop on HTTP 402 or on any error that mentions a usage limit or billing. The author runs on Mason's
subscription, so usage is recorded but does not stop the run.
"""

import hashlib
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

from voice.delegation.draft import DRAFT_SCHEMA, placeholder
from voice.segmentation.client import Pacer

BUDGET = 150
MAX_429_SHARE = 0.2
MIN_ATTEMPTS_FOR_SHARE = 5
MAX_ATTEMPTS = 5
LIMIT_WORDS = ("usage limit", "limit reached", "quota", "billing", "credit", "payment", "insufficient", "subscription")

SYSTEM = (
    "You write the task prompt that a coding and research agent receives. A voice assistant heard a user ask for one "
    "task. Restate exactly that task for the agent. Do not add steps, actions, or goals the user did not ask for. "
    "Do not drop anything the user asked for. Put details the user gave into the instruction or the constraints. "
    "Put anything the agent would have to guess into open_questions. Answer with one JSON object only."
)


@dataclass(frozen=True)
class AuthorRequest:
    system: str
    user: str
    schema: dict
    max_tokens: int = 4096
    temperature: float = 0.0


@dataclass(frozen=True)
class AuthorReply:
    """One attempt. `sent` is the request body as the transport sent it, without headers."""
    status: int | None
    content: dict | None
    ms: float
    error: str | None = None
    retry_after_s: float | None = None
    sent: dict | None = None
    served_model: str | None = None
    usage: dict | None = None
    finish_reason: str | None = None
    meta: dict | None = None


class Transport(Protocol):
    model: str

    def send(self, request: AuthorRequest) -> AuthorReply: ...


class AuthorStopped(RuntimeError):
    pass


def author_request(source):
    deps = "".join(f"\n- It needs the result of {d}. Write {placeholder(d)} where that result goes."
                   for d in source.depends_on) or "\n- None."
    effect = ("This task acts on the outside world. The agent must confirm with the user before acting."
              if source.side_effect else "This task has no effect outside the conversation.")
    user = (f"The user's request for this task:\n{source.instruction}\n\n"
            f"Everything the user said in this utterance, for context only:\n{source.context}\n\n"
            f"Dependencies:{deps}\n\n{effect}\n\n"
            "Fields: instruction (the task, in one or two sentences), context (facts from the utterance the agent "
            "needs, or an empty string), constraints, success_criteria (at least one), open_questions.")
    return AuthorRequest(SYSTEM, user, DRAFT_SCHEMA)


def request_hash(model, request):
    return hashlib.sha256(json.dumps([model, asdict(request)], sort_keys=True).encode()).hexdigest()[:24]


def limit_hit(reply):
    return reply.status == 402 or any(word in (reply.error or "").lower() for word in LIMIT_WORDS)


class CachedAuthor:
    def __init__(self, transport, directory, budget=BUDGET):
        self.transport, self.directory, self.budget = transport, Path(directory), budget
        self.ledger = self.directory / "calls.jsonl"
        self.pacer = Pacer()
        self._last = None

    def _attempts(self):
        if not self.ledger.exists():
            return []
        return [json.loads(line) for line in self.ledger.read_text(encoding="utf-8").splitlines()]

    def write(self, request, label=""):
        """The cached record for this request, calling the transport at most once per distinct request."""
        path = self.directory / f"draft-{request_hash(self.transport.model, request)}.json"
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        self.directory.mkdir(parents=True, exist_ok=True)
        reply = None
        for attempt in range(MAX_ATTEMPTS):
            attempts = self._attempts()
            if len(attempts) >= self.budget:
                raise AuthorStopped(f"budget of {self.budget} attempts used")
            limited = sum(a["status"] == 429 for a in attempts)
            if len(attempts) >= MIN_ATTEMPTS_FOR_SHARE and limited / len(attempts) > MAX_429_SHARE:
                raise AuthorStopped(f"{limited} of {len(attempts)} attempts drew a 429")
            if self._last is not None and self.pacer.gap_s:
                time.sleep(max(0.0, self.pacer.gap_s - (time.monotonic() - self._last)))
            self._last = time.monotonic()
            reply = self.transport.send(request)
            with self.ledger.open("a", encoding="utf-8") as ledger:
                ledger.write(json.dumps({"label": label, "model": self.transport.model, "status": reply.status,
                                         "ms": round(reply.ms, 1), "usage": reply.usage, "error": reply.error}) + "\n")
            if limit_hit(reply):
                raise AuthorStopped(f"status {reply.status} on {label}: {reply.error}")
            if reply.status == 429:
                self.pacer.on_429(reply.retry_after_s)
                continue
            if reply.status is None or reply.status >= 500:
                time.sleep(2 ** (attempt + 1))
                continue
            self.pacer.on_success()
            break
        record = {"label": label, "model": self.transport.model, "request": asdict(request), **asdict(reply)}
        if reply.status == 200:
            path.write_text(json.dumps(record, indent=1) + "\n", encoding="utf-8")
        return record
