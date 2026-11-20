"""Drafting and checking speech about T3 acts.

Code says what is true as typed acts (voice/t3/acts.py). `Speech.render` asks the text model for one or two natural
spoken sentences that say those acts. `Speech.accept` checks both the draft and the call's reply with `acts.check`
and then the decider (voice/t3/verify.py). A passing line becomes a RenderedLine.

A thread's summary is written here too, only when the voice is about to speak about that thread: an answer to an ask,
a done, or a need. It reads the thread through the seam (status and need come from code, plus the full last reply),
writes a gist into the act's `summary` slot, and is cached per thread, read sequence and question. A speculative
start may begin it while Mason is still asking.

The summaries use the text-model lane. The live renderer uses a separate text-only realtime lane
by default, with the text model available as an override.
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

from voice.t3 import local_summary as LS
from voice.t3 import types as T
from voice.t3.pick.model import allowed_model
from voice.t3.acts import (NOTHING_NEW, QUESTIONS, RULES, SUMMARISED, Act, Kind, check, check_names, forbidden_claims, normalise,
                           recites)
from voice.t3.realtime_writer import RealtimeWriter
from voice.t3.verify import Decider, Verdict

RENDER_TIMEOUT_S = 8.0
SUMMARY_TIMEOUT_S = 10.0
assert LS.CHAT_TIMEOUT_S + LS.GATE_TIMEOUT_S < SUMMARY_TIMEOUT_S
TURNS = 3

NEED_WORDS = ("`need` is what it waits for: command_approval (approve a command it wants to run), file_change "
              "(approve file edits), file_read, permission, tool_input, question (answer its question), link or "
              "secret or card (something only the app can take), or unanswerable (a request no one can answer any "
              "more, so they can stop it in the app). Describe the need in a few words of your own, using the "
              "`prompt` (or `pending`) only to say what it is about, such as the program a command runs. Never read "
              "the prompt out, and never say a command line, a flag, a path or code.")
# What each act means, for the model that words it. These are directions, never wording to repeat.
MEANINGS: dict[Kind, str] = {
    Kind.CONFIRM_LAUNCH: "Ask whether to start a new coding thread in this project. Nothing has started yet. Do not "
                         "repeat what they asked for.",
    Kind.ASK_WHICH: "Ask which of these they mean, saying whether `of` is project or thread. Name every candidate.",
    Kind.NO_PROJECT: "In one sentence, tell them you can't find any project to use.",
    Kind.LAUNCH_PENDING: "T3 was asked to start the thread and has not confirmed it yet.",
    Kind.LAUNCHED: "A new thread started in this project.",
    Kind.LAUNCH_UNCONFIRMED: "T3 was asked to start the thread but never confirmed it. They can check the app.",
    Kind.NO_THREAD: "No thread matches what they asked about. `recent` is how many recent threads were checked, "
                    "and `other` means there is no other thread besides the one they turned down.",
    Kind.CONFIRM_THREAD: "Ask whether the existing thread named in thread is the one they mean.",
    Kind.CONFIRM_TELL: "Ask whether to send this message to the thread. Say the message exactly as given, in quotes, "
                        "leaving words like my and I as they are. Nothing is sent yet.",
    Kind.CONFIRM_STOP: "Ask whether to stop the work this thread is doing. Nothing is stopped yet.",
    Kind.SAME_TITLE: "More than one of their threads has this name, so you cannot tell which one they mean and did "
                     "nothing. `op` is what they asked for: tell (send it a message) or stop. They can do it in the "
                     "app.",
    Kind.CANCELLED: "Acknowledge that you will not do what was asked (`op`: start, ask, tell or stop). Name the "
                    "thread only when the record gives its title.",
    Kind.THREAD_STATUS: "Answer how the thread is going: say its status, then the summary, in answer to what they "
                        "asked. When `since_last_asked` is set, say first that there is nothing new since they last "
                        "heard, and do not offer the status as news, but still give the summary. When `need` is set, "
                        "the thread is waiting on them: " + NEED_WORDS,
    Kind.TELL_PENDING: "T3 was asked to pass the message on and has not confirmed it yet.",
    Kind.SENT_FOLLOWUP: "The message reached the thread.",
    Kind.TELL_UNCONFIRMED: "T3 was asked to pass the message on but never confirmed it arrived. They can check the "
                           "app.",
    Kind.HELD_FOLLOWUP: "The thread is still working, so the message is held and will be sent when its run ends.",
    Kind.DROPPED_FOLLOWUP: "The held message will not be sent.",
    Kind.STOPPING: "T3 was asked to stop the thread's run. It has not stopped yet.",
    Kind.NOTHING_RUNNING: "The thread has no run going, so there was nothing to stop.",
    Kind.STOP_UNCONFIRMED: "Say that you couldn't reach T3 to stop the thread. Include its exact title in that "
                           "sentence.",
    Kind.RUN_UPDATE: "The thread's run is now in this status (running or queued).",
    Kind.REPLIED: "The thread posted this reply while it works. Give its gist in one sentence. Attribute any claims "
                  "about work or results to the thread as what it says.",
    Kind.NEEDS_YOU: "The thread is waiting on them. " + NEED_WORDS + " `after_end` means the run ended and the "
                    "need is still open.",
    Kind.NEED_RESOLVED: "A request the thread had made of them was settled (answered, expired or cancelled).",
    Kind.DONE: "The thread's run ended with this status. Say the status, then the summary.",
    Kind.THREAD_DELETED: "The thread was deleted.",
    Kind.LOST_TRACK: "Say that you lost track of the named thread. Make no claim about where it is or whether it "
                     "still exists.",
    Kind.THREAD_GONE: "The voice can no longer see the thread.",
    Kind.WATCH_QUEUED: "The voice already follows `following` threads and will follow this one when one ends.",
    Kind.UNREACHABLE: "The voice cannot reach T3 right now.",
    Kind.SEAM_DOWN: "The voice stopped following their threads, which are still in the app. `reason`: connection "
                    "(the connection to T3 was lost), access (T3 no longer accepts the voice's access) or seam_off.",
    Kind.REFUSED: "Say what T3 refused to do in plain words. For `op` tell, say it refused to send the follow-up "
                  "message. For start, ask, or stop, say it refused to start, look up, or stop the thread, "
                  "respectively. If the record gives a reason, attribute it to T3 as its reason for refusing.",
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
    budget there (measured), so the `<think>` token is banned outright. As the summariser it writes in the form
    measured for it (`summarise`), checked by the local d1-2B at `gate_url`."""

    model = "local"

    def __init__(self, url: str | None = None, timeout_s: float = RENDER_TIMEOUT_S, max_tokens: int = 200,
                 gate_url: str | None = None):
        self.url = (url or os.environ.get("VOICE_RENDERER_URL", "http://127.0.0.1:8098")).rstrip("/")
        self.gate_url = gate_url or os.environ.get("VOICE_SUMMARY_GATE_URL", "http://127.0.0.1:8100")
        self.timeout_s, self.max_tokens = timeout_s, max_tokens
        self.calls = 0
        self.ledger: list[dict] = []
        self._ban: list | None = None
        self._gate = None
        self._reuse: Decider | None = None

    def _post(self, path: str, body: dict, timeout_s: float | None = None) -> dict:
        request = urllib.request.Request(f"{self.url}{path}", data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=timeout_s or self.timeout_s) as response:
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
        return self._chat({"messages": messages, "temperature": 0, "max_tokens": self.max_tokens})

    def summarise(self, state: T.ThreadState, question: str | None) -> dict:
        """The thread's gist as measured for this model: S11's prompt, JSON out, then the gate. `text` is empty when
        the model wrote no gist or the gate did not pass it, so the status is said without a summary."""
        record = self._chat({"messages": LS.messages(state, question), "max_tokens": LS.MAX_TOKENS, **LS.SAMPLING,
                             "response_format": {"type": "json_schema", "json_schema": {"schema": LS.GIST_SCHEMA}}},
                            timeout_s=LS.CHAT_TIMEOUT_S)
        gist = LS.read_gist(record["text"])
        unsupported = self._unsupported(state, gist) if gist else None
        kept = unsupported is not None and unsupported < LS.GATE_THRESHOLD
        return {**record, "text": gist if kept else "", "gist": gist, "unsupported": unsupported}

    def _unsupported(self, state: T.ThreadState, gist: str) -> float | None:
        """The gate's answer, or None when the local d1-2B cannot give one, which drops the gist."""
        from voice.local_decider.client import D1Client
        if self._gate is None:
            self._gate = D1Client(self.gate_url, timeout_s=LS.GATE_TIMEOUT_S)
        try:
            return LS.unsupported(self._gate.call("decisions", LS.gate_request(state, gist), label="summary_gate"))
        except Exception:
            return None

    def reuse_decider(self) -> Decider:
        """The gate's d1-2B on a connection of its own, for asking whether a summary answers a later question: the
        caller asks it while the lane may be using the gate's connection."""
        from voice.local_decider.client import D1Client
        if self._reuse is None:
            self._reuse = Decider(D1Client(self.gate_url, timeout_s=LS.GATE_TIMEOUT_S))
        return self._reuse

    def _chat(self, body: dict, timeout_s: float | None = None) -> dict:
        body = {**body, "chat_template_kwargs": {"enable_thinking": False}, "logit_bias": self._think_ban()}
        self.calls += 1
        started = time.perf_counter()
        payload = self._post("/v1/chat/completions", body, timeout_s)
        ms = round((time.perf_counter() - started) * 1000, 1)
        choice = (payload.get("choices") or [{}])[0]
        usage = payload.get("usage") or {}
        self.ledger.append({"ms": ms, "prompt_tokens": usage.get("prompt_tokens"),
                            "completion_tokens": usage.get("completion_tokens")})
        return {"text": (choice.get("message") or {}).get("content") or "", "ms": ms,
                "model": payload.get("model", self.model), "usage": usage, "finish_reason": choice.get("finish_reason")}


