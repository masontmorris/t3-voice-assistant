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

import hashlib
import json
import os
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Iterable, Mapping

from voice.t3 import local_summary as LS
from voice.t3 import types as T
from voice.t3.pick.model import allowed_model
from voice.t3.acts import (NOTHING_NEW, QUESTIONS, RULES, SUMMARISED, Act, Kind, check, check_names, forbidden_claims, normalise, outcome_meaning,
                           recites)
from voice.t3.realtime_writer import RealtimeWriter
from voice.t3.journal import launch_title
from voice.t3.verify import REQUEST_ACTS, Decider, Verdict

RENDER_TIMEOUT_S = 8.0
SUMMARY_TIMEOUT_S = 10.0
assert LS.CHAT_TIMEOUT_S + LS.GATE_TIMEOUT_S < SUMMARY_TIMEOUT_S
TURNS = 3

NEED_WORDS = ("`need` is what it waits for: command_approval (approve a command it wants to run), file_change "
              "(approve file edits), file_read, permission, tool_input, question (answer its question), link or "
              "secret or card (something only the app can take), or unanswerable (a request no one can answer any "
              "more, so they can stop it in the app). Say what it waits for in a few words of your own. When "
              "`request` is given, say what that request would do from it, in your own words, without naming a program, a "
              "file or a path, and add nothing to it. "
              "Without `request`, say only the kind of thing it waits for, such as approval to run a command, and "
              "never what the command does or is for. Never say a command line, a flag, a path or code.")
BEGUN = (" When `begun_in` is given, the thread has no name yet: it is the one you started for them in that project, "
         "so say that in place of a thread name. The project is not the thread's name. When `begun_for` is given, more "
         "than one thread was started in that project, so also say what this one was started for, from it.")
# What each act means, for the model that words it. These are directions, never wording to repeat.
MEANINGS: dict[Kind, str] = {
    Kind.CONFIRM_LAUNCH: "Ask whether to start a new coding thread in this project. Nothing has started yet. Do not "
                         "repeat what they asked for.",
    Kind.ASK_WHICH: "Ask which one they mean, saying whether `of` is project or thread, and name every candidate. "
                    "Join the final two choices with or. Do not say the word candidates or repeat what they asked.",
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
    Kind.SAME_TITLE: "Say that you did nothing, because more than one of their threads has this name. `op` is what "
                     "they asked for: tell (send it a message) or stop. Say they can do it in the app.",
    Kind.CANCELLED: "Acknowledge that you will not do what was asked (`op`: start, ask, tell, stop or answer). Name the "
                    "thread only when the record gives its title.",
    Kind.THREAD_STATUS: "Answer how the thread is going: say its status, then say what `summary` says, in answer to "
                        "what they asked. Say the content of `summary` itself, in your own words, and never say the "
                        "word summary. When `since_last_asked` is set, say first that there is nothing new since they "
                        "last heard, and do not offer the status as news, but still give the summary. When `need` is set, "
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
    Kind.CONFIRM_RESPOND: "Ask whether to approve or deny the request the thread is waiting on, as `decision` says, "
                          "naming the thread. When `summary` is given, say what this request would do, in your own "
                          "words, from it: what the person would be allowing. A denial has no `summary`: name the "
                          "thread and ask whether to say no to its request. Never read out a command line, flag, path "
                          "or code. Nothing is answered yet.",
    Kind.NEEDS_THE_APP: "Name the thread using the full thread title from the record exactly as supplied, without "
                        "wrapping it in quotes. Say that you cannot describe what the request would do and that "
                        "approval has to happen in the app.",
    Kind.RESPONDED: "T3 accepted the answer: say that the request was approved or denied, as `decision` says, for the "
                    "thread. Do not say what the thread does next.",
    Kind.NOTHING_PENDING: "The thread is not waiting on any request right now, so there was nothing to answer. "
                          "Name the thread when it is given.",
    Kind.ASK_QUESTION: "The thread is asking them a question and waiting for the answer. Ask it, naming the thread, and "
                       "say every value in `options` exactly as given and in the order given, so they can pick one by name or "
                       "place. Say the question in your own words. When `own_words` is true, say they can also answer in their own words. When `count` "
                       "is set, this is question `number` of `count`: say which. `restated` unheard means you did not "
                       "catch which option they meant, and changed means the thread now asks something different: say "
                       "so, then ask. Nothing is answered yet.",
    Kind.CONFIRM_ANSWER: "Ask whether to give the thread this answer to its question. Name the thread, and say every "
                         "value in `answers` exactly as given, in the order given. Do not ask the thread's "
                         "questions again: ask whether to give it these answers. Nothing is sent yet.",
    Kind.ANSWERED: "T3 took the answer: say the thread has been given it, and say every value in `answers` exactly as "
                   "given. Do not say what the thread does next.",
    Kind.ANSWER_IN_APP: "You cannot take this thread's question by voice: say they have to answer it in the app. Name "
                        "the thread.",
    Kind.UNANSWERED: "Say in one short sentence that you could not give an answer just now. Give no reason, report "
                     "no outcome, and make no other claim.",
    Kind.NOTHING_DONE: "What you said a moment ago about starting, sending, stopping or approving something was "
                       "wrong: nothing has been done. Say so in one short sentence, and make no other claim.",
    Kind.CORRECTION: "What you said a moment ago about this thread was wrong. Say so in a few words, then say its real "
                     "status. When `need` is set, the thread is waiting on them: " + NEED_WORDS,
}
assert set(MEANINGS) == set(Kind)

