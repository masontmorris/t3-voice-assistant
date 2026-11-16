"""A minimal MCP client over stdio: newline-delimited JSON-RPC 2.0 to one server process.

It does the initialize handshake, then `tools/call`. Calls run one at a time. A response that arrives after its
call timed out is matched by id and dropped. It does not answer server-to-client requests, which the time server
never sends.
"""

import json
import queue
import subprocess
import threading
import time

PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "t3-voice-assistant", "version": "0"}


class McpError(Exception):
    pass


class StdioClient:
    def __init__(self, command, timeout_s=15.0):
        started = time.perf_counter()
        self.proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL)
        self._lines = queue.Queue()
        self._lock = threading.Lock()
        self._next_id = 0
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()
        try:
            init = self.request("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                                               "clientInfo": CLIENT_INFO}, timeout_s)
        except BaseException:
            self.close()
            raise
        self.server_info, self.protocol_version = init.get("serverInfo"), init.get("protocolVersion")
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.startup_s = time.perf_counter() - started

    def _read(self):
        for line in self.proc.stdout:
            self._lines.put(line)
        self._lines.put(None)

    def _send(self, message):
        self.proc.stdin.write(json.dumps(message).encode("utf-8") + b"\n")
        self.proc.stdin.flush()

    def request(self, method, params, timeout_s):
        deadline = time.monotonic() + timeout_s
        with self._lock:
            self._next_id += 1
            request_id = self._next_id
            try:
                self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
            except OSError as exc:
                raise McpError(f"MCP server exited with code {self.proc.poll()}") from exc
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"no MCP response to {method} in {timeout_s} s")
                try:
                    line = self._lines.get(timeout=remaining)
                except queue.Empty:
                    continue
                if line is None:
                    raise McpError(f"MCP server exited with code {self.proc.poll()}")
                message = json.loads(line)
                if message.get("id") != request_id or "method" in message:
                    continue
                if "error" in message:
                    raise McpError(message["error"].get("message", "MCP error"))
                return message["result"]

    def call_tool(self, name, arguments, timeout_s):
        """The tool's structured result. A tool-level error (`isError`) raises McpError with the server's text."""
        result = self.request("tools/call", {"name": name, "arguments": arguments}, timeout_s)
        text = "".join(part.get("text", "") for part in result.get("content", []) if part.get("type") == "text")
        if result.get("isError"):
            raise McpError(text or f"{name} failed")
        return result.get("structuredContent") or json.loads(text)

    def close(self):
        if self.proc.poll() is None:
            self.proc.stdin.close()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self._reader.join(timeout=5)
        self.proc.stdout.close()
        if not self.proc.stdin.closed:
            self.proc.stdin.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
