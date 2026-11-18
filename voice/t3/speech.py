"""The voice's own words for T3 acts: a free text model renders them, and code checks each line.

Code says what is true as typed acts (voice/t3/acts.py). `Speech.render` asks the text model for one or two natural
spoken sentences that say those acts, fitted to the last few turns of the call, and keeps the line only when
`acts.check` and then the decider pass it (voice/t3/verify.py), as a RenderedLine. `Speech.note` is the same acts as
a private direction for LFM3, the fallback when the text model fails, and LFM3's reply goes through the same
`Speech.accept`. No line is ever a fixed string.

A thread's summary is written here too, only when the voice is about to speak about that thread: an answer to an ask,
a done, or a need. It reads the thread through the seam (status and need come from code, plus the full last reply),
writes a gist into the act's `summary` slot, and is cached per thread, read sequence and question. A speculative
start may begin it while Mason is still asking.

Every model request goes through one `Lane`: one request in flight, on the lane's own thread, so no request runs
under the T3 lock and none waits on another caller's slot past its own timeout. The lane's model is chosen by
`$VOICE_RENDERER` (`text_model`): `particle-1`, the free cloud model and the default, or `local`, an OpenAI-compatible
llama-server on this machine (docs/voice/local-renderer.md).
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from voice.t3 import types as T
from voice.t3.pick.model import allowed_model
from voice.t3.acts import QUESTIONS, RULES, SUMMARISED, Act, Kind, check, forbidden_claims, normalise
from voice.t3.verify import Verdict

RENDER_TIMEOUT_S = 8.0
SUMMARY_TIMEOUT_S = 10.0
TURNS = 3

NEED_WORDS = ("`need` is what it waits for: command_approval (approve a command it wants to run), file_change "
              "(approve file edits), file_read, permission, tool_input, question (answer its question), link or "
              "secret or card (something only the app can take), or unanswerable (a request no one can answer any "
              "more, so they can stop it in the app). Describe the need in a few words of your own, using the "
              "`prompt` (or `pending`) only to say what it is about, such as the program a command runs. Never read "
              "the prompt out, and never say a command line, a flag, a path or code.")
# What each act means, for the model that words it. These are directions, never wording to repeat.
MEANINGS: dict[Kind, str] = {
    Kind.CONFIRM_LAUNCH: "Ask whether to start a new coding thread for their request in this project. Nothing has "
                         "started yet.",
    Kind.ASK_WHICH: "Ask which of these they mean. Name every candidate.",
    Kind.NO_PROJECT: "No project matches what they named. Name the projects a thread can start in.",
    Kind.LAUNCH_PENDING: "T3 was asked to start the thread and has not confirmed it yet.",
    Kind.LAUNCHED: "The thread started in this project.",
    Kind.LAUNCH_UNCONFIRMED: "T3 was asked to start the thread but never confirmed it. They can check the app.",
    Kind.NO_THREAD: "No thread matches what they asked about. `recent` is how many recent threads were checked, "
                    "and `other` means there is no other thread besides the one they turned down.",
    Kind.CONFIRM_THREAD: "Ask whether this is the thread they mean.",
    Kind.CONFIRM_TELL: "Ask whether to send this message to the thread. Nothing is sent yet.",
    Kind.CONFIRM_STOP: "Ask whether to stop the work this thread is doing. Nothing is stopped yet.",
    Kind.CANCELLED: "Acknowledge that you will not do what was asked (`op`: start, ask, tell or stop).",
    Kind.THREAD_STATUS: "Answer how the thread is going: say its status, then the summary, in answer to what they "
                        "asked. When `need` is set, the thread is waiting on them: " + NEED_WORDS,
    Kind.TELL_PENDING: "T3 was asked to pass the message on and has not confirmed it yet.",
    Kind.SENT_FOLLOWUP: "The message reached the thread.",
    Kind.TELL_UNCONFIRMED: "T3 was asked to pass the message on but never confirmed it arrived. They can check the "
                           "app.",
    Kind.HELD_FOLLOWUP: "The thread is still working, so the message is held and will be sent when its run ends.",
    Kind.DROPPED_FOLLOWUP: "The held message will not be sent.",
    Kind.STOPPING: "T3 was asked to stop the thread's run. It has not stopped yet.",
    Kind.NOTHING_RUNNING: "The thread has no run going, so there was nothing to stop.",
    Kind.STOP_UNCONFIRMED: "The voice could not reach T3 to stop the thread. They can stop it in the app.",
    Kind.RUN_UPDATE: "The thread's run is now in this status (running or queued).",
    Kind.REPLIED: "The thread posted this reply while it works. Give its gist in one sentence. Attribute any claims "
                  "about work or results to the thread as what it says.",
    Kind.NEEDS_YOU: "The thread is waiting on them. " + NEED_WORDS + " `after_end` means the run ended and the "
                    "need is still open.",
    Kind.NEED_RESOLVED: "A request the thread had made of them was settled (answered, expired or cancelled).",
    Kind.DONE: "The thread's run ended with this status. Say the status, then the summary.",
    Kind.THREAD_DELETED: "The thread was deleted.",
    Kind.LOST_TRACK: "The voice lost track of the thread. It is still in the app.",
    Kind.THREAD_GONE: "The voice can no longer see the thread.",
    Kind.WATCH_QUEUED: "The voice already follows `following` threads and will follow this one when one ends.",
    Kind.UNREACHABLE: "The voice cannot reach T3 right now.",
    Kind.SEAM_DOWN: "The voice stopped following their threads, which are still in the app. `reason`: connection "
                    "(the connection to T3 was lost), access (T3 no longer accepts the voice's access) or seam_off.",
    Kind.REFUSED: "T3 refused to do what was asked (`op`). Say why, from `reason`.",
    Kind.LEGACY_UPDATE: "News about one of their threads, saved before an upgrade as an older line, `text`. Say the "
                        "same news in your own words, adding nothing. Attribute any work or results to the thread.",
}
assert set(MEANINGS) == set(Kind)

CLAIM_WORDS = {"finished": "that the thread is finished, done or completed",
               "stopped": "that the thread stopped or was cancelled", "started": "that the thread started",
               "sent": "that the message was sent or passed on",
               "working": "that the thread is still working or running"}

RENDER_SYSTEM = (
    "You write the next thing a voice assistant says out loud, to the person it is talking to on a call, about "
    "their coding threads in an app called T3. You get typed facts called acts. Use turns only for context. "
    "Say all the acts, in order, as natural speech: one short sentence per act, two at most, and under 35 words in "
    "all. Use only the facts given and never add one. Say every value under keep_exact word for word, and say a "
    "thread's name once. Ask only a question the acts call for. When ask is true, end with that question. "
    "Never say the things listed under never_say, and "
    "never mention a summary, records or notes. Keep attribution to the thread for any work or results it reports. "
    "Speak as \"I\", to \"you\". No lists, markdown, quotes around "
    "names, or ids. Reply with the spoken words only."
)
SUMMARY_SYSTEM = (
    "You read the records of one coding thread and write a gist for a voice assistant to say out loud. Answer the "
    "question when there is one, else say what the thread reported. One spoken sentence of at most 25 words. "
    "Attribute claims from the last reply to the thread. Use wording like \"it says\" or \"the thread reported\" "
    "for any claimed work or results. Also, never say the thread did what its name says unless the last reply says "
    "it. State "
    "only what the records say: no guesses, no advice, no file paths or code unless asked. Do not say whether the "
    "thread is running, finished, failed or stopped, because the assistant says that separately. The thread's name "
    "is only a name, not evidence of what was done. If the records do not answer the question, say what they do "
    "show. When the run has ended, "
    "say what its last reply said, not what it is doing now. When last_reply is null, say what it waits on, from "
    "status and pending. Reply with the gist only."
)
TITLE_KEY = "name (only a name, not evidence of what was done)"

_QUOTES = "\"'\N{LEFT DOUBLE QUOTATION MARK}\N{RIGHT DOUBLE QUOTATION MARK}"


def assert_free(model: str) -> None:
    if not allowed_model(model):
        raise ValueError(f"refusing a non-free model id: {model}")


class LocalText:
    """A text model on a local llama-server's OpenAI-compatible chat route, the same `complete(messages)` record as
    the cloud client. No request leaves the machine, so no quota or `:free` rule applies. LFM3 thinks by default, and
    with the template's thinking switch off it still opened `<think>` on render prompts and spent its whole token
    budget there (measured), so the `<think>` token is banned outright."""

    model = "local"

    def __init__(self, url: str | None = None, timeout_s: float = RENDER_TIMEOUT_S, max_tokens: int = 200):
        self.url = (url or os.environ.get("VOICE_RENDERER_URL", "http://127.0.0.1:8098")).rstrip("/")
        self.timeout_s, self.max_tokens = timeout_s, max_tokens
        self.calls = 0
        self.ledger: list[dict] = []
        self._ban: list | None = None

    def _post(self, path: str, body: dict) -> dict:
        request = urllib.request.Request(f"{self.url}{path}", data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                return json.load(response)
        except (urllib.error.URLError, OSError) as exc:
            raise ConnectionError(f"the local text model at {self.url} failed: {exc}") from exc

    def _think_ban(self) -> list:
        if self._ban is None:
            tokens = self._post("/tokenize", {"content": "<think>", "parse_special": True}).get("tokens") or []
            if len(tokens) != 1:
                raise RuntimeError(f"the local text model splits <think> into {len(tokens)} tokens, so it can't be banned")
            self._ban = [[tokens[0], False]]
        return self._ban

    def complete(self, messages) -> dict:
        body = {"messages": messages, "temperature": 0, "max_tokens": self.max_tokens,
                "chat_template_kwargs": {"enable_thinking": False}, "logit_bias": self._think_ban()}
        self.calls += 1
        started = time.perf_counter()
        payload = self._post("/v1/chat/completions", body)
        ms = round((time.perf_counter() - started) * 1000, 1)
        choice = (payload.get("choices") or [{}])[0]
        usage = payload.get("usage") or {}
        self.ledger.append({"ms": ms, "prompt_tokens": usage.get("prompt_tokens"),
                            "completion_tokens": usage.get("completion_tokens")})
        return {"text": (choice.get("message") or {}).get("content") or "", "ms": ms,
                "model": payload.get("model", self.model), "usage": usage, "finish_reason": choice.get("finish_reason")}


RENDERERS = ("particle-1", "local")


def text_model(name: str | None = None, cloud: Callable[[], Any] | None = None):
    """The lane's model by `$VOICE_RENDERER`: `local` is LocalText; `particle-1`, the default, is `cloud()`, which
    returns None when no key is configured."""
    name = name or os.environ.get("VOICE_RENDERER") or "particle-1"
    if name not in RENDERERS:
        raise ValueError(f"VOICE_RENDERER must be one of {RENDERERS}, not {name!r}")
    if name == "local":
        return LocalText()
    return cloud() if cloud is not None else None


class Lane:
    """One model request in flight for every voice caller: the picker, the renderer, and the summaries."""

    def __init__(self, client):
        self._check(client)
        self.client = client
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="t3-text")
        self.lock = threading.Lock()
        self.calls = 0

    @staticmethod
    def _check(client) -> None:
        if not isinstance(client, LocalText):
            assert_free(client.model)

    def submit(self, messages) -> Future:
        self._check(self.client)
        with self.lock:
            self.calls += 1
        return self.pool.submit(self.client.complete, messages)

    def call(self, messages, timeout: float) -> dict:
        future = self.submit(messages)
        try:
            return future.result(timeout=timeout)
        except FutureTimeout:
            # A queued request is dropped; one already running keeps the slot until its HTTP call returns.
            future.cancel()
            raise TimeoutError("the voice text model did not answer in time") from None

    def picker(self, timeout: float) -> Any:
        lane = self

        class Picker:
            def complete(self, messages):
                return lane.call(messages, timeout)

        return Picker()


def _turns(turns: Iterable[dict]) -> list[dict]:
    """The call's last few turns as the prompt shows them: what the person said and what the assistant said."""
    out = []
    for turn in list(turns)[-TURNS:]:
        item = {}
        if turn.get("user"):
            item["person"] = turn["user"]
        said = turn.get("assistant")
        if said:
            item["assistant"] = " ".join(said) if isinstance(said, (list, tuple)) else said
        if item:
            out.append(item)
    return out