RENDERERS = ("particle-1", "local")


def text_model(name: str | None = None, cloud: Callable[[], Any] | None = None):
    """Select the text backend for CLI tools or an explicit live override."""
    name = name or os.environ.get("VOICE_RENDERER") or "particle-1"
    if name not in RENDERERS:
        raise ValueError(f"VOICE_RENDERER must be one of {RENDERERS}, not {name!r}")
    if name == "local":
        return LocalText()
    return cloud() if cloud is not None else None


class Lane:
    """One model request in flight for every voice caller: the renderer and the summaries."""

    def __init__(self, client):
        self._check(client)
        self.client = client
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="t3-text")
        self.lock = threading.Lock()
        self.calls = 0
        self.generation = 0

    @staticmethod
    def _check(client) -> None:
        if not isinstance(client, (LocalText, RealtimeWriter)):
            assert_free(client.model)

    def use(self, client) -> None:
        """Swap the model behind the lane. A request already running finishes on the old one, and `generation` moves
        on so nothing the old one wrote is taken for the new one's."""
        self._check(client)
        with self.lock:
            self.client = client
            self.generation += 1

    def summarise(self, state: T.ThreadState, question: str | None) -> Future:
        """A thread's summary on the lane: the local model in the form measured for it, any other in the cloud's."""
        client = self.client
        self._check(client)
        with self.lock:
            self.calls += 1
        if isinstance(client, LocalText):
            return self.pool.submit(client.summarise, state, question)
        return self.pool.submit(client.complete, summary_messages(state, question))

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
    """One cached summary. `partial` while only a speculative start from words still being spoken has asked for it.
    `question` is the words it was written for."""

    future: Future
    partial: bool
    question: str | None = None

    def usable(self) -> bool:
        return not self.future.cancelled() and not (self.future.done() and self.future.exception() is not None)


