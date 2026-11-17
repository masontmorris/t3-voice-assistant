"""The general lookup worker: one instruction and the call's recent turns in, one or two spoken sentences out.

The worker asks one cheap OpenRouter chat model for one JSON object: a weather or time lookup, a direct answer, or a
refusal. A lookup runs through the registry, and a second model call words the tool's result. That makes at most two
model calls and one tool call per instruction. A worded answer that states a number found in neither the tool result
nor the conversation is dropped for the tool's own summary, so the voice never speaks a number the tool did not give.
The context is a list of turns, oldest first, each {"user": str, "assistant": [str], "results": [str]}, any key
optional. The last `CONTEXT_TURNS` are used.

    python -m voice.tools.agent "what day was George Washington born on?"

runs one instruction for real against the live registry and prints the result.
"""

import http.client
import json
import os
import re
import socket
import sys
import threading
import time
from datetime import date
from pathlib import Path

from voice.segmentation import client
from voice.tools.lookups import WEEKDAYS, remaining

MODEL = "deepseek/deepseek-v4.1-flash-1017"
DAILY_CAP_USD = 0.50
HOST = "openrouter.ai"
PATH = "/api/v1/chat/completions"
TIMEOUT_S = 12.0
CONTEXT_TURNS = 6
WHENS = ("today", "tomorrow", "weekend", *WEEKDAYS)
NUMBER = re.compile(r"\d+(?:\.\d+)?")
VOICE_HOME = Path(os.environ.get("VOICE_HOME", Path.home() / "voice"))
LEDGER = VOICE_HOME / "evidence" / "agent" / "calls.jsonl"


class Failed(Exception):
    """The worker ran and has no answer. `summary` is the sentence the voice may speak."""

    def __init__(self, summary, detail):
        super().__init__(summary)
        self.summary, self.detail = summary, detail


def context_lines(context):
    lines = []
    for turn in (context or [])[-CONTEXT_TURNS:]:
        if not isinstance(turn, dict):
            continue
        if isinstance(turn.get("user"), str):
            lines.append(f"Person: {turn['user']}")
        lines += [f"Assistant: {text}" for text in turn.get("assistant") or [] if isinstance(text, str)]
        lines += [f"Lookup result: {text}" for text in turn.get("results") or [] if isinstance(text, str)]
    return lines


def conversation(instruction, context):
    lines = context_lines(context)
    earlier = "Conversation so far, oldest first:\n" + "\n".join(lines) if lines else "No earlier conversation."
    return f"{earlier}\n\nRequest: {instruction}"


def choose_messages(instruction, context, today, default_place):
    home = (f"The person's default place is {default_place}; use it when neither the request nor the conversation "
            "names a place." if default_place else "The person has no default place.")
    system = f"""You are the lookup worker behind a voice assistant. Today is {today:%A, %B} {today.day}, {today.year}. {home}
Reply with exactly one JSON object and nothing else, in one of these forms:
{{"tool": "weather", "place": "<city>", "when": "today|tomorrow|weekend|monday|tuesday|wednesday|thursday|friday|saturday|sunday"}} for a forecast, or for anything that depends on the weather, such as what to wear.
{{"tool": "time", "place": "<city>"}} for the current time somewhere.
{{"answer": "<one or two short spoken sentences>"}} for a question you can answer from general knowledge or from the conversation.
{{"cannot": "<why>"}} when the request needs something you have no tool for, such as email, calendars, files, timers or live prices, or a fact you do not know.
Use the conversation to work out what the request refers to. If the person asks you to pick a place, pick one yourself. Never state weather or a time without the tool."""
    return [{"role": "system", "content": system},
            {"role": "user", "content": conversation(instruction, context)}]


def word_messages(instruction, context, today, tool_summary):
    system = f"""You word the answer a voice assistant speaks aloud. Today is {today:%A, %B} {today.day}, {today.year}.
Reply with one or two short sentences in plain spoken words: no markdown, no lists, no symbols.
Answer the request directly from the lookup result. Use its numbers exactly as given: do not round, convert or add any."""
    return [{"role": "system", "content": system},
            {"role": "user", "content": f"{conversation(instruction, context)}\n\nLookup result: {tool_summary}"}]