def _clean(text: str) -> str:
    text = " ".join((text or "").split())
    if len(text) > 1 and text[0] in _QUOTES and text[-1] in _QUOTES:
        text = text[1:-1].strip()
    return text


def kept_exact(acts: Iterable[Act]) -> list[str]:
    values = []
    for a in acts:
        for name in RULES[a.kind].named:
            value = a.slots.get(name)
            if value is None:
                continue
            values.extend(str(v) for v in value) if isinstance(value, (list, tuple)) else values.append(str(value))
    return values


def never_say(acts: Iterable[Act]) -> list[str]:
    """The status claims a line for these acts must not make, as words the prompt warns against."""
    return [CLAIM_WORDS[claim] for claim in sorted(forbidden_claims(acts))]


def act_facts(a: Act) -> dict:
    return {"act": a.kind.value, "means": MEANINGS[a.kind], "facts": dict(a.slots)}


@dataclass
class Rendered:
    """One wording of some acts. `line` is set only when both the slot check and the decider passed it."""

    line: RenderedLine | None
    problems: list[str] = field(default_factory=list)
    raw: str | None = None
    error: str | None = None
    verdict: Verdict | None = None

    @property
    def text(self) -> str | None:
        return self.line.text if self.line is not None else None


_MINT = object()


@dataclass(frozen=True)
class RenderedLine:
    """A line the voice may speak about T3 work. Only `Speech.accept` makes one, after the slot check and the
    decider both pass it, and every T3 speaking entry point takes only this type, so no plain string can be said."""

    text: str
    acts: tuple[Act, ...]
    worded_by: str
    verdict: Verdict
    mint: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.mint is not _MINT:
            raise TypeError("a RenderedLine comes only from Speech.accept, after its line passed both checks")