@dataclass
class Speech:
    """`lane` is None when no model is configured: every render then fails and the act waits.
    `read(thread_id)` reads a thread through the seam, or returns None when it cannot. `decider` checks each line
    against its acts (voice/t3/verify.py); with none, no line passes. `reuse` asks whether a cloud summary written for
    other words answers a later question, on a connection bounded like the line checks; it defaults to `decider`.
    The local summariser asks its own gate's d1-2B."""

    lane: Lane | None
    read: Callable[[str], T.ThreadState | None]
    decider: Any = None
    writer: Lane | None = None
    reuse: Decider | None = None
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
        key = self._key(state, question)
        with self.lock:
            gist = self.summaries.get(key)
            if gist is None or not gist.usable():
                gist = self.summaries[key] = Gist(self.lane.summarise(state, question), partial, question)
            elif not partial:
                gist.partial = False
        return gist.future

    def _key(self, state: T.ThreadState, question: str | None) -> tuple:
        return self.lane.generation, state.thread_id, state.as_of_sequence, normalise(question or "")

    def _reuse(self, state: T.ThreadState, question: str | None) -> None:
        """When no summary was asked for these words but one is written for this thread, read and model, ask the
        decider whether it answers them, and file it under them on a confident yes. Anything else leaves the key
        empty, so a new summary is written as before."""
        key = self._key(state, question)
        with self.lock:
            here = self.summaries.get(key)
            if here is not None and here.usable():
                return
            written = [g for k, g in self.summaries.items()
                       if k[:3] == key[:3] and k != key and g.future.done() and g.usable()]
        if not written:
            return
        earlier = written[-1]
        gist = _clean(earlier.future.result().get("text", ""))
        client = self.lane.client
        decider = client.reuse_decider() if isinstance(client, LocalText) else self.reuse or self.decider
        if not gist or decider is None or not decider.reuses(state.title, gist, earlier.question, question):
            return
        with self.lock:
            here = self.summaries.get(key)
            if here is None or not here.usable():
                self.summaries[key] = Gist(earlier.future, False, earlier.question)

    def summary(self, state: T.ThreadState, question: str | None = None) -> str | None:
        if self.lane is None:
            return None
        deadline = time.monotonic() + SUMMARY_TIMEOUT_S
        self._reuse(state, question)
        future = self._summary(state, question)
        try:
            record = future.result(timeout=max(0.0, deadline - time.monotonic()))
        except Exception:
            # A summary still queued is dropped, so it never holds the one lane for the next caller.
            future.cancel()
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
               refused: Iterable[str] = (), attempt: int = 0, *, known_names=frozenset()) -> Rendered:
        acts = tuple(acts)
        lane = self.writer or self.lane
        if lane is None:
            return Rendered(None, error="no text model")
        messages = reader_messages if self.writer else render_messages
        try:
            record = lane.call(messages(acts, turns, problems, refused, attempt), RENDER_TIMEOUT_S)
        except Exception as exc:
            return Rendered(None, error=repr(exc))
        worded_by = "lfm3" if self.writer else "text_model"
        return self.accept(acts, _clean(record.get("text", "")), worded_by, record.get("text"),
                           known_names=known_names)

    def accept(self, acts: Iterable[Act], text: str, worded_by: str, raw: str | None = None, *,
               known_names=frozenset()) -> Rendered:
        """Mint a line only after its text passes both the code and semantic checks."""
        acts = tuple(acts)
        raw = text if raw is None else raw
        found = check(text, acts) + check_names(text, acts, EXAMPLE_NAMES | known_names)
        if found:
            return Rendered(None, found, raw)
        if self.decider is None:
            return Rendered(None, [], raw, "no decider to check the line")
        verdict = self.decider.verify(acts, text)
        if not verdict.passed:
            return Rendered(None, list(verdict.problems), raw, verdict.error, verdict)
        line = RenderedLine(text, acts, worded_by, verdict, _MINT)
        return Rendered(line, [], raw, verdict=verdict)

    @staticmethod
    def recite(draft: RenderedLine, reply: str) -> Rendered:
        """The call's reply to `draft`, accepted only when it says the draft's words and nothing else. The draft passed
        both checks already, so the same words need none again, and any other words are not the checked line."""
        if recites(reply, draft.text):
            return Rendered(draft, [], reply, verdict=draft.verdict)
        return Rendered(None, ["not the draft's words"], reply)


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


