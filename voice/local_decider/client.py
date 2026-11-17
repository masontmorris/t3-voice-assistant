"""d1-2B on llama.cpp's or mistral.rs's `/v1/systemone`, behind the same `call(route, body, label)` interface as the
cloud client.

The segmenter builds OpenRouter Decisions bodies. `to_systemone` and `from_systemone` are the one adapter between the
two. The request drops the OpenRouter `model` field and keeps `state` and `questions`, which `/v1/systemone` takes as
they are. The answers come back in the same TypeSafe shape (`type`, `choice`, `probabilities`, `confidence`, `noul`),
except that `confidence` is on a different scale. No call leaves the machine.
"""

import contextlib
import http.client
import json
import os
import socket
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
# The Linux CUDA build of mistral.rs lives in WSL, since only its Linux release ships CUDA. serve-d1.sh reads PORT and
# PAGED_ATTN (docs/voice/local-decider.md).
MISTRALRS_DISTRO = os.environ.get("VOICE_MISTRALRS_DISTRO", "Ubuntu-24.04")
MISTRALRS_BIN = os.environ.get("VOICE_MISTRALRS_BIN", "/root/mistralrs-linux/mistralrs")
MISTRALRS_PORT = int(os.environ.get("VOICE_MISTRALRS_PORT", 8097))


def wsl_path(path):
    """The WSL path for a Windows path: `C:\\x\\y` is `/mnt/c/x/y`."""
    text = str(path).replace("\\", "/")
    return f"/mnt/{text[0].lower()}{text[2:]}"


MISTRALRS_SCRIPT = wsl_path(VOICE_HOME / "mistralrs-linux" / "serve-d1.sh")
# The card uses -ub 8192. Each question is read in one physical batch, and every recorded question prompt fits in
# 2048 tokens, which gave byte-identical answers at the same latency with about 970 MiB less VRAM
# (docs/voice/local-decider.md). 512 is too small: one question needs 521 tokens.
FLAGS = ("-b", "8192", "-ub", "2048", "-fa", "on")
TIMEOUT_S = 60
# The server runs one request at a time and finishes a request whose client has gone (measured: a turn's 3-question
# request took 2.4 s, not 87 ms, behind an aborted 32-question one). A lane sends this many questions per request and
# stops between requests when aborted, so the turns' next request waits behind at most one such piece.
LANE_QUESTIONS = 4


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
    """One persistent HTTP connection to a local llama-server or mistral.rs, one request in flight.

    `set_size` is mistral.rs's questions-per-state group and is omitted for llama.cpp, which has no such field. Packing
    happens inside one request, so a packed request is still one request in flight.
    """

    def __init__(self, url=None, set_size=None):
        parts = urlsplit(url or os.environ.get("VOICE_D1_URL", f"http://127.0.0.1:{DEFAULT_PORT}"))
        self.host, self.port = parts.hostname, parts.port
        self.set_size = set_size
        self._conn = None
        self.timeout_s, self.attempts, self.aborted, self.questions_per_request = TIMEOUT_S, 2, False, None

    def lane(self, timeout_s):
        """A second client on the same server for a caller that must never hold up this one's requests: its own
        connection, one attempt, `timeout_s` on each socket operation, and `LANE_QUESTIONS` questions per request."""
        lane = D1Client(f"http://{self.host}:{self.port}", self.set_size)
        lane.timeout_s, lane.attempts, lane.questions_per_request = timeout_s, 1, LANE_QUESTIONS
        return lane

    def _post(self, payload):
        for attempt in range(self.attempts):
            try:
                if self._conn is None:
                    conn = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout_s)
                    conn.connect()
                    self._conn = conn
                    if self.aborted:
                        raise ConnectionAbortedError("the client was aborted")
                self._conn.request("POST", "/v1/systemone", body=payload, headers={"Content-Type": "application/json"})
                response = self._conn.getresponse()
                return response.status, response.read()
            except (ConnectionError, http.client.HTTPException):
                self.close()
                if self.aborted or attempt + 1 == self.attempts:
                    raise

    def close(self):
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def abort(self):
        """Ends this client's request in flight from another thread, and any it would start. A shutdown wakes a read
        blocked on the socket on Linux, and only closing the handle does on Windows (measured: after a shutdown alone
        the read waited out its timeout), so it does both."""
        self.aborted = True
        conn = self._conn
        sock = conn.sock if conn is not None else None
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)
            with contextlib.suppress(OSError):
                socket.close(sock.detach())

    def call(self, route, body, label="", fresh=False):
        """Ask every question in `body` and return a record shaped like the cloud client's cache record. `fresh` is
        the cloud clients' cache bypass; this client keeps no cache, so every call is fresh."""
        assert route == "decisions", route
        started_at = _now()
        started = time.perf_counter()
        items = list(body["questions"].items())
        size = self.questions_per_request or max(len(items), 1)
        status, response = 200, None
        for first in range(0, max(len(items), 1), size):
            if self.aborted:
                raise ConnectionAbortedError("the client was aborted")
            status, part = self._ask({**body, "questions": dict(items[first:first + size])})
            if response is not None and status == 200 and isinstance(part.get("answers"), dict):
                part = {**part, "answers": {**response["answers"], **part["answers"]}}
            response = part
            if status != 200 or not isinstance(part.get("answers"), dict):
                break
        ms = (time.perf_counter() - started) * 1000
        return {"route": route, "label": label, "request": body, "status": status, "response": response,
                "timing": {"started_at": started_at, "ms": ms, "connect_ms": None}, "attempts": []}

    def _ask(self, body):
        request = to_systemone(body)
        if self.set_size is not None:
            request["set_size"] = self.set_size
        status, raw = self._post(json.dumps(request).encode())
        try:
            return status, from_systemone(json.loads(raw))
        except ValueError:
            return status, {"unparsed": raw[:2000].decode("utf-8", "replace")}


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