def parse_choice(text):
    """The model's JSON choice as ("tool", name, arguments), ("answer", text) or ("cannot", reason); None when the
    reply holds no such object."""
    start, end = (text or "").find("{"), (text or "").rfind("}")
    if start < 0 or end < start:
        return None
    try:
        choice = json.loads(text[start:end + 1])
    except ValueError:
        return None
    if not isinstance(choice, dict):
        return None
    tool, place = choice.get("tool"), choice.get("place")
    if tool in ("weather", "time"):
        arguments = {"place": place.strip() if isinstance(place, str) and place.strip() else None}
        if tool == "weather":
            when = str(choice.get("when") or "today").strip().lower()
            arguments["when"] = when if when in WHENS else "today"
        return ("tool", tool, arguments)
    if isinstance(choice.get("answer"), str) and choice["answer"].strip():
        return ("answer", choice["answer"])
    if "cannot" in choice:
        return ("cannot", str(choice["cannot"]))
    return None


def spoken(text):
    """Plain words, at most two sentences."""
    text = re.sub(r"[*_#`>|]+", "", text or "")
    text = re.sub(r"^\s*(?:[-•]|\d+\.)\s+", "", text, flags=re.M)
    text = " ".join(text.split())
    return " ".join(re.split(r"(?<=[.!?])\s+", text)[:2]).strip()


def unsupported_numbers(answer, sources):
    known = set(NUMBER.findall(" ".join(sources)))
    return [n for n in NUMBER.findall(answer) if n not in known]


class Agent:
    """`chat(messages, timeout_s)` returns the model's reply text. `run_tool(tool, arguments)` returns the registry's
    `ToolResult` and never raises."""

    def __init__(self, chat, run_tool, default_place, timeout_s=TIMEOUT_S, today=date.today):
        self.chat, self.run_tool, self.default_place = chat, run_tool, default_place
        self.timeout_s, self.today = timeout_s, today

    def call(self, arguments, deadline):
        instruction, context = arguments["instruction"], arguments.get("context")
        today = self.today()
        model = getattr(self.chat, "model", None)
        reply = self.chat(choose_messages(instruction, context, today, self.default_place), remaining(deadline))
        detail = {"model": model, "choice_reply": reply}
        choice = parse_choice(reply)
        if choice is None:
            raise Failed("The lookup got no usable answer.", detail)
        if choice[0] == "cannot":
            reason = spoken(choice[1])
            raise Failed(f"The lookup can't answer that. Its reason: {reason}" if reason
                         else "The lookup can't answer that with the tools it has.", {**detail, "reason": choice[1]})
        if choice[0] == "answer":
            answer = spoken(choice[1])
            if not answer:
                raise Failed("The lookup got no usable answer.", detail)
            return answer, {**detail, "source": "model"}
        _, tool, tool_arguments = choice
        tool_arguments["place"] = tool_arguments["place"] or self.default_place
        result = self.run_tool(tool, tool_arguments)
        detail["tool_result"] = result.record()
        if result.outcome != "ok":
            raise Failed(result.summary, detail)
        try:
            worded = self.chat(word_messages(instruction, context, today, result.summary), remaining(deadline))
        except client.BillingStop:
            raise
        except Exception as exc:
            return result.summary, {**detail, "source": "tool", "word_error": repr(exc)}
        answer = spoken(worded)
        sources = [result.summary, instruction, *context_lines(context)]
        extra = unsupported_numbers(answer, sources)
        if not answer or extra:
            return result.summary, {**detail, "source": "tool", "word_reply": worded, "unsupported": extra}
        return answer, {**detail, "source": "model", "word_reply": worded}


def read_key():
    """The OpenRouter key, the same way the decider's client reads it, or None when none is configured."""
    try:
        return client._api_key()
    except (RuntimeError, OSError):
        return None


class SpendStop(RuntimeError):
    pass


def spent_today(ledger):
    """US dollars this UTC day, from the ledger's recorded usage costs."""
    if not ledger.is_file():
        return 0.0
    day, total = client._now()[:10], 0.0
    for line in ledger.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if str(entry.get("at", "")).startswith(day):
            total += float((entry.get("usage") or {}).get("cost") or 0)
    return total