def speakable(line: Any, entry_point: str) -> RenderedLine:
    """`line` when it is a RenderedLine; anything else, a plain string above all, raises."""
    if not isinstance(line, RenderedLine):
        raise TypeError(f"{entry_point} speaks only a RenderedLine, never {type(line).__name__}")
    return line


@dataclass
class Gist:
    """One cached summary. `partial` while only a speculative start from words still being spoken has asked for it."""

    future: Future
    partial: bool


@dataclass
class Speech:
    """`lane` is None when no model is configured: every render then fails and the act goes to LFM3 or waits.
    `read(thread_id)` reads a thread through the seam, or returns None when it cannot. `decider` checks each line
    against its acts (voice/t3/verify.py); with none, no line passes."""

    lane: Lane | None
    read: Callable[[str], T.ThreadState | None]
    decider: Any = None
    states: dict = field(default_factory=dict)
    summaries: dict = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def remember(self, state: T.ThreadState) -> None:
        with self.lock:
            self.states[(state.thread_id, state.as_of_sequence)] = state

    def prefetch(self, thread_id: str, question: str | None = None) -> None:
        """A speculative start: read the thread now and begin its summary on the lane, without waiting. The words so
        far may not be the whole question, so the entry is partial: only a final question that normalises to the
        same words reuses it."""
        state = self.read(thread_id)
        if state is not None:
            self.remember(state)
            self._summary(state, question, partial=True)

    def _summary(self, state: T.ThreadState, question: str | None, partial: bool = False) -> Future | None:
        if self.lane is None:
            return None
        key = (state.thread_id, state.as_of_sequence, normalise(question or ""))
        with self.lock:
            gist = self.summaries.get(key)
            if gist is None or (gist.future.done() and gist.future.exception() is not None):
                gist = self.summaries[key] = Gist(self.lane.submit(summary_messages(state, question)), partial)
            elif not partial:
                gist.partial = False
        return gist.future

    def summary(self, state: T.ThreadState, question: str | None = None) -> str | None:
        future = self._summary(state, question)
        if future is None:
            return None
        try:
            record = future.result(timeout=SUMMARY_TIMEOUT_S)
        except Exception:
            return None
        text = _clean(record.get("text", ""))
        return text or None

    def fill(self, acts: tuple[Act, ...], thread_id: str | None, sequences: tuple[int, ...] = ()):
        """The acts with the read's title and the thread's summary filled in, and the read's sequence as evidence."""
        if thread_id is None or not any(a.kind in SUMMARISED and "summary" not in a.slots for a in acts):
            return acts, ()
        state, read = None, ()
        if any(a.kind is Kind.THREAD_STATUS for a in acts):
            with self.lock:
                state = next((self.states[(thread_id, s)] for s in sequences if (thread_id, s) in self.states), None)
        if state is None:
            state = self.read(thread_id)
            if state is None:
                return acts, ()
            self.remember(state)
            read = (state.as_of_sequence,)
        question = next((a.slots.get("asked") for a in acts if a.slots.get("asked")), None)
        gist = self.summary(state, question)
        filled = []
        for a in acts:
            if a.kind in SUMMARISED:
                slots = {"summary": gist} if gist else {}
                if "thread" not in a.slots and state.title:
                    slots["thread"] = state.title
                a = a.with_slots(**slots)
            filled.append(a)
        return tuple(filled), read

    def render(self, acts: Iterable[Act], turns: Iterable[dict] = (), problems: Iterable[str] = (),
               refused: Iterable[str] = (), attempt: int = 0) -> Rendered:
        acts = tuple(acts)
        if self.lane is None:
            return Rendered(None, error="no text model")
        try:
            record = self.lane.call(render_messages(acts, turns, problems, refused, attempt), RENDER_TIMEOUT_S)
        except Exception as exc:
            return Rendered(None, error=repr(exc))
        return self.accept(acts, _clean(record.get("text", "")), "text_model", record.get("text"))

    def accept(self, acts: Iterable[Act], text: str, worded_by: str, raw: str | None = None) -> Rendered:
        """`text` as a RenderedLine when code's slot check and then the decider pass it for `acts`."""
        acts = tuple(acts)
        raw = text if raw is None else raw
        found = check(text, acts)
        if found:
            return Rendered(None, found, raw)
        if self.decider is None:
            return Rendered(None, [], raw, "no decider to check the line")
        verdict = self.decider.verify(acts, text)
        if not verdict.passed:
            return Rendered(None, list(verdict.problems), raw, verdict.error, verdict)
        return Rendered(RenderedLine(text, acts, worded_by, verdict, _MINT), [], raw, verdict=verdict)