def _pgrep():
    """The PIDs of a running mistral.rs inside WSL, empty when none is left."""
    out = subprocess.run(["wsl", "-d", MISTRALRS_DISTRO, "--", "pgrep", "-f", MISTRALRS_BIN],
                         capture_output=True, text=True).stdout
    return out.split()


def stop_mistralrs(timeout=30):
    """Stop every mistral.rs in WSL and wait until pgrep sees none, since killing wsl.exe does not kill it."""
    subprocess.run(["wsl", "-d", MISTRALRS_DISTRO, "--", "pkill", "-f", MISTRALRS_BIN], capture_output=True)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _pgrep():
            return True
        time.sleep(0.5)
    return False


class MistralRsServer:
    """Starts mistral.rs on the WSL GPU through serve-d1.sh and stops that server on exit.

    `startup_s` is launch to first healthy reply. Paged attention is a server flag, so it belongs to the server and not
    to a request; `set_size` is per request and lives on D1Client.
    """

    def __init__(self, port=MISTRALRS_PORT, log_path=None, paged_attn="off"):
        self.port, self.paged_attn, self.log_path, self.proc = port, paged_attn, Path(log_path), None

    def __enter__(self):
        if _pgrep():
            raise RuntimeError(f"a mistral.rs server is already running, PIDs {_pgrep()}")
        command = ["wsl", "-d", MISTRALRS_DISTRO, "--", "env", f"PORT={self.port}",
                   f"PAGED_ATTN={self.paged_attn}", "bash", MISTRALRS_SCRIPT]
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log = open(self.log_path, "w")
        started = time.perf_counter()
        self.proc = subprocess.Popen(command, stdout=self.log, stderr=subprocess.STDOUT)
        try:
            self._wait_healthy()
        except BaseException:
            self.__exit__()
            raise
        self.startup_s = time.perf_counter() - started
        return self

    def _wait_healthy(self, timeout=300):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"mistral.rs exited with {self.proc.returncode}, see {self.log_path}")
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/health", timeout=2) as response:
                    if response.status == 200:
                        return
            except OSError:
                pass
            time.sleep(0.5)
        raise RuntimeError(f"mistral.rs did not become healthy, see {self.log_path}")

    def __exit__(self, *exc):
        if not stop_mistralrs():
            print(f"WARNING mistral.rs left running, PIDs {_pgrep()}", flush=True)
        try:
            self.proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=30)
        self.log.close()


@contextmanager
def decider(name, log_dir=None):
    """The decider a run asked for: `liquid` (Liquid AI's hosted d1.2, the default), `cloud` (OpenRouter's
    pplx-decider-flash, kept as the fallback), or `d1` (a local llama-server it starts, or whatever server
    `$VOICE_D1_URL` names, which is how mistral.rs gets measured)."""
    if name == "cloud":
        from voice.segmentation import client as api
        yield api.Client()
        return
    if name == "liquid":
        from voice.liquid_decider.client import LiquidClient
        yield LiquidClient()
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
    return os.environ.get("VOICE_DECIDER", "liquid")