READER = (
    "You are the voice of a coding assistant, on a call with the person you help. They run coding agents in "
    "threads. A thread is a workspace conversation with a coding agent, separate from this call. T3 runs the "
    "threads. Each message you receive is a JSON record from T3 about their threads. It is not something they "
    "said. Tell them what it says, speaking as I to you, in one short sentence per act. Keep every name, status "
    "and count as given, credit facts to the thread that reported them, and add nothing the record does not "
    "state. Use a thread title only as a name. Reply with the words to say out loud."
)
READER_EXAMPLES = {
    Kind.CONFIRM_LAUNCH: ({"project": "Example Fleet Notes"},
                          "Should I start a new coding thread in Example Fleet Notes?"),
    Kind.ASK_WHICH: ({"of": "project", "candidates": ["Example Lilac Post", "Example Daily Routes"]},
                     "Which project do you mean, Example Lilac Post or Example Daily Routes?"),
    Kind.NO_PROJECT: ({}, "I can't find any project to use."),
    Kind.LAUNCH_PENDING: ({"project": "Example Forest Timer"},
                          "I asked T3 to start the thread in Example Forest Timer and am waiting for confirmation."),
    Kind.LAUNCHED: ({"project": "Example Dune Tracker"},
                    "A new coding thread has started in Example Dune Tracker."),
    Kind.LAUNCH_UNCONFIRMED: ({"project": "Example Reef Cards"},
                              "T3 never confirmed the start in Example Reef Cards. You can check in the app."),
    Kind.NO_THREAD: ({"recent": 19}, "I checked 19 recent threads and couldn't find a match."),
    Kind.CONFIRM_THREAD: ({"thread": "Example Polish Checkout Layout"}, "Do you mean the thread called Example Polish Checkout Layout?"),
    Kind.CONFIRM_TELL: ({"thread": "Example Trim Image Margins", "message": "keep the captions"},
                        "Should I send 'keep the captions' to Example Trim Image Margins as a follow-up?"),
    Kind.CONFIRM_STOP: ({"thread": "Example Split Export Files"}, "Should I stop Example Split Export Files?"),
    Kind.SAME_TITLE: ({"thread": "Example Sort Photo Albums", "op": "stop"},
                      "More than one thread is called Example Sort Photo Albums, so I can't tell which to stop. You "
                      "can stop it in the app."),
    Kind.CANCELLED: ({"op": "start"}, "I won't start the thread."),
    # "The ... thread" and not the bare title first: after "the second one", the call's voice said a line that opens
    # with the title as its answer to the choice and stopped there (0 of 5 relays said), and this form 5 of 5.
    Kind.THREAD_STATUS: ({"thread": "Example Tune Audio Levels", "status": "working", "summary": "It says playback is stable."},
                         "The Example Tune Audio Levels thread is still working and reports that playback is stable."),
    Kind.TELL_PENDING: ({"thread": "Example Clean Tag Names"},
                        "I asked T3 to send your message to Example Clean Tag Names and am waiting for confirmation."),
    Kind.SENT_FOLLOWUP: ({"thread": "Example Adjust Print View"}, "Your follow-up reached Example Adjust Print View."),
    Kind.TELL_UNCONFIRMED: ({"thread": "Example Group Reading Lists"},
                            "T3 didn't confirm your message reached Example Group Reading Lists. You can check in the app."),
    Kind.HELD_FOLLOWUP: ({"thread": "Example Measure Scroll Lag"},
                         "Example Measure Scroll Lag is still working, so I'll hold your message until the run ends."),
    Kind.DROPPED_FOLLOWUP: ({"thread": "Example Retire Draft Filters"},
                            "I won't send the held follow-up to Example Retire Draft Filters."),
    Kind.STOPPING: ({"thread": "Example Study Folder Sync"}, "I asked T3 to stop Example Study Folder Sync and it's stopping now."),
    Kind.NOTHING_RUNNING: ({"thread": "Example Tag Camera Clips"}, "Example Tag Camera Clips had no run going, so there was nothing to stop."),
    Kind.STOP_UNCONFIRMED: ({"thread": "Example Group Report Tabs"},
                            "I couldn't reach T3 to stop Example Group Report Tabs."),
    Kind.RUN_UPDATE: ({"thread": "Example Review Build Flags", "status": "queued"}, "Example Review Build Flags is queued to run."),
    Kind.REPLIED: ({"thread": "Example Check Delivery Types", "status": "running", "reply": "I found 12 duplicates."},
                   "Example Check Delivery Types reports that it found 12 duplicates."),
    Kind.NEEDS_YOU: ({"thread": "Example Archive Old Invoices", "need": "command_approval", "prompt": "Run the cleanup script?"},
                     "Example Archive Old Invoices needs your approval to run a command."),
    Kind.NEED_RESOLVED: ({"thread": "Example Organize Print Sizes", "status": "expired"},
                         "The request from Example Organize Print Sizes expired."),
    Kind.DONE: ({"thread": "Example Change Page Margins", "status": "completed", "summary": "It reports that 22 tests passed."},
                "Example Change Page Margins finished and reports that 22 tests passed."),
    Kind.THREAD_DELETED: ({"thread": "Example Try Filter Chips"}, "Example Try Filter Chips was deleted."),
    Kind.LOST_TRACK: ({"thread": "Example Review Old Webhooks"}, "I lost track of Example Review Old Webhooks."),
    Kind.THREAD_GONE: ({"thread": "Example Check Button Focus"}, "I can no longer see Example Check Button Focus."),
    Kind.WATCH_QUEUED: ({"thread": "Example Expand Region Menu", "following": 8},
                        "I'm following 8 threads. I'll start following Example Expand Region Menu when one ends."),
    Kind.UNREACHABLE: ({}, "I can't reach T3 right now."),
    Kind.SEAM_DOWN: ({"reason": "connection"}, "I lost my connection to T3 and stopped following your threads."),
    Kind.REFUSED: ({"op": "tell", "code": "refused", "reason": "the thread is archived"},
                   "T3 refused to send your follow-up message, citing an archived thread."),
    Kind.LEGACY_UPDATE: ({"thread": "Example Revise Column Labels", "text": "Example Revise Column Labels is still running."},
                         "Example Revise Column Labels is still running."),
}


