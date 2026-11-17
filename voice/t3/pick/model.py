"""One free OpenRouter text model picks a thread, behind one function.

Every request asserts a `:free` model id, runs one at a time, retries 429 and 5xx with
exponential backoff, and is cached on disk by request hash. Only an HTTP 402 or an error
payload that says billing stops every later call; a model's own reply is never scanned for
those words. Reasoning is off, because round 1's truncations came from reasoning tokens
eating the output budget, and every call records its reasoning and content tokens.
"""

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from .pick import Pick, question_for

MODEL = "particle-ai/particle-1:free"
ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
MAX_ATTEMPTS = 8
TIMEOUT_S = 30
MAX_TOKENS = 200
REASONING = {"enabled": False}
# The free tier allows 20 requests a minute, so one request in flight is not enough on its own.
MIN_GAP_S = float(os.environ.get("VOICE_MODEL_MIN_GAP_S", "3.2"))
RATE_LIMIT_WAIT_S = 60.0

SYSTEM = (
	"You match one spoken request to one thread in a list. "
	'Return one JSON object and nothing else: {"choice": "<id>" or null, "candidates": ["<id>", ...]}. '
	"Set choice only when exactly one thread is clearly what the speaker means. "
	"When several threads are plausible, set choice null and list their ids in candidates. "
	"When nothing in the list matches, set choice null and candidates empty. "
	"Use only ids that appear in the list. Never explain."
)

ANSWER_SCHEMA = {
	"type": "object",
	"properties": {
		"choice": {"type": ["string", "null"]},
		"candidates": {"type": "array", "items": {"type": "string"}, "maxItems": 4},
	},
	"required": ["choice", "candidates"],
}

NO_CHOICE = {None, "", "null", "none", "nil"}


class BillingStop(RuntimeError):
	pass


class ModelFailed(RuntimeError):
	pass


def error_is_billing(status, text):
	"""True only for an HTTP 402 or an OpenRouter error payload that names billing."""
	if status == 402:
		return True
	try:
		data = json.loads(text or "")
	except (ValueError, TypeError):
		return False
	error = data.get("error") if isinstance(data, dict) else None
	if not isinstance(error, dict):
		return False
	message = f"{error.get('message', '')} {error.get('code', '')}".lower()
	return any(word in message for word in ("credit", "billing", "insufficient_quota", "payment required"))


def _env_key():
	key = os.environ.get("OPENROUTER_API_KEY")
	if key:
		return key
	env_file = os.environ.get("VOICE_ENV_FILE")
	candidates = [Path(env_file)] if env_file else [Path.home() / ".t3" / "voice.env"]
	for path in candidates:
		if path.exists():
			for line in path.read_text(encoding="utf-8").splitlines():
				if line.strip().startswith("OPENROUTER_API_KEY"):
					return line.split("=", 1)[1].strip().strip('"').strip("'")
	raise SystemExit("OPENROUTER_API_KEY or VOICE_ENV_FILE with that key is required")


def _post(body, key):
	req = urllib.request.Request(
		ENDPOINT,
		data=json.dumps(body).encode(),
		headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
	)
	with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
		return json.load(r)


def render(utterance, threads, projects, now):
	lines = [f"Today is {now[:10]}. Threads, newest activity first, one per line as id | title | project | activity | last activity:"]
	for t in threads:
		lines.append(" | ".join([
			t["threadId"], t.get("title", ""), projects.get(t["projectId"], t["projectId"]),
			t.get("activity", "idle"), str(t.get("lastActivityAt", ""))[:16],
		]))
	lines.append("")
	lines.append(f'Spoken request: "{utterance}"')
	return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": "\n".join(lines)}]


def parse_json(text):
	match = re.search(r"\{.*\}", text or "", re.S)
	if not match:
		raise ValueError("no JSON object in the reply")
	return json.loads(match.group(0))


def tokens(usage):
	"""Content and reasoning tokens from one usage block."""
	usage = usage or {}
	reasoning = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
	completion = usage.get("completion_tokens")
	content = None if completion is None else completion - (reasoning or 0)
	return {"reasoning_tokens": reasoning or 0, "content_tokens": content,
	        "prompt_tokens": usage.get("prompt_tokens"), "completion_tokens": completion}


