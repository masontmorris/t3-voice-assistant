"""Probe client for Liquid's hosted System One decisions API, with audio. Throwaway research code.

Loads LIQUID_API_KEY in-process from the env file named by VOICE_ENV_FILE (default: the main checkout's .env). The key is
only placed in the Authorization header. Every call is appended to a ledger with latency, usage and cost, and the run
stops once the summed cost passes SPEND_CAP.
"""
import base64
import hashlib
import http.client
import io
import json
import os
import time
import wave
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

HOME = Path(os.environ.get("VOICE_HOME", Path.home() / "voice"))
OUT = HOME / "research" / "audio-decider"
LEDGER = OUT / "liquid_ledger.jsonl"
CACHE = OUT / "liquid_cache"
HOST = "api.liquid.ai"
PATH = "/decisions/v1/systemone"
MODEL = "d1.2"
SPEND_CAP = 0.50
ENV_FILE = Path(os.environ.get("VOICE_ENV_FILE", Path.home() / "source/repos/t3-voice-assistant/.env"))


class SpendCap(RuntimeError):
    pass


def spent():
    if not LEDGER.exists():
        return 0.0
    return sum((json.loads(l).get("cost") or 0.0) for l in LEDGER.read_text().splitlines() if l)


def _key():
    for line in ENV_FILE.read_text(encoding="utf-8-sig").splitlines():
        name, _, value = line.strip().partition("=")
        if name == "LIQUID_API_KEY":
            return value.strip().strip("\"'")
    raise RuntimeError("LIQUID_API_KEY not found")


def wav_b64(samples, rate=16000):
    """16-bit mono PCM WAV as a data URI from float or int16 samples."""
    x = np.asarray(samples)
    if x.dtype != np.int16:
        x = (np.clip(x, -1, 1) * 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(x.tobytes())
    return "data:audio/wav;base64," + base64.b64encode(buf.getvalue()).decode()


def read_wav(path):
    with wave.open(str(path)) as w:
        rate, n, ch = w.getframerate(), w.getnframes(), w.getnchannels()
        x = np.frombuffer(w.readframes(n), dtype=np.int16).reshape(-1, ch).mean(axis=1).astype(np.int16)
    return x, rate


def resample16k(x, rate):
    if rate == 16000:
        return x
    n = int(round(len(x) * 16000 / rate))
    return np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x.astype(np.float32)).astype(np.int16)


class Client:
    def __init__(self):
        self._conn = None
        self._k = None

    def call(self, body, label="", cache_key=None, fresh=False):
        """POST one decisions body. Returns the cached record unless `fresh`. Record: response, ms, usage, cost."""
        key = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:24] if cache_key is None else cache_key
        CACHE.mkdir(parents=True, exist_ok=True)
        path = CACHE / f"{key}.json"
        if path.exists() and not fresh:
            return json.loads(path.read_text())
        if spent() >= SPEND_CAP:
            raise SpendCap(f"spend {spent():.4f} >= {SPEND_CAP}")
        self._k = self._k or _key()
        payload = json.dumps({"model": MODEL, **body}).encode()
        for attempt in range(4):
            if self._conn is None:
                self._conn = http.client.HTTPSConnection(HOST, timeout=60)
            started = time.perf_counter()
            try:
                self._conn.request("POST", PATH, payload, {"Authorization": f"Bearer {self._k}",
                                                           "Content-Type": "application/json"})
                r = self._conn.getresponse()
                raw = r.read()
            except Exception as exc:
                self._conn.close()
                self._conn = None
                err = type(exc).__name__
                time.sleep(2 ** attempt)
                continue
            ms = (time.perf_counter() - started) * 1000
            if r.getheader("connection", "").lower() == "close":
                self._conn.close()
                self._conn = None
            try:
                resp = json.loads(raw)
            except ValueError:
                resp = {"unparsed": raw[:500].decode("utf-8", "replace")}
            usage = resp.get("usage") if isinstance(resp, dict) else None
            cost = (usage or {}).get("cost") if isinstance(usage, dict) else None
            rec = {"label": label, "status": r.status, "ms": round(ms, 1), "bytes": len(payload), "usage": usage,
                   "cost": cost, "at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                   "response": resp}
            with LEDGER.open("a") as f:
                f.write(json.dumps({k: v for k, v in rec.items() if k != "response"}) + "\n")
            if r.status == 429 or r.status >= 500:
                time.sleep(2 ** (attempt + 1))
                continue
            if r.status == 200:
                path.write_text(json.dumps(rec))
            return rec
        raise RuntimeError(f"call failed: {err if 'err' in dir() else 'status'}")