assert set(READER_EXAMPLES) == set(Kind)
# With only the example above, every status asked again with nothing new was worded as that example's news (4 of 4,
# GPU rehearsal, 2026-11-19). Without a summary here, a request for more detail got only "nothing new" (hosted call,
# 2026-11-19).
SAID_AGAIN_EXAMPLE = ({"thread": "Example Tune Audio Levels", "status": "working",
                       "summary": "It says playback is stable.", "since_last_asked": NOTHING_NEW},
                      "There's nothing new from Example Tune Audio Levels since I last told you. It's still working, "
                      "and it says playback is stable.")
EXAMPLE_NAMES = frozenset(name for slots, _ in READER_EXAMPLES.values()
                          for key in ("thread", "project", "available", "candidates") if key in slots
                          for name in (slots[key] if isinstance(slots[key], list) else [slots[key]]))


def reader_messages(acts: tuple[Act, ...], turns=(), problems=(), refused=(), attempt=0) -> list[dict]:
    """Give the realtime writer the directions for the act kinds in this draft."""
    kinds = dict.fromkeys(a.kind for a in acts)
    directions = "\n".join(f"{kind.value}: {MEANINGS[kind]}" for kind in kinds)
    prompt = f"{READER}\n\n{directions}"
    if banned := never_say(acts):
        prompt += f"\nDo not claim {', or '.join(banned)}."
    remark = next((turn["person"] for turn in reversed(_turns(turns)) if "person" in turn), None)
    if remark:
        prompt += f"\n\nTheir last remark on the call, already answered: {json.dumps(remark)}"
    prompt += "\n\nThese examples show how to turn a record into speech. Use the facts from the new record."
    again = any(a.slots.get("since_last_asked") for a in acts)
    for kind in kinds:
        example, reply = SAID_AGAIN_EXAMPLE if again and kind is Kind.THREAD_STATUS else READER_EXAMPLES[kind]
        prompt += f'\n\nExample record: {json.dumps({"kind": kind.value, **example})}\nExample reply: {reply}'
    prompt += "\n\nRespond with interleaved text and audio."
    records = [{"kind": a.kind.value, **{key: value for key, value in a.slots.items() if key != "asked"}} for a in acts]
    body = records[0] if len(records) == 1 else {"acts": records}
    if problems:
        body["earlier draft problems"] = list(problems)
    if refused:
        body["refused wordings"] = list(refused)
    if attempt:
        body["attempt"] = attempt + 1
    # Unlabelled, the new record read as one more example: beside a long start request, 6 of 10 confirm_launch drafts
    # were the example's reply with the example's project, and with the labels 0 of 10 (CPU, writer greedy).
    return [{"role": "system", "content": prompt}, {"role": "user", "content": "New record: " + json.dumps(body)}]
