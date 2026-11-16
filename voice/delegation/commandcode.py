"""The Command Code transport: DeepSeek V4.1 Flash through Command Code's OpenAI-compatible API.

By default the request carries no reasoning parameter, so the model runs on its default reasoning. A transport
made with `reasoning_effort` adds that one key to the body. The key file's path comes
from VOICE_AUTHOR_KEY_FILE. The file is read at each send and the key goes only into the Authorization header.
"""

import http.client
import json
import os
import socket
import time
from pathlib import Path

from voice.delegation.author import AuthorReply
from voice.delegation.draft import DRAFT_NAME

HOST = "api.commandcode.ai"
PATH = "/provider/v1/chat/completions"
MODEL = "deepseek/deepseek-v4.1-flash"
TIMEOUT_S = 60


class KeyMissing(RuntimeError):
    pass


def _key():
    name = os.environ.get("VOICE_AUTHOR_KEY_FILE")
    path = Path(name) if name else None
    if path is None or not path.is_file():
        raise KeyMissing("the file named by VOICE_AUTHOR_KEY_FILE is missing")
    key = path.read_text(encoding="utf-8-sig").strip()
    if not key:
        raise KeyMissing("the key file is empty")
    return key


def body_for(request, model=MODEL, reasoning_effort=None):
    """The JSON body sent to Command Code. It is recorded in the cache as sent, and it never holds the key."""
    effort = {} if reasoning_effort is None else {"reasoning_effort": reasoning_effort}
    return {**effort, "model": model, "max_tokens": request.max_tokens, "temperature": request.temperature,
            "messages": [{"role": "system", "content": request.system}, {"role": "user", "content": request.user}],
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": DRAFT_NAME, "strict": True, "schema": request.schema}}}


def _choice(body):
    choices = body.get("choices") if isinstance(body, dict) else None
    return choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}


def _content(body):
    text = (_choice(body).get("message") or {}).get("content")
    try:
        return json.loads(text) if isinstance(text, str) else None
    except ValueError:
        return None


class CommandCodeTransport:
    def __init__(self, model=MODEL, reasoning_effort=None):
        self.model, self.reasoning_effort = model, reasoning_effort
        self.setting = None if reasoning_effort is None else f"reasoning_effort={reasoning_effort}"
        self._conn = None

    def _connection(self):
        if self._conn is None:
            self._conn = http.client.HTTPSConnection(HOST, timeout=TIMEOUT_S)
        return self._conn

    def send(self, request):
        sent = body_for(request, self.model, self.reasoning_effort)
        headers = {"Authorization": f"Bearer {_key()}", "Content-Type": "application/json"}
        started = time.perf_counter()
        try:
            conn = self._connection()
            conn.request("POST", PATH, body=json.dumps(sent).encode(), headers=headers)
            response = conn.getresponse()
            raw = response.read()
        except (TimeoutError, socket.timeout, ConnectionError, http.client.HTTPException, OSError) as exc:
            if self._conn is not None:
                self._conn.close()
            self._conn = None
            return AuthorReply(None, None, (time.perf_counter() - started) * 1000, error=type(exc).__name__,
                               sent=sent)
        ms = (time.perf_counter() - started) * 1000
        try:
            body = json.loads(raw)
        except ValueError:
            body = {"unparsed": raw[:2000].decode("utf-8", "replace")}
        retry = response.getheader("retry-after") or ""
        ok = response.status == 200
        return AuthorReply(response.status, _content(body) if ok else None, ms,
                           error=None if ok else json.dumps(body.get("error", body))[:500],
                           retry_after_s=float(retry) if retry.replace(".", "", 1).isdigit() else None,
                           sent=sent, served_model=body.get("model"), usage=body.get("usage"),
                           finish_reason=_choice(body).get("finish_reason"),
                           meta={k: v for k, v in body.items() if k not in ("choices", "usage")} if ok else None)
