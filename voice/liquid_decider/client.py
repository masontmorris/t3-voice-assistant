"""Liquid AI's hosted d1.2, behind the decider's `call(route, body, label)` interface.

The segmenter builds OpenRouter Decisions bodies. `systemone` is the wire body: it keeps `state` and `questions` and
sets `model` to `d1.2`, because Liquid's endpoint is the same TypeSafe System One API that the local llama-server
speaks. The body's own model field is the cloud client's id and is replaced here, so a cached request and every record
key stay identical whichever decider answered it.

The hosted answers carry the same shape as pplx's, and a choice's `confidence` is already the margin pplx reports.
Measured on one probe: a three-way answer with probabilities 0.349/0.333/0.318 came back as confidence 0.0229, which is
(p_max - 1/3) / (1 - 1/3) to seven places. The gate's thresholds therefore read the same scale here and no rescaling
is applied. The replay in `voice.research.liquid_decider.replay` re-checks that identity on every answer it sees.

Spend. Every attempt appends the `usage.cost` the API reports to a ledger, and the day's total is the guard. A call
that starts after the UTC day passes $1.00 is refused and the run stops; at $0.02 per million input tokens that is
about 50 million tokens. A 429 is a rate limit: it backs off on Retry-After and is not a billing event. Only a 402 or
a payment error writes the billing stop.

The key comes from LIQUID_API_KEY or the env file named by VOICE_ENV_FILE and is only ever placed in the
Authorization header. Requests and responses hold transcripts, so the cache and the ledger live under $VOICE_HOME.
"""

import http.client
import json
import os
import socket
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from voice.segmentation import client as api

MODEL = "d1.2"
HOST = "api.liquid.ai"
PATH = "/decisions/v1/systemone"
# The API takes up to 128 questions per request and bills the state once, so a body is sent whole whenever it fits.
MAX_QUESTIONS = 128
TIMEOUT_S = 30
MAX_ATTEMPTS = 5
DAY_LIMIT_USD = 1.00
VOICE_HOME = Path(os.environ.get("VOICE_HOME", Path.home() / "voice"))
OUT = VOICE_HOME / "evidence" / "liquid"
CACHE = OUT / "cache"
LEDGER = OUT / "calls.jsonl"
STOP = OUT / "SPEND_STOP"
BILLING = OUT / "BILLING_STOP"
PAYMENT_WORDS = ("credit", "billing", "payment", "insufficient")


class SpendStop(RuntimeError):
    pass


class BillingStop(RuntimeError):
    pass


class CallFailed(RuntimeError):
    def __init__(self, record):
        super().__init__(f"{record['route']} failed with status {record['status']}")
        self.record = record


def cache_path(route, body):
    return CACHE / f"{route}-{api.request_hash(route, body)[:24]}.json"


def cached(route, body):
    path = cache_path(route, body)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def answered(record, body):
    """Whether `record` is a 200 that answers every question `body` asked. One that leaves a question out is not kept
    or served: cached, it would fail every identical request the same way."""
    answers = (record.get("response") or {}).get("answers")
    return record.get("status") == 200 and isinstance(answers, dict) and set(body.get("questions") or {}) <= set(answers)


def use_cache(directory):
    """Keep this run's Liquid cache and ledger in `directory`; the defaults are shared with live mode."""
    global CACHE, LEDGER, STOP, BILLING
    directory = Path(directory)
    CACHE, LEDGER, STOP, BILLING = (directory, directory / "calls.jsonl", directory / "SPEND_STOP",
                                    directory / "BILLING_STOP")


def entries(path=None):
    path = path or LEDGER
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def spend_on(day, path=None):
    """The dollars the ledger records for one UTC day, as `YYYY-MM-DD`."""
    return sum(float(entry.get("cost") or 0.0) for entry in entries(path) if (entry.get("at") or "").startswith(day))


def spend_today(path=None):
    return spend_on(datetime.now(timezone.utc).strftime("%Y-%m-%d"), path)


def _api_key():
    key = os.environ.get("LIQUID_API_KEY")
    if key:
        return key
    env_file = os.environ.get("VOICE_ENV_FILE")
    if not env_file:
        raise RuntimeError("set LIQUID_API_KEY or VOICE_ENV_FILE")
    for line in Path(env_file).read_text(encoding="utf-8-sig").splitlines():
        name, _, value = line.strip().partition("=")
        if name == "LIQUID_API_KEY":
            return value.strip().strip("\"'")
    raise RuntimeError("LIQUID_API_KEY not found in VOICE_ENV_FILE")


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def systemone(body, questions=None):
    return {"model": MODEL, "state": body["state"], "questions": questions or body["questions"]}


