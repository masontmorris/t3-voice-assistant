"""OpenRouter Decisions and Extractions client for the free decider.

Every model response is cached on disk by request hash and never re-requested. Every HTTP attempt is appended to
the call ledger, which also enforces the round's call budget. The API key comes from OPENROUTER_API_KEY or from the
env file named by VOICE_ENV_FILE and is only ever placed in the Authorization header.
"""

import hashlib
import http.client
import json
import os
import socket
import time
from datetime import datetime, timezone
from pathlib import Path

MODEL = "perplexity/pplx-decider-flash-v1.3:free"
HOST = "openrouter.ai"
ROUTES = {"decisions": "/api/alpha/decisions", "extractions": "/api/alpha/extractions"}
ROOT = Path(__file__).resolve().parent
CACHE = ROOT / "cache"
LEDGER = ROOT / "calls.jsonl"
STOP = ROOT / "BILLING_STOP"
# Round 2 used 157 ledger attempts; the variants round adds its own cap of 400.
BUDGET = 157 + 400
TIMEOUT_S = 30
MAX_ATTEMPTS = 5


class BillingStop(RuntimeError):
    pass


class BudgetExhausted(RuntimeError):
    pass


class CallFailed(RuntimeError):
    def __init__(self, record):
        super().__init__(f"{record['route']} failed with status {record['status']}")
        self.record = record


def request_hash(route, body):
    return hashlib.sha256((route + "\n" + json.dumps(body, sort_keys=True)).encode()).hexdigest()


def cache_path(route, body):
    return CACHE / f"{route}-{request_hash(route, body)[:24]}.json"


def cached(route, body):
    path = cache_path(route, body)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def ledger_attempts():
    if not LEDGER.exists():
        return 0
    return sum(1 for line in LEDGER.read_text(encoding="utf-8").splitlines() if json.loads(line)["route"] in ROUTES)


def _api_key():
    key = os.environ.get("OPENROUTER_API_KEY")
    if key:
        return key
    env_file = os.environ.get("VOICE_ENV_FILE")
    if not env_file:
        raise RuntimeError("set OPENROUTER_API_KEY or VOICE_ENV_FILE")
    for line in Path(env_file).read_text(encoding="utf-8-sig").splitlines():
        name, _, value = line.strip().partition("=")
        if name == "OPENROUTER_API_KEY":
            return value.strip().strip("\"'")
    raise RuntimeError("OPENROUTER_API_KEY not found in VOICE_ENV_FILE")


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _mentions_billing(body):
    text = json.dumps(body.get("error", "")).lower() if isinstance(body, dict) else str(body).lower()
    return any(word in text for word in ("credit", "billing", "payment", "insufficient"))


def _nonzero_cost(body):
    usage = body.get("usage") if isinstance(body, dict) else None
    return bool(usage) and (usage.get("cost") or 0) != 0


class Client:
    """One persistent HTTPS connection, one request in flight."""

    def __init__(self):
        self._conn = None
        self._key = None

    def _connection(self):
        if self._conn is not None:
            return self._conn, None
        conn = http.client.HTTPSConnection(HOST, timeout=TIMEOUT_S)
        started = time.perf_counter()
        conn.connect()
        self._conn = conn
        return conn, (time.perf_counter() - started) * 1000

    def _drop(self):
        if self._conn is not None:
            self._conn.close()
        self._conn = None

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

    def call(self, route, body, label=""):
        """Return the cached record for this request, calling the API at most once per distinct request."""
        assert body["model"].endswith(":free"), body["model"]
        hit = cached(route, body)
        if hit is not None:
            return hit
        if STOP.exists():
            raise BillingStop(STOP.read_text(encoding="utf-8"))
        payload = json.dumps(body, sort_keys=True).encode()
        attempts = []
        for attempt in range(MAX_ATTEMPTS):
            if ledger_attempts() >= BUDGET:
                raise BudgetExhausted(f"{BUDGET} attempts used")
            result = self._send("POST", ROUTES[route], payload)
            entry = {"at": result["started_at"], "route": route, "hash": request_hash(route, body)[:24],
                     "label": label, "status": result["status"], "ms": round(result["ms"], 1),
                     "connect_ms": None if result["connect_ms"] is None else round(result["connect_ms"], 1),
                     "error": result.get("error")}
            with LEDGER.open("a", encoding="utf-8") as ledger:
                ledger.write(json.dumps(entry) + "\n")
            attempts.append(entry)
            status = result["status"]
            if status == 402 or (status and status >= 400 and _mentions_billing(result["body"])) \
                    or (status == 200 and _nonzero_cost(result["body"])):
                STOP.write_text(json.dumps({"at": entry["at"], "status": status, "label": label}), encoding="utf-8")
                raise BillingStop(f"status {status} on {label}")
            if status in (429, 500, 502, 503, 524, 529) or status is None:
                delay = 2 ** (attempt + 1)
                if status == 429 and (result.get("retry_after") or "").isdigit():
                    delay = max(delay, int(result["retry_after"]))
                time.sleep(delay)
                continue
            break
        record = {"route": route, "label": label, "request": body, "status": result["status"],
                  "response": result.get("body"), "timing": {"started_at": result["started_at"],
                  "ms": result["ms"], "connect_ms": result["connect_ms"]}, "attempts": attempts}
        CACHE.mkdir(exist_ok=True)
        cache_path(route, body).write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        return record

    def key_info(self):
        """GET /api/v1/key and keep only limit, usage, rate-limit and free-tier fields. Not a model call."""
        result = self._send("GET", "/api/v1/key", None)
        data = (result.get("body") or {}).get("data", {})
        keep = {k: v for k, v in data.items()
                if any(word in k for word in ("limit", "usage", "rate", "free", "tier"))}
        return {"status": result["status"], "ms": result["ms"], "connect_ms": result["connect_ms"], "fields": keep}
