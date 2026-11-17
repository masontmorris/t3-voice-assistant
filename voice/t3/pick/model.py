"""One free OpenRouter text model picks a thread, behind one function.

Every request asserts a `:free` model id, runs one at a time, backs off exponentially on 429, and is
cached on disk by request hash. A 402 or any billing language stops every later call in the process.
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
MAX_ATTEMPTS = 5
TIMEOUT_S = 30

SYSTEM = (
	"You match one spoken request to one thread in a list. "
	'Return one JSON object and nothing else: {"choice": "<id>" or null, "candidates": ["<id>", ...]}. '
	"Set choice only when exactly one thread is clearly what the speaker means. "
	"When several threads are plausible, set choice null and list their ids in candidates. "
	"When nothing in the list matches, set choice null and candidates empty. "
	"Use only ids that appear in the list. Never explain."
)


class BillingStop(RuntimeError):
	pass


class ModelFailed(RuntimeError):
	pass


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


def _check_refusal(text):
	lower = (text or "").lower()
	if "credits" in lower or "billing" in lower or "insufficient" in lower:
		raise BillingStop(text[:200])


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

	def _path(self, messages):
		blob = json.dumps({"model": self.model, "attempt": self.attempt, "messages": messages}, sort_keys=True)
		return self.cache_dir / f"{hashlib.sha256(blob.encode()).hexdigest()[:32]}.json"

	def complete(self, messages):
		assert self.model.endswith(":free"), self.model
		if self.paused:
			raise ModelFailed(self.paused)
		path = self._path(messages)
		if path.exists():
			return json.loads(path.read_text(encoding="utf-8"))
		body = {"model": self.model, "temperature": 0, "max_tokens": 300, "provider": {"sort": "latency"}, "messages": messages}
		key = _env_key()
		gap = 2.0
		last = None
		for _ in range(MAX_ATTEMPTS):
			self.calls += 1
			start = time.perf_counter()
			try:
				payload = _post(body, key)
			except urllib.error.HTTPError as e:
				text = e.read().decode(errors="replace")
				if e.code == 402:
					self.paused = "402 from OpenRouter; every later call stops"
					raise BillingStop(self.paused)
				_check_refusal(text)
				last = f"http {e.code}: {text[:160]}"
				if e.code == 429:
					time.sleep(gap)
					gap = min(30.0, gap * 2)
					continue
				raise ModelFailed(last)
			elapsed = time.perf_counter() - start
			text = payload["choices"][0]["message"].get("content") or ""
			_check_refusal(text)
			record = {
				"text": text, "ms": round(elapsed * 1000), "model": payload.get("model", self.model),
				"usage": payload.get("usage", {}),
			}
			path.write_text(json.dumps(record, indent=1), encoding="utf-8")
			self.ledger.append({"attempt": self.attempt, "ms": record["ms"], "usage": record["usage"]})
			return record
		self.paused = f"gave up after {MAX_ATTEMPTS} attempts: {last}"
		raise ModelFailed(self.paused)


@dataclass
class LocalClient:
	"""A local llama-server on the GPU, same interface as `ModelClient`, JSON mode on."""

	model: str = "d1-2B-Q4_K_M"
	url: str = ""
	attempt: int = 1
	calls: int = 0
	paused: str | None = None

	def __post_init__(self):
		self.url = self.url or os.environ.get("VOICE_D1_URL", "http://127.0.0.1:8096")

	def complete(self, messages):
		body = {
			"model": self.model, "temperature": 0, "max_tokens": 300,
			"response_format": {"type": "json_object"}, "messages": messages,
		}
		req = urllib.request.Request(
			f"{self.url}/v1/chat/completions",
			data=json.dumps(body).encode(),
			headers={"Content-Type": "application/json"},
		)
		start = time.perf_counter()
		self.calls += 1
		try:
			with urllib.request.urlopen(req, timeout=TIMEOUT_S * 2) as r:
				payload = json.load(r)
		except urllib.error.HTTPError as e:
			raise ModelFailed(f"local {e.code}: {e.read().decode(errors='replace')[:160]}")
		elapsed = time.perf_counter() - start
		text = payload["choices"][0]["message"].get("content") or ""
		return {"text": text, "ms": round(elapsed * 1000), "model": self.model, "usage": payload.get("usage", {})}


def pick_thread(utterance, threads, projects, now, client):
	"""Return (Pick, meta). `threads` is what the picker is allowed to see, newest first."""
	listed = [t["threadId"] for t in threads]
	titles = {t["threadId"]: t.get("title", "") for t in threads}
	if not threads:
		return Pick.ask([], "There are no threads to check."), {"ms": 0}
	record = client.complete(render(utterance, threads, projects, now))
	try:
		answer = parse_json(record["text"])
	except (ValueError, json.JSONDecodeError) as e:
		return Pick.ask(listed[:3], question_for([titles[i] for i in listed[:3]]),
		                f"unparseable reply: {e}"), {"ms": record["ms"], "raw": record["text"]}
	choice = answer.get("choice")
	candidates = [c for c in (answer.get("candidates") or []) if c in listed]
	if choice in listed:
		return Pick.one(choice, "model"), {"ms": record["ms"], "raw": record["text"]}
	if choice:
		return Pick.coerce(choice, listed, question_for([titles[i] for i in listed[:3]])), {"ms": record["ms"], "raw": record["text"]}
	if candidates:
		return Pick.ask(candidates, question_for([titles[i] for i in candidates]),
		                "model"), {"ms": record["ms"], "raw": record["text"]}
	return Pick.none("model"), {"ms": record["ms"], "raw": record["text"]}