class OpenRouterChat:
    """One cheap paid chat model on OpenRouter, capped at DAILY_CAP_USD a day by the ledger's own cost records. One request in flight under `lock`, which a caller may share with the
    decider. Each attempt is appended to `ledger` without the request headers. Nothing is cached: an answer about
    the weather must be fresh."""

    def __init__(self, key, model=None, lock=None, ledger=LEDGER, max_tokens=300):
        self.model = model or os.environ.get("VOICE_AGENT_MODEL") or MODEL
        self.lock, self.ledger, self.max_tokens = lock or threading.Lock(), ledger, max_tokens
        self.pacer = client.Pacer()
        self._key, self._conn = key, None

    def _send(self, payload, timeout_s):
        headers = {"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"}
        started = time.perf_counter()
        try:
            if self._conn is None:
                self._conn = http.client.HTTPSConnection(HOST, timeout=timeout_s)
            self._conn.timeout = timeout_s
            if self._conn.sock is not None:
                self._conn.sock.settimeout(timeout_s)
            self._conn.request("POST", PATH, body=payload, headers=headers)
            response = self._conn.getresponse()
            raw = response.read()
        except (TimeoutError, socket.timeout, ConnectionError, http.client.HTTPException, OSError) as exc:
            if self._conn is not None:
                self._conn.close()
            self._conn = None
            return {"status": None, "error": exc, "ms": (time.perf_counter() - started) * 1000}
        try:
            body = json.loads(raw)
        except ValueError:
            body = {"unparsed": raw[:2000].decode("utf-8", "replace")}
        return {"status": response.status, "body": body, "ms": (time.perf_counter() - started) * 1000,
                "retry_after": response.getheader("retry-after")}

    def _log(self, entry):
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        with self.ledger.open("a", encoding="utf-8") as ledger:
            ledger.write(json.dumps(entry) + "\n")

    def __call__(self, messages, timeout_s):
        deadline = time.monotonic() + timeout_s
        body = {"model": self.model, "messages": messages, "max_tokens": self.max_tokens, "temperature": 0.2,
                "reasoning": {"enabled": False}, "usage": {"include": True}}
        payload = json.dumps(body).encode()
        for attempt in range(8):
            if spent_today(self.ledger) >= DAILY_CAP_USD:
                raise SpendStop(f"agent spend reached ${DAILY_CAP_USD:.2f} today")
            if client.STOP.exists():
                raise client.BillingStop(client.STOP.read_text(encoding="utf-8"))
            if self.pacer.gap_s:
                time.sleep(min(self.pacer.gap_s, remaining(deadline)))
            with self.lock:
                result = self._send(payload, remaining(deadline))
            status, response = result["status"], result.get("body")
            usage = response.get("usage") if isinstance(response, dict) else None
            error = repr(result["error"]) if "error" in result else None
            if status != 200 and isinstance(response, dict):
                error = json.dumps(response.get("error") or response)[:500]
            self._log({"at": client._now(), "route": "agent", "model": self.model, "attempt": attempt,
                       "status": status, "ms": round(result["ms"], 1), "error": error, "usage": usage})
            if status == 402 or (status and status >= 400 and client._mentions_billing(response)):
                client.STOP.write_text(json.dumps({"at": client._now(), "status": status, "label": "agent"}),
                                       encoding="utf-8")
                print(f"agent: billing stop on status {status}", file=sys.stderr)
                raise client.BillingStop(f"status {status} on agent")
            if status == 429:
                retry_after = result.get("retry_after") or ""
                self.pacer.on_429(int(retry_after) if retry_after.isdigit() else None)
                continue
            if status is None and isinstance(result["error"], (TimeoutError, socket.timeout)):
                raise TimeoutError("agent model call timed out")
            if status is None or status >= 500:
                time.sleep(min(2 ** attempt, remaining(deadline)))
                continue
            if status != 200:
                raise RuntimeError(f"agent model call failed with status {status}")
            self.pacer.on_success()
            choices = response.get("choices") or [{}]
            return (choices[0].get("message") or {}).get("content") or ""
        raise RuntimeError("agent model call kept failing")


def main():
    from voice.tools.registry import open_registry

    if len(sys.argv) != 2:
        sys.exit(__doc__)
    with open_registry() as registry:
        call = registry.ask(sys.argv[1])
        print(json.dumps(registry.run(call).record() if call else "no agent configured", indent=1, default=str))


if __name__ == "__main__":
    main()