def chunks(questions):
    """The question dicts to send, split only when a body holds more than the API's 128."""
    names = list(questions)
    if len(names) <= MAX_QUESTIONS:
        return [questions]
    return [{name: questions[name] for name in names[start:start + MAX_QUESTIONS]}
            for start in range(0, len(names), MAX_QUESTIONS)]


def _payment_error(body):
    """A payment error, read from the response's `error` only: a transcript can say "credit" or "balance"."""
    error = body.get("error") if isinstance(body, dict) else None
    text = json.dumps(error).lower() if error is not None else ""
    return any(word in text for word in PAYMENT_WORDS)


class LiquidClient:
    """One persistent HTTPS connection to Liquid's System One endpoint, one request in flight."""

    def __init__(self, url=None, pacer=None, day_limit=DAY_LIMIT_USD, timeout_s=TIMEOUT_S, max_attempts=MAX_ATTEMPTS):
        self._url = url
        parts = urlsplit(url or os.environ.get("VOICE_LIQUID_URL", f"https://{HOST}"))
        self.secure, self.host, self.port = parts.scheme != "http", parts.hostname, parts.port
        self._conn = None
        self._key = None
        self._last = None
        self.pacer = pacer or api.Pacer()
        self.day_limit = day_limit
        self.timeout_s, self.max_attempts = timeout_s, max_attempts

    def spare(self):
        """A second client on the same terms, with a connection of its own, for a reading that runs beside one of this
        client's: this one keeps one request in flight."""
        if getattr(self, "_spare", None) is None:
            self._spare = LiquidClient(self._url, self.pacer, self.day_limit, self.timeout_s, self.max_attempts)
        return self._spare

    def _connection(self):
        if self._conn is not None:
            return self._conn, None
        connect = http.client.HTTPSConnection if self.secure else http.client.HTTPConnection
        conn = connect(self.host, self.port, timeout=self.timeout_s)
        started = time.perf_counter()
        conn.connect()
        self._conn = conn
        return conn, (time.perf_counter() - started) * 1000

    def _drop(self):
        if self._conn is not None:
            self._conn.close()
        self._conn = None

    def close(self):
        self._drop()
        if getattr(self, "_spare", None) is not None:
            self._spare.close()

    def live_calls(self):
        """The attempts this decider's ledger holds, so a run's own record counts Liquid's calls too."""
        return len(entries())

    def _send(self, method, path, payload):
        if self._key is None:
            self._key = _api_key()
        headers = {"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"}
        conn, connect_ms = self._connection()
        started_at = _now()
        started = time.perf_counter()
        try:
            conn.request(method, path, body=payload, headers=headers)
            response = conn.getresponse()
            raw = response.read()
        except (TimeoutError, socket.timeout, ConnectionError, http.client.HTTPException, OSError) as exc:
            self._drop()
            return {"status": None, "error": type(exc).__name__, "started_at": started_at,
                    "ms": (time.perf_counter() - started) * 1000, "connect_ms": connect_ms}
        ms = (time.perf_counter() - started) * 1000
        if response.getheader("connection", "").lower() == "close":
            self._drop()
        try:
            body = json.loads(raw)
        except ValueError:
            body = {"unparsed": raw[:2000].decode("utf-8", "replace")}
        return {"status": response.status, "body": body, "started_at": started_at, "ms": ms,
                "connect_ms": connect_ms, "retry_after": response.getheader("retry-after")}

    def _record(self, label, result, chunk=0, of=1):
        body = result.get("body") or {}
        usage = body.get("usage") if isinstance(body, dict) else None
        entry = {"at": result["started_at"], "route": "decisions", "model": MODEL,
                 "label": label + (f"#{chunk}" if of > 1 else ""), "status": result["status"],
                 "ms": round(result["ms"], 1),
                 "connect_ms": None if result["connect_ms"] is None else round(result["connect_ms"], 1),
                 "input_tokens": (usage or {}).get("input_tokens"),
                 "output_tokens": (usage or {}).get("output_tokens"),
                 "cost": float((usage or {}).get("cost") or 0.0),
                 "error": result.get("error")}
        LEDGER.parent.mkdir(parents=True, exist_ok=True)
        with LEDGER.open("a", encoding="utf-8") as ledger:
            ledger.write(json.dumps(entry) + "\n")
        return entry

    def _guard(self, label):
        if STOP.exists():
            raise SpendStop(STOP.read_text(encoding="utf-8"))
        spent = spend_today()
        if spent >= self.day_limit:
            self._stop(spent, label)

    def _stop(self, spent, label):
        STOP.parent.mkdir(parents=True, exist_ok=True)
        STOP.write_text(json.dumps({"at": _now(), "spent_usd": round(spent, 6), "label": label}),
                        encoding="utf-8")
        raise SpendStop(f"the day's Liquid spend reached ${spent:.4f}, at the ${self.day_limit:.2f} limit")

    def _exchange(self, body, label):
        """Send every chunk of one body once, merging the answers. Returns (status, response, entries, results)."""
        parts = chunks(body["questions"])
        answers, usage = {}, {"input_tokens": 0, "output_tokens": 0, "cost": 0.0}
        records, status, sent = [], 200, []
        for index, questions in enumerate(parts):
            result = self._send("POST", PATH, json.dumps(systemone(body, questions), sort_keys=True).encode())
            sent.append(result)
            records.append(self._record(label, result, index + 1, len(parts)))
            status = result["status"]
            if status != 200:
                return status, result.get("body"), records, sent
            response = result["body"] or {}
            answers.update(response.get("answers") or {})
            for name in usage:
                usage[name] += float((response.get("usage") or {}).get(name) or 0.0)
        return 200, {"model": MODEL, "answers": answers, "usage": usage}, records, sent

    def call(self, route, body, label="", fresh=False, *, cache_record=True):
        """Return the cached record for this request, calling the API at most once per distinct request.

        `fresh` skips cache lookup and never overwrites the cached entry. `cache_record=False` skips both cache reads
        and writes while keeping the spend ledger. A body with
        no questions is answered here: the API rejects it with a 422 while pplx answers it with an empty answer set
        (measured on one live turn), and there is nothing to ask and nothing to bill."""
        assert route == "decisions", route
        hit = None if fresh or not cache_record else cached(route, body)
        if hit is not None and answered(hit, body):
            return hit
        if not body.get("questions"):
            record = {"route": route, "label": label, "request": body, "status": 200, "model": MODEL,
                      "response": {"model": MODEL, "answers": {},
                                   "usage": {"input_tokens": 0, "output_tokens": 0, "cost": 0.0}},
                      "timing": {"started_at": _now(), "ms": 0.0, "connect_ms": None},
                      "attempts": [], "requests": 0}
            return self._cache_record(route, body, record) if cache_record else record
        if BILLING.exists():
            raise BillingStop(BILLING.read_text(encoding="utf-8"))
        self._guard(label)
        payload_attempts, sent, status, response, started_at = [], [], None, None, None
        for attempt in range(self.max_attempts):
            again = attempt + 1 < self.max_attempts
            if self._last is not None and self.pacer.gap_s:
                time.sleep(max(0.0, self.pacer.gap_s - (time.monotonic() - self._last)))
            self._last = time.monotonic()
            status, response, records, results = self._exchange(body, label)
            payload_attempts += records
            sent = results
            started_at = started_at or results[0]["started_at"]
            spent = spend_today()
            if status == 402 or (status and status >= 400 and _payment_error(response)):
                BILLING.parent.mkdir(parents=True, exist_ok=True)
                BILLING.write_text(json.dumps({"at": _now(), "status": status, "label": label}), encoding="utf-8")
                raise BillingStop(f"status {status} on {label}")
            if spent >= self.day_limit:
                self._stop(spent, label)
            if status == 429:
                retry_after = results[-1].get("retry_after") or ""
                self.pacer.on_429(int(retry_after) if retry_after.isdigit() else None)
                continue
            if status in (500, 502, 503, 504, 524, 529) or status is None:
                if again:
                    time.sleep(2 ** (attempt + 1))
                continue
            self.pacer.on_success()
            break
        ms = sum(result["ms"] for result in sent) if sent else 0.0
        connect_ms = sent[0]["connect_ms"] if sent else None
        record = {"route": route, "label": label, "request": body, "status": status, "response": response,
                  "model": MODEL, "timing": {"started_at": started_at, "ms": ms, "connect_ms": connect_ms},
                  "attempts": payload_attempts,
                  "requests": len(sent)}
        # A failed call (a connection reset, a timeout) is never kept: cached, it answered every identical request with
        # the same failure, so the retry of a line the decider had never judged was refused too.
        return self._cache_record(route, body, record) if cache_record and answered(record, body) else record

    def _cache_record(self, route, body, record):
        path = cache_path(route, body)
        path.parent.mkdir(parents=True, exist_ok=True)
        kept = cached(route, body)
        if kept is None or not answered(kept, body):
            path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        return record