CLAIM_WORDS = {"answered": "that the request was already approved or denied", "finished": "that the thread is finished, done or completed",
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
        """A spoken line for `local_messages`: the line as JSON, since the grammar cannot open `<think>` or run on past
        the line, then the card's sampling. Output cut at `LINE_MAX_TOKENS` is no line."""
        record = self._chat({"messages": messages, "max_tokens": LINE_MAX_TOKENS, **LS.SAMPLING,
                             "response_format": {"type": "json_schema", "json_schema": {"schema": LINE_SCHEMA}}})
        return {**record, "text": read_line(record["text"])}

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

    def chat_gist(self, messages) -> str:
        """The `gist` string of a request answered in the description's JSON form."""
        record = self._chat({"messages": messages, "max_tokens": LS.MAX_TOKENS, **LS.SAMPLING,
                             "response_format": {"type": "json_schema", "json_schema": {"schema": LS.REQUEST_SCHEMA}}},
                            timeout_s=LS.CHAT_TIMEOUT_S)
        return record["text"]

    def describe(self, need: T.PendingNeed) -> dict:
        """What approving `need` would let the thread do, in the approval prompt's form. It is not gated here: the
        gate is the caller's (`RequestGate`), so no model's description reaches a confirmation without it."""
        record = self._chat({"messages": LS.request_messages(need), "max_tokens": LS.MAX_TOKENS, **LS.SAMPLING,
                             "response_format": {"type": "json_schema", "json_schema": {"schema": LS.REQUEST_SCHEMA}}},
                            timeout_s=LS.CHAT_TIMEOUT_S)
        return {**record, "text": LS.read_description(record["text"]) or ""}

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


LINE_SCHEMA = {"type": "object", "properties": {"line": {"type": "string", "maxLength": 300}}, "required": ["line"],
               "additionalProperties": False}
LINE_MAX_TOKENS = 120


def read_line(text: str) -> str:
    """The `line` of the model's JSON, or empty when it is not JSON with one."""
    try:
        record = json.loads(text)
    except ValueError:
        return ""
    line = record.get("line") if isinstance(record, dict) else None
    return line if isinstance(line, str) else ""


class RequestGate:
    """The local d1-2B's check that a description says what a request would do, one connection and one request in
    flight. Every description of an approval goes through it, whichever model wrote it, and with no answer from it
    (not configured, down, a timeout, an answer that is not a probability) nothing is described."""

    def __init__(self, url: str):
        self.url = url
        self._client = None
        self._lock = threading.Lock()

    def doubts_account(self, instruction: str, account: str) -> float | None:
        """P(the account says something the request does not support), or None when the gate gives no answer."""
        from voice.local_decider.client import D1Client
        with self._lock:
            try:
                if self._client is None:
                    self._client = D1Client(self.url, timeout_s=LS.GATE_TIMEOUT_S)
                return LS.unsupported(self._client.call("decisions", LS.account_gate(instruction, account),
                                                         label="account_gate"))
            except Exception:
                return None

    def doubt(self, need: T.PendingNeed, description: str) -> float | None:
        from voice.local_decider.client import D1Client
        with self._lock:
            try:
                if self._client is None:
                    self._client = D1Client(self.url, timeout_s=LS.GATE_TIMEOUT_S)
                return LS.doubt(self._client.call("decisions", LS.request_gate(need, description),
                                                   label="request_gate"))
            except Exception:
                return None


def described(client, need: T.PendingNeed, gate: RequestGate | None) -> str:
    """`client`'s description of `need`, when the gate passes it against the request; otherwise empty."""
    if isinstance(client, LocalText):
        text = client.describe(need)["text"]
    else:
        text = LS.read_description(client.complete(LS.request_messages(need)).get("text") or "") or ""
    doubt = gate.doubt(need, text) if gate is not None and text else None
    return text if doubt is not None and doubt < LS.REQUEST_THRESHOLD else ""


def accounted(client, instruction: str, gate: RequestGate | None) -> str:
    """`client`'s account of what a thread was started for, when the gate passes it against the request."""
    messages = LS.account_messages(instruction)
    if isinstance(client, LocalText):
        text = LS.read_description(client.chat_gist(messages)) or ""
    else:
        text = LS.read_description(client.complete(messages).get("text") or "") or ""
    doubt = gate.doubts_account(instruction, text) if gate is not None and text else None
    return text if doubt is not None and doubt < LS.ACCOUNT_THRESHOLD else ""


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

    def account(self, instruction: str, gate: RequestGate | None) -> Future:
        """What a thread was started for, written by the model behind the lane and passed by `gate`."""
        client = self.client
        self._check(client)
        with self.lock:
            self.calls += 1
        return self.pool.submit(accounted, client, instruction, gate)

    def describe(self, need: T.PendingNeed, gate: RequestGate | None) -> Future:
        """What approving `need` would let the thread do, written by the model behind the lane and passed by `gate`."""
        client = self.client
        self._check(client)
        with self.lock:
            self.calls += 1
        return self.pool.submit(described, client, need, gate)

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


@dataclass(frozen=True)
class Launch:
    """A thread this voice started on a person's request, in any call: what they said and the project it started in.
    "You started" covers exactly these. A thread another client made is the person's, not the voice's to claim."""

    thread_id: str
    instruction: str
    project: str


@dataclass(frozen=True)
class Reference:
    """How to name one launched thread now. `unnamed`: T3 still titles it with the launch title, which is the person's
    own words cut off and no name to say. `shared`: another launched thread in the project is as unnamed, so the
    project alone does not tell them apart."""

    title: str
    instruction: str
    project: str
    unnamed: bool
    shared: bool


def reference(thread_id: str, launched: Iterable[Launch], titles: Mapping[str, str]) -> Reference | None:
    """The reference for `thread_id` against the titles T3 holds now, or None when the voice did not start it or T3
    does not list it. The thread is picked by its id; a title is only compared with the one the launch gave."""
    launched = tuple(launched)
    me = next((l for l in launched if l.thread_id == thread_id), None)
    title = titles.get(thread_id)
    if me is None or title is None:
        return None
    unnamed = title == launch_title(me.instruction)
    shared = unnamed and any(l.thread_id != thread_id and l.project == me.project
                             and titles.get(l.thread_id) == launch_title(l.instruction) for l in launched)
    return Reference(title, me.instruction, me.project, unnamed, shared)


def named(acts: Iterable[Act], ref: Reference | None, account: str | None) -> tuple[Act, ...]:
    """`acts` about the thread `ref` names, with its title as T3 holds it now. One still titled with the launch title
    is carried by its project (`begun_in`) and, when another launched thread in the project is as unnamed, by what it
    was started for (`begun_for`, a gated account). With no gated account to tell them apart the title stays: it is
    unsayable but it does not name another thread."""
    acts = tuple(acts)
    if ref is None:
        return acts
    out = []
    for a in acts:
        title = a.slots.get("thread")
        if "begun_in" not in RULES[a.kind].slots or title not in (launch_title(ref.instruction), ref.title):
            out.append(a)
            continue
        slots = {k: v for k, v in a.slots.items() if k != "thread"}
        if not ref.unnamed or (ref.shared and not account):
            out.append(Act(a.kind, {**slots, "thread": ref.title}))
        else:
            out.append(Act(a.kind, {**slots, "begun_in": ref.project, **({"begun_for": account} if ref.shared else {})}))
    return tuple(out)


def candidate_names(thread_ids: Iterable[str], titles: Mapping[str, str], launched: Iterable[Launch],
                    account: Callable[[Launch], str | None]) -> list[str]:
    """What the voice calls each of the threads it offers to choose between: its title, or for a launched thread that
    still has the launch title, what it was started for, when a gated account of that is available."""
    launched = {l.thread_id: l for l in launched}
    names = []
    for thread in thread_ids:
        me = launched.get(thread)
        said = account(me) if me is not None and titles[thread] == launch_title(me.instruction) else None
        names.append(said or titles[thread])
    return names


def without_request(acts: Iterable[Act]) -> tuple[Act, ...]:
    """`acts` with no description of a request: the second draft of a line the first draft of which was refused says
    only what kind of thing the thread waits for, so a description the writer cannot word never costs the line."""
    return tuple(Act(a.kind, {k: v for k, v in a.slots.items() if k != "request"}) if "request" in a.slots else a
                 for a in acts)


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
    return {"act": a.kind.value, "means": wording_direction(a), "facts": dict(a.slots)}


def wording_direction(a: Act) -> str:
    direction = _direction(a)
    return direction + BEGUN if a.slots.get("begun_in") else direction


def _direction(a: Act) -> str:
    if a.kind is Kind.UNANSWERED and (outcomes := a.slots.get("outcomes")):
        return f"Say only that you {outcome_meaning(outcomes)}. Do not say you could not answer."
    if a.slots.get("recap"):
        return MEANINGS[a.kind] + " Make clear this is a recap of the same action you already reported."
    if a.kind in (Kind.THREAD_STATUS, Kind.NEEDS_YOU) and not (a.slots.get("summary") or a.slots.get("since_last_asked")
                                                                or a.slots.get("request")):
        return (MEANINGS[a.kind] + " Use at most eight words besides the full thread title. Copy the title as supplied, "
                "including any ellipsis, without quotes or adding spaces.")
    return MEANINGS[a.kind]


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
    gate: RequestGate | None = None
    descriptions: dict = field(default_factory=dict)
    accounts: dict = field(default_factory=dict)

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
        """A summary is written from the thread's last reply and never from a pending request: what a request would
        do is the description's job, gated against the request. So the state the model sees has none, whether the
        summary is a prefetch or a reuse, and a thread with only a request has nothing to summarise."""
        if self.lane is None or (state.last_reply is None and state.pending is not None):
            return None
        state = replace(state, pending=None)
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
        if self.lane is None or (state.last_reply is None and state.pending is not None):
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

    def describe(self, thread_id: str | None, need: T.PendingNeed | None) -> str | None:
        """What approving `need` would let the thread do, or None. It is bound to this request by its id and its
        stored content, and to this purpose: an ordinary summary is never one, and the descriptions are not summaries.
        A request the voice cannot describe, no gate, a gate with no answer, or a description the gate doubts is None,
        and the approval is left to the app."""
        if self.lane is None or need is None or thread_id is None or self.gate is None or not LS.describable(need):
            return None
        content = hashlib.sha256(json.dumps([need.source.value, need.prompt]).encode()).hexdigest()
        key = (self.lane.generation, thread_id, need.request_id, content)
        deadline = time.monotonic() + SUMMARY_TIMEOUT_S
        with self.lock:
            future = self.descriptions.get(key)
            if future is None or future.cancelled() or (future.done() and future.exception() is not None):
                future = self.descriptions[key] = self.lane.describe(need, self.gate)
        try:
            return _clean(future.result(timeout=max(0.0, deadline - time.monotonic()))) or None
        except Exception:
            future.cancel()
            return None

    def account(self, launch: Launch) -> str | None:
        """What a thread the voice started was started for, from the request that started it and nothing else, or
        None. It is cached per request and passed by its own gate question against that request, so with no gate, no
        answer from it, or an account it doubts there is none and the thread is not guessed at."""
        if self.lane is None or self.gate is None:
            return None
        key = (self.lane.generation, launch.thread_id, hashlib.sha256(launch.instruction.encode()).hexdigest())
        deadline = time.monotonic() + SUMMARY_TIMEOUT_S
        with self.lock:
            future = self.accounts.get(key)
            if future is None or future.cancelled() or (future.done() and future.exception() is not None):
                future = self.accounts[key] = self.lane.account(launch.instruction, self.gate)
        try:
            return _clean(future.result(timeout=max(0.0, deadline - time.monotonic()))) or None
        except Exception:
            future.cancel()
            return None

    def _confirmed(self, a: Act, thread_id: str | None, need: T.PendingNeed | None) -> Act:
        """A confirmation of an answer to a request. An approval carries the description of `need` if one passed the
        gate, and never one it was given; a denial carries none, since nothing runs when a request is denied."""
        slots = {k: v for k, v in a.slots.items() if k != "summary"}
        text = self.describe(thread_id, need) if a.slots.get("decision") == "approve" else None
        return Act(a.kind, {**slots, "summary": text} if text else slots)

    def _request(self, acts: tuple[Act, ...], thread_id: str | None, state: T.ThreadState | None) -> tuple[Act, ...]:
        """A line about a pending request never sees the request's stored text. What it may say the request is about is
        `request`, the description the approval gate passed for this request as the read holds it now, and without one
        it says only what kind of thing the thread waits for."""
        out = []
        for a in acts:
            if a.kind not in REQUEST_ACTS or not ({"prompt", "pending"} & a.slots.keys()):
                out.append(a)
                continue
            stored = a.slots.get("prompt", a.slots.get("pending"))
            need = state.pending if state is not None and state.pending is not None and state.pending.prompt == stored else None
            text = self.describe(thread_id, need)
            slots = {k: v for k, v in a.slots.items() if k not in ("prompt", "pending")}
            out.append(Act(a.kind, {**slots, "request": text} if text else slots))
        return tuple(out)

    def fill(self, acts: tuple[Act, ...], thread_id: str | None, sequences: tuple[int, ...] = (),
             need: T.PendingNeed | None = None):
        """The acts with the read's title and the thread's summary filled in, and the read's sequence as evidence. A
        confirmation of an answer to a request is filled from `need`, the request itself, and never from a summary."""
        acts = tuple(self._confirmed(a, thread_id, need) if a.kind is Kind.CONFIRM_RESPOND else a for a in acts)
        summarised = SUMMARISED - {Kind.CONFIRM_RESPOND}
        asks_request = any(a.kind in REQUEST_ACTS and {"prompt", "pending"} & a.slots.keys() for a in acts)
        if thread_id is None or not (asks_request or any(a.kind in summarised and "summary" not in a.slots
                                                          for a in acts)):
            return self._request(acts, thread_id, None), ()
        state, read = None, ()
        if any(a.kind is Kind.THREAD_STATUS for a in acts):
            with self.lock:
                state = next((self.states[(thread_id, s)] for s in sequences if (thread_id, s) in self.states), None)
        if state is None:
            state = self.read(thread_id)
            if state is None:
                return self._request(acts, thread_id, None), ()
            self.remember(state)
            read = (state.as_of_sequence,)
        acts = self._request(acts, thread_id, state)
        question = next((a.slots.get("asked") for a in acts if a.slots.get("asked")), None)
        gist = self.summary(state, question)
        filled = []
        for a in acts:
            if a.kind in summarised:
                slots = {"summary": gist} if gist else {}
                if "thread" not in a.slots and state.title:
                    slots["thread"] = state.title
                a = a.with_slots(**slots)
            filled.append(a)
        return tuple(filled), read

    def render(self, acts: Iterable[Act], turns: Iterable[dict] = (), problems: Iterable[str] = (),
               refused: Iterable[str] = (), attempt: int = 0, *, known_names=frozenset()) -> Rendered:
        acts = tuple(acts)
        routes = [(lane, build, by) for lane, build, by in
                  ((self.writer, reader_messages, "lfm3"), (self.lane, self._lane_messages(), "text_model"))
                  if lane is not None]
        if not routes:
            return Rendered(None, error="no text model")
        problems, refused = list(problems), list(refused)
        first = None
        for lane, build, worded_by in routes:
            try:
                record = lane.call(build(acts, turns, problems, refused, attempt), RENDER_TIMEOUT_S)
            except Exception as exc:
                rendered = Rendered(None, error=repr(exc))
            else:
                rendered = self.accept(acts, _clean(record.get("text", "")), worded_by, record.get("text"),
                                       known_names=known_names)
            if rendered.line is not None:
                return rendered
            first = first or rendered
            # The next route is told what this one was refused for, so it does not word the same line.
            problems = list(rendered.problems or ([rendered.error] if rendered.error else []))
            if rendered.raw and rendered.raw not in refused:
                refused.append(rendered.raw)
        return first

    def _lane_messages(self):
        return local_messages if self.lane is not None and isinstance(self.lane.client, LocalText) else render_messages

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
    "state. Use a thread title only as a name, without quotes. Never say a record's field names, and never mention a "
    "summary, records or notes: say what they hold. Reply with the words to say out loud."
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
    # Said to the call's voice, the older shape ("More than one thread is called X, so I can't tell which to stop")
    # was refused on 20 of 24 relays: it opened on a refusal of its own ("I can't help"). This shape recited 24 of 24.
    Kind.SAME_TITLE: ({"thread": "Example Sort Photo Albums", "op": "stop"},
                      "I didn't stop anything, because more than one thread is called Example Sort Photo Albums. You "
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
    Kind.NEEDS_YOU: ({"thread": "Example Archive Old Invoices", "need": "command_approval"},
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
    Kind.CONFIRM_RESPOND: ({"thread": "Example Archive Old Invoices", "decision": "approve", "need": "command_approval",
                            "summary": "It wants to run a script that deletes the old invoice folder."},
                           "Should I approve Example Archive Old Invoices deleting the old invoice folder?"),
    Kind.RESPONDED: ({"thread": "Example Mend Rail Maps", "decision": "approve", "need": "file_change"},
                     "I approved the file changes for Example Mend Rail Maps."),
    Kind.NEEDS_THE_APP: ({"thread": "Example Tag Camera Clips", "decision": "approve"},
                          "For Example Tag Camera Clips, I can't describe the request. It needs approval in the app."),
    Kind.NOTHING_PENDING: ({"thread": "Example Tag Camera Clips"},
                           "Example Tag Camera Clips isn't waiting on any request, so there was nothing to answer."),
    Kind.ASK_QUESTION: ({"thread": "Example Pick Storage Layer", "question": "Which database should it use?",
                         "options": ["Postgres", "SQLite", "MySQL"], "own_words": False},
                        "The Example Pick Storage Layer thread is asking which database it should use: Postgres, SQLite, or "
                        "MySQL?"),
    Kind.CONFIRM_ANSWER: ({"thread": "Example Pick Storage Layer", "answers": ["Postgres"]},
                          "Should I answer Example Pick Storage Layer with Postgres?"),
    Kind.ANSWERED: ({"thread": "Example Mend Rail Maps", "answers": ["Postgres"]},
                    "I gave Example Mend Rail Maps the answer Postgres."),
    Kind.ANSWER_IN_APP: ({"thread": "Example Tag Camera Clips"},
                         "Example Tag Camera Clips has a question I can't take by voice, so you'll need to answer it "
                         "in the app."),
    Kind.UNANSWERED: ({}, "I couldn't give you an answer just now."),
    Kind.NOTHING_DONE: ({}, "Sorry, I got that wrong. I haven't started or changed anything."),
    Kind.CORRECTION: ({"thread": "Example Bake Menu Cards", "status": "working"},
                      "Sorry, I got that wrong. The Example Bake Menu Cards thread is still working."),
}


assert set(READER_EXAMPLES) == set(Kind)
# A kind whose example is chosen by a slot as well: shown only the project example, the writer restates the task for
# thread candidates and names none.
READER_VARIANTS = {
    (Kind.UNANSWERED, "outcome"): ({"outcomes": ["sent"]}, "I sent the follow-up message."),
    (Kind.SAME_TITLE, "tell"): ({"thread": "Example Sort Photo Albums", "op": "tell"},
                                "I didn't send anything, because more than one thread is called Example Sort Photo "
                                "Albums. You can send it in the app."),
    (Kind.CONFIRM_RESPOND, "deny"): ({"thread": "Example Archive Old Invoices", "decision": "deny",
                                     "need": "command_approval"},
                                    "Should I say no to the request Example Archive Old Invoices is waiting on?"),
    (Kind.RESPONDED, "deny"): ({"thread": "Example Mend Rail Maps", "decision": "deny", "need": "file_change"},
                               "I said no to the file changes for Example Mend Rail Maps."),
    (Kind.CANCELLED, "stop"): ({"op": "stop"}, "I won't stop the thread."),
    (Kind.NEEDS_YOU, "request"): ({"thread": "Example Archive Old Invoices", "need": "command_approval",
                                   "request": "It wants to delete the old invoice folder."},
                                  "Example Archive Old Invoices needs your approval to delete the old invoice folder."),
    (Kind.NEEDS_YOU, "begun_for"): ({"begun_in": "Example Fleet Notes", "begun_for": "sorting the fleet log by date",
                                    "need": "command_approval"},
                                   "The thread you started in Example Fleet Notes for sorting the fleet log by date needs "
                                   "your approval to run a command."),
    (Kind.THREAD_STATUS, "begun_for"): ({"begun_in": "Example Fleet Notes", "begun_for": "sorting the fleet log by date",
                                        "status": "waiting_on_you", "need": "command_approval"},
                                       "The thread you started in Example Fleet Notes for sorting the fleet log by date is "
                                       "waiting for your approval to run a command."),
    (Kind.CONFIRM_THREAD, "begun_for"): ({"begun_in": "Example Fleet Notes", "begun_for": "sorting the fleet log by date"},
                                        "Do you mean the thread you started in Example Fleet Notes for sorting the fleet "
                                        "log by date?"),
    (Kind.NEEDS_THE_APP, "begun_for"): ({"begun_in": "Example Fleet Notes", "begun_for": "sorting the fleet log by date",
                                        "decision": "approve"},
                                       "I can't describe the request the thread you started in Example Fleet Notes for "
                                       "sorting the fleet log by date is waiting on. It needs approval in the app."),
    (Kind.NEEDS_YOU, "begun"): ({"begun_in": "Example Fleet Notes", "need": "command_approval"},
                                "The thread you started in Example Fleet Notes needs your approval to run a command."),
    (Kind.THREAD_STATUS, "begun"): ({"begun_in": "Example Fleet Notes", "status": "waiting_on_you",
                                     "need": "command_approval"},
                                    "The thread you started in Example Fleet Notes is waiting for your approval to run "
                                    "a command."),
    (Kind.NO_THREAD, "other"): ({"other": True}, "I couldn't find any other thread that matches, besides the one you turned down."),
    (Kind.CONFIRM_THREAD, "begun"): ({"begun_in": "Example Fleet Notes"},
                                     "Do you mean the thread you started in Example Fleet Notes?"),
    (Kind.NEEDS_THE_APP, "begun"): ({"begun_in": "Example Fleet Notes", "decision": "approve"},
                                    "I can't describe the request the thread you started in Example Fleet Notes is "
                                    "waiting on. It needs approval in the app."),
    # Worded without an example, "I will not answer the thread "X"." came out with quotes around the name (2 of 2 drafts).
    (Kind.CANCELLED, "answer"): ({"op": "answer", "thread": "Example Pick Storage Layer"},
                                 "I won't give Example Pick Storage Layer that answer."),
    # No options to say: ask the question itself as a question. Worded from the options example it ended on a colon.
    (Kind.ASK_QUESTION, "open"): ({"thread": "Example Rename Job Queue", "question": "What should the job be called?",
                                   "options": [], "own_words": True},
                                  "The Example Rename Job Queue thread is asking what the job should be called. What name do you "
                                  "want to give it?"),
    # Several answers go in one question, every one as given, not the thread's questions asked again.
    (Kind.CONFIRM_ANSWER, "several"): ({"thread": "Example Pick Storage Layer", "answers": ["Postgres", "Keep one"]},
                                       "Should I answer Example Pick Storage Layer with Postgres and Keep one?"),
    (Kind.ASK_QUESTION, "unheard"): ({"thread": "Example Pick Storage Layer", "question": "Which database should it use?",
                                      "options": ["Postgres", "SQLite", "MySQL"], "own_words": False, "restated": "unheard"},
                                     "I didn't catch which one you meant. The Example Pick Storage Layer thread is asking which "
                                     "database it should use: Postgres, SQLite, or MySQL?"),
    (Kind.ASK_QUESTION, "changed"): ({"thread": "Example Pick Storage Layer", "question": "Which database should it use?",
                                      "options": ["Postgres", "SQLite", "MySQL"], "own_words": False, "restated": "changed"},
                                     "The Example Pick Storage Layer thread is asking something different now: which database "
                                     "should it use, Postgres, SQLite, or MySQL?"),
    (Kind.ASK_WHICH, "thread_pair"): ({"of": "thread", "candidates": ["Example Mend Rail Maps",
                                                                      "Example Mend Rail Labels"]},
                                      "Which thread do you mean, Example Mend Rail Maps or Example Mend Rail Labels?"),
    (Kind.ASK_WHICH, "thread"): ({"of": "thread", "candidates": ["Example Mend Rail Maps", "Example Mend Rail Labels",
                                                                 "Example Add Rail Legend"]},
                                 "Which thread do you mean, Example Mend Rail Maps, Example Mend Rail Labels, or "
                                 "Example Add Rail Legend?"),
}
# With only the example above, every status asked again with nothing new was worded as that example's news (4 of 4,
# GPU rehearsal, 2026-11-19). Without a summary here, a request for more detail got only "nothing new" (hosted call,
# 2026-11-19).
SAID_AGAIN_EXAMPLE = ({"thread": "Example Tune Audio Levels", "status": "working",
                       "summary": "It says playback is stable.", "since_last_asked": NOTHING_NEW},
                      "There's nothing new from Example Tune Audio Levels since I last told you. It's still working, "
                      "and it says playback is stable.")
SAID_AGAIN_WAITING_EXAMPLE = ({"thread": "Example Tune Audio Levels", "status": "waiting_on_you",
                              "need": "command_approval", "since_last_asked": NOTHING_NEW},
                             "There's nothing new from Example Tune Audio Levels since I last told you. It's still "
                             "waiting for your approval to run a command.")
SAID_AGAIN_REQUEST_EXAMPLE = ({"thread": "Example Tune Audio Levels", "status": "waiting_on_you",
                              "need": "command_approval", "request": "It wants to delete the old sound folder.",
                              "since_last_asked": NOTHING_NEW},
                             "There's nothing new from Example Tune Audio Levels since I last told you. It's still "
                             "waiting for your approval to delete the old sound folder.")
SAID_AGAIN_BEGUN_EXAMPLE = ({"begun_in": "Example Fleet Notes", "status": "waiting_on_you", "need": "command_approval",
                            "since_last_asked": NOTHING_NEW},
                           "There's nothing new from the thread you started in Example Fleet Notes since I last told "
                           "you. It's still waiting for your approval to run a command.")
EXAMPLE_NAMES = frozenset(name for slots, _ in (*READER_EXAMPLES.values(), *READER_VARIANTS.values())
                          for key in ("thread", "project", "available", "candidates", "begun_in") if key in slots
                          for name in (slots[key] if isinstance(slots[key], list) else [slots[key]]))


def _shape(kind: Kind, slots: dict) -> str | None:
    if kind is Kind.ASK_WHICH and slots.get("of") == "thread" and len(slots.get("candidates") or ()) == 2:
        return "thread_pair"
    if slots.get("begun_for") and (kind, "begun_for") in READER_VARIANTS:
        return "begun_for"
    if slots.get("begun_in") and (kind, "begun") in READER_VARIANTS:
        return "begun"
    if kind is Kind.NEEDS_YOU and slots.get("request"):
        return "request"
    if kind is Kind.NO_THREAD and slots.get("other"):
        return "other"
    if variant := (slots.get("of") or slots.get("op") or slots.get("decision") or slots.get("restated")):
        return variant
    if kind is Kind.UNANSWERED and slots.get("outcomes"):
        return "outcome"
    if kind is Kind.ASK_QUESTION and slots.get("options") == []:
        return "open"
    if kind is Kind.CONFIRM_ANSWER and len(slots.get("answers") or ()) > 1:
        return "several"
    return None


def _reader_parts(acts: tuple[Act, ...], turns, problems, refused, attempt):
    """The writer's request in parts: the directions, one (record, reply) example per act kind, and the new record."""
    kinds = dict.fromkeys(a.kind for a in acts)
    firsts = {}
    for a in acts:
        firsts.setdefault(a.kind, a)
    directions = "\n".join(f"{kind.value}: {wording_direction(firsts[kind])}" for kind in kinds)
    head = f"{READER}\n\n{directions}"
    if banned := never_say(acts):
        head += f"\nDo not claim {', or '.join(banned)}."
    remark = next((turn["person"] for turn in reversed(_turns(turns)) if "person" in turn), None)
    if remark:
        head += f"\n\nTheir last remark on the call, already answered: {json.dumps(remark)}"
    again = any(a.slots.get("since_last_asked") for a in acts)
    examples = []
    for kind in kinds:
        slots = firsts[kind].slots
        shown = READER_VARIANTS.get((kind, _shape(kind, slots)), READER_EXAMPLES[kind])
        if again and kind is Kind.THREAD_STATUS:
            shown = (SAID_AGAIN_BEGUN_EXAMPLE if slots.get("begun_in") else
                     SAID_AGAIN_REQUEST_EXAMPLE if slots.get("request") else
                     SAID_AGAIN_WAITING_EXAMPLE if slots.get("need") else SAID_AGAIN_EXAMPLE)
        example, reply = shown
        examples.append((json.dumps({"kind": kind.value, **example}), reply))
    # The questions a confirmation answers are for the line check; the writer given them asked them again (3 of 3 drafts).
    records = [{"kind": a.kind.value, **{key: value for key, value in a.slots.items() if key not in ("asked", "questions")}}
               for a in acts]
    body = records[0] if len(records) == 1 else {"acts": records}
    if body.get("kind") == Kind.NEEDS_THE_APP.value:
        body = {"thread": body["thread"], **body} if "thread" in body else body
    if problems:
        body["earlier draft problems"] = list(problems)
    if refused:
        body["refused wordings"] = list(refused)
    if attempt:
        body["attempt"] = attempt + 1
    return head, examples, body


def reader_messages(acts: tuple[Act, ...], turns=(), problems=(), refused=(), attempt=0, *,
                    realtime: bool = True) -> list[dict]:
    """The realtime writer's request: one system prompt with the directions and examples, and the new record. The last
    instruction asks the realtime session for its text."""
    head, examples, body = _reader_parts(acts, turns, problems, refused, attempt)
    prompt = f"{head}\n\nThese examples show how to turn a record into speech. Use the facts from the new record."
    for record, reply in examples:
        prompt += f"\n\nExample record: {record}\nExample reply: {reply}"
    if realtime:
        prompt += "\n\nRespond with interleaved text and audio."
    # Unlabelled, the new record read as one more example: beside a long start request, 6 of 10 confirm_launch drafts
    # were the example's reply with the example's project, and with the labels 0 of 10 (CPU, writer greedy).
    return [{"role": "system", "content": prompt}, {"role": "user", "content": "New record: " + json.dumps(body)}]


def local_messages(acts: tuple[Act, ...], turns=(), problems=(), refused=(), attempt=0) -> list[dict]:
    """The same directions for the local chat model, with the examples as earlier chat turns and the answer as the
    JSON `LocalText.complete` reads."""
    head, examples, body = _reader_parts(acts, turns, problems, refused, attempt)
    out = [{"role": "system", "content": f'{head}\n\nAnswer with JSON: {{"line": the words to say}}.'}]
    for record, reply in examples:
        out += [{"role": "user", "content": f"New record: {record}"},
                {"role": "assistant", "content": json.dumps({"line": reply})}]
    return [*out, {"role": "user", "content": "New record: " + json.dumps(body)}]