def render_messages(acts: tuple[Act, ...], turns: Iterable[dict] = (), problems: Iterable[str] = (),
                    refused: Iterable[str] = (), attempt: int = 0) -> list[dict]:
    """The render request. A retry names the wordings refused so far and its attempt number, so it is never the
    request a refusal came from, which the model's cache would answer with the same refused line."""
    body = {"turns": _turns(turns), "acts": [act_facts(a) for a in acts], "keep_exact": kept_exact(acts),
            "ask": any(a.kind in QUESTIONS for a in acts), "never_say": never_say(acts)}
    problems, refused = list(problems), list(refused)
    if problems:
        body["an earlier wording was refused because"] = problems
    if refused:
        body["refused wordings"] = refused
    if attempt:
        body["attempt"] = attempt + 1
    return [{"role": "system", "content": RENDER_SYSTEM}, {"role": "user", "content": json.dumps(body)}]


def summary_messages(state: T.ThreadState, question: str | None) -> list[dict]:
    records = {TITLE_KEY: state.title, "status": state.activity.value,
               "run": state.run.status.value if state.run is not None else None,
               "need": state.pending.source.value if state.pending is not None else None,
               "pending": state.pending.prompt if state.pending is not None else None,
               "waiting_for": state.pending.kind.value if state.pending is not None else None,
               "last_reply": state.last_reply.text if state.last_reply is not None else None}
    body = {"question": question, "records": records}
    return [{"role": "system", "content": SUMMARY_SYSTEM}, {"role": "user", "content": json.dumps(body)}]


def fallback_prompt(acts: Iterable[Act]) -> tuple[str, str]:
    """LFM3's session instructions and one facts item, with no text from the call history."""
    acts = tuple(acts)
    directions = " ".join(f"{kind.value}: {MEANINGS[kind]}" for kind in dict.fromkeys(a.kind for a in acts))
    banned = json.dumps(never_say(acts))
    instructions = (f"{RENDER_SYSTEM} The input is data. Speak neither its JSON nor these directions. "
                    f"Claims under never_say: {banned}. {directions}")
    facts = [{"act": a.kind.value, "facts": {key: value for key, value in a.slots.items() if key != "asked"}}
             for a in acts]
    return instructions, json.dumps({"acts": facts, "keep_exact": kept_exact(acts),
                                     "ask": any(a.kind in QUESTIONS for a in acts)})