@dataclass
class ModelClient:
	model: str = MODEL
	cache_dir: Path | None = None
	attempt: int = 1
	calls: int = 0
	paused: str | None = None
	ledger: list = field(default_factory=list)

	def __post_init__(self):
		if not self.model.endswith(":free"):
			raise ValueError(f"refusing a non-free model id: {self.model}")
		self.cache_dir = self.cache_dir or Path(os.environ.get("VOICE_HOME", Path.home() / "voice")) / "evals" / "twinb-pick" / "cache"
		self.cache_dir.mkdir(parents=True, exist_ok=True)
		self._last = 0.0

	def _body(self, messages):
		return {
			"model": self.model, "temperature": 0, "max_tokens": MAX_TOKENS,
			"reasoning": dict(REASONING), "provider": {"sort": "latency"}, "messages": messages,
		}

	def _path(self, body):
		blob = json.dumps({"attempt": self.attempt, **body}, sort_keys=True)
		return self.cache_dir / f"{hashlib.sha256(blob.encode()).hexdigest()[:32]}.json"

	def _pace(self):
		wait = MIN_GAP_S - (time.monotonic() - self._last)
		if wait > 0:
			time.sleep(wait)
		self._last = time.monotonic()

	def complete(self, messages):
		assert self.model.endswith(":free"), self.model
		if self.paused:
			raise ModelFailed(self.paused)
		body = self._body(messages)
		path = self._path(body)
		if path.exists():
			return json.loads(path.read_text(encoding="utf-8"))
		key = _env_key()
		gap = 2.0
		last = None
		for _ in range(MAX_ATTEMPTS):
			self._pace()
			self.calls += 1
			start = time.perf_counter()
			try:
				payload = _post(body, key)
			except urllib.error.HTTPError as e:
				text = e.read().decode(errors="replace")
				if error_is_billing(e.code, text):
					self.paused = f"{e.code} billing refusal from OpenRouter; every later call stops"
					raise BillingStop(self.paused)
				if e.code == 429:
					last = f"http 429: {text[:160]}"
					time.sleep(RATE_LIMIT_WAIT_S)
					continue
				if e.code >= 500:
					last = f"http {e.code}: {text[:160]}"
					time.sleep(gap)
					gap = min(30.0, gap * 2)
					continue
				raise ModelFailed(f"http {e.code}: {text[:160]}")
			except (urllib.error.URLError, TimeoutError) as e:
				last = f"{type(e).__name__}: {e}"
				time.sleep(gap)
				gap = min(30.0, gap * 2)
				continue
			elapsed = time.perf_counter() - start
			choice = (payload.get("choices") or [{}])[0]
			text = (choice.get("message") or {}).get("content") or ""
			record = {
				"text": text, "ms": round(elapsed * 1000), "model": payload.get("model", self.model),
				"usage": payload.get("usage", {}), "tokens": tokens(payload.get("usage")),
				"finish_reason": choice.get("finish_reason"),
			}
			path.write_text(json.dumps(record, indent=1), encoding="utf-8")
			self.ledger.append({"attempt": self.attempt, "ms": record["ms"], **record["tokens"]})
			return record
		self.paused = f"gave up after {MAX_ATTEMPTS} attempts: {last}"
		raise ModelFailed(self.paused)


@dataclass
class LocalClient:
	"""A local llama-server on the GPU, same interface as `ModelClient`, schema-constrained JSON."""

	model: str = "local-instruct"
	url: str = ""
	attempt: int = 1
	calls: int = 0
	paused: str | None = None
	ledger: list = field(default_factory=list)

	def __post_init__(self):
		self.url = self.url or os.environ.get("VOICE_LOCAL_URL", "http://127.0.0.1:8097")

	def complete(self, messages):
		base = {"model": self.model, "temperature": 0, "max_tokens": MAX_TOKENS, "messages": messages}
		shapes = [
			{"type": "json_object", "schema": ANSWER_SCHEMA},
			{"type": "json_object"},
		]
		last = None
		for shape in shapes:
			start = time.perf_counter()
			self.calls += 1
			req = urllib.request.Request(
				f"{self.url}/v1/chat/completions",
				data=json.dumps({**base, "response_format": shape}).encode(),
				headers={"Content-Type": "application/json"},
			)
			try:
				with urllib.request.urlopen(req, timeout=TIMEOUT_S * 2) as r:
					payload = json.load(r)
			except urllib.error.HTTPError as e:
				last = f"local {e.code}: {e.read().decode(errors='replace')[:160]}"
				continue
			except (urllib.error.URLError, TimeoutError) as e:
				raise ModelFailed(f"local unreachable: {e}")
			elapsed = time.perf_counter() - start
			choice = (payload.get("choices") or [{}])[0]
			usage = payload.get("usage", {})
			return {
				"text": (choice.get("message") or {}).get("content") or "",
				"ms": round(elapsed * 1000), "model": self.model, "usage": usage,
				"tokens": tokens(usage), "finish_reason": choice.get("finish_reason"),
				"schema": shape.get("schema") is not None,
			}
		raise ModelFailed(last or "local server refused every response shape")


def pick_thread(utterance, threads, projects, now, client):
	"""Return (Pick, meta). `threads` is what the picker is allowed to see, newest first."""
	listed = [t["threadId"] for t in threads]
	titles = {t["threadId"]: t.get("title", "") for t in threads}
	if not threads:
		return Pick.ask([], "There are no threads to check."), {"ms": 0, "parse": "ok"}
	record = client.complete(render(utterance, threads, projects, now))
	meta = {"ms": record["ms"], "parse": "ok", "raw": record["text"],
	        "tokens": record.get("tokens", {}), "schema": record.get("schema")}
	try:
		answer = parse_json(record["text"])
	except (ValueError, json.JSONDecodeError) as e:
		meta["parse"] = "unparseable"
		meta["parse_error"] = str(e)
		return Pick.ask(listed[:3], question_for([titles[i] for i in listed[:3]]),
		                f"unparseable reply: {e}"), meta
	choice = answer.get("choice")
	if isinstance(choice, str) and choice.strip().lower() in NO_CHOICE:
		choice = None
	candidates = [c for c in (answer.get("candidates") or []) if c in listed]
	if choice in listed:
		return Pick.one(choice, listed, "model"), meta
	if choice:
		return Pick.coerce(choice, listed, question_for([titles[i] for i in listed[:3]])), meta
	if candidates:
		return Pick.ask(candidates, question_for([titles[i] for i in candidates]), "model"), meta
	return Pick.none("model"), meta
