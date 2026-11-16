"""d1-2B on llama.cpp's `/v1/systemone`, behind the same `call(route, body, label)` interface as the cloud client.

The segmenter builds OpenRouter Decisions bodies. `to_systemone` and `from_systemone` are the one adapter between the
two. The request drops the OpenRouter `model` field and keeps `state` and `questions`, which `/v1/systemone` takes as
they are. The answers come back in the same TypeSafe shape (`type`, `choice`, `probabilities`, `confidence`, `noul`),
except that `confidence` is on a different scale. No call leaves the machine.
"""

import http.client
import json
import os
import subprocess
import time
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

VOICE_HOME = Path(os.environ.get("VOICE_HOME", Path.home() / "voice"))
SERVER = VOICE_HOME / "llamacpp" / "bin" / "llama-server.exe"
MODEL = VOICE_HOME / "gguf" / "d1-2B-Q4_K_M.gguf"
DEFAULT_PORT = 8096
# The card uses -ub 8192. Each question is read in one physical batch, and every recorded question prompt fits in
# 2048 tokens, which gave byte-identical answers at the same latency with about 970 MiB less VRAM
# (docs/voice/local-decider.md). 512 is too small: one question needs 521 tokens.
FLAGS = ("-b", "8192", "-ub", "2048", "-fa", "on")
TIMEOUT_S = 60


def to_systemone(body):
    """The `/v1/systemone` body for one OpenRouter Decisions body."""
    return {"state": body["state"], "questions": body["questions"]}


def from_systemone(response):
    """Rescale each choice `confidence` to pplx's scale, keeping d1's own value as `top_probability`.

    llama.cpp reports the winning option's probability. pplx reports (p_max - 1/n) / (1 - 1/n), which is 0 when every
    option is equally likely (measured on cached pplx answers). The hybrid's readiness near-tie hold compares that value
    with 0.2, which a top probability of 3 options can never fall under.
    """
    answers = response.get("answers") if isinstance(response, dict) else None
    if not isinstance(answers, dict):
        return response
    mapped = {}
    for name, answer in answers.items():
        probs = answer.get("probabilities") if isinstance(answer, dict) and answer.get("type") == "choice" else None
        if isinstance(probs, dict) and len(probs) > 1 and all(isinstance(p, (int, float)) for p in probs.values()):
            chance = 1 / len(probs)
            answer = {**answer, "top_probability": answer.get("confidence"),
                      "confidence": max(0.0, (max(probs.values()) - chance) / (1 - chance))}
        mapped[name] = answer
    return {**response, "answers": mapped}


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class D1Client:
    """One persistent HTTP connection to a local llama-server, one request in flight."""

    def __init__(self, url=None):
        parts = urlsplit(url or os.environ.get("VOICE_D1_URL", f"http://127.0.0.1:{DEFAULT_PORT}"))
        self.host, self.port = parts.hostname, parts.port
        self._conn = None

    def _post(self, payload):
        for attempt in range(2):
            if self._conn is None:
                self._conn = http.client.HTTPConnection(self.host, self.port, timeout=TIMEOUT_S)
            try:
                self._conn.request("POST", "/v1/systemone", body=payload, headers={"Content-Type": "application/json"})
                response = self._conn.getresponse()
                return response.status, response.read()
            except (ConnectionError, http.client.HTTPException):
                self._conn.close()
                self._conn = None
                if attempt:
                    raise

    def close(self):
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def call(self, route, body, label=""):
        """Ask every question in `body` and return a record shaped like the cloud client's cache record."""
        assert route == "decisions", route
        payload = json.dumps(to_systemone(body)).encode()
        started_at = _now()
        started = time.perf_counter()
        status, raw = self._post(payload)
        ms = (time.perf_counter() - started) * 1000
        try:
            response = from_systemone(json.loads(raw))
        except ValueError:
            response = {"unparsed": raw[:2000].decode("utf-8", "replace")}
        return {"route": route, "label": label, "request": body, "status": status, "response": response,
                "timing": {"started_at": started_at, "ms": ms, "connect_ms": None}, "attempts": []}


class LlamaServer:
    """Starts one llama-server and stops that one process on exit. `startup_s` is launch to first healthy reply."""

    def __init__(self, args, port, log_path):
        self.args, self.port, self.log_path, self.proc = list(args), port, Path(log_path), None

    def __enter__(self):
        command = [str(SERVER), *self.args, "--host", "127.0.0.1", "--port", str(self.port)]
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log = open(self.log_path, "w")
        started = time.perf_counter()
        self.proc = subprocess.Popen(command, stdout=self.log, stderr=subprocess.STDOUT)
        self.log_path.with_suffix(".pid").write_text(str(self.proc.pid))
        try:
            self._wait_healthy()
        except BaseException:
            self.__exit__()
            raise
        self.startup_s = time.perf_counter() - started
        return self

    def _wait_healthy(self, timeout=180):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"llama-server exited with {self.proc.returncode}, see {self.log_path}")
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/health", timeout=2) as response:
                    if response.status == 200:
                        return
            except OSError:
                pass
            time.sleep(0.2)
        raise RuntimeError("llama-server did not become healthy")

    def __exit__(self, *exc):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=30)
        self.log.close()
        self.log_path.with_suffix(".pid").unlink(missing_ok=True)


def d1_server(port=DEFAULT_PORT, log_path=None, flags=FLAGS):
    """d1-2B with every layer on the GPU and one slot, so one request is in flight at a time."""
    log_path = log_path or VOICE_HOME / "evidence" / "local-decider" / "d1-server.log"
    return LlamaServer(["-m", str(MODEL), "-ngl", "99", "-np", "1", *flags], port, log_path)


@contextmanager
def decider(name, log_dir=None):
    """The decider a run asked for: `cloud` (the default, OpenRouter) or `d1` (a local llama-server it starts)."""
    if name == "cloud":
        from voice.segmentation import client as api
        yield api.Client()
        return
    if name != "d1":
        raise ValueError(f"unknown decider {name!r}")
    if os.environ.get("VOICE_D1_URL"):
        yield D1Client()
        return
    log_path = Path(log_dir) / "d1-server.log" if log_dir else None
    with d1_server(log_path=log_path) as server:
        print(f"d1 llama-server pid {server.proc.pid} healthy after {server.startup_s:.1f} s", flush=True)
        yield D1Client(f"http://127.0.0.1:{server.port}")


def default_decider():
    return os.environ.get("VOICE_DECIDER", "cloud")
