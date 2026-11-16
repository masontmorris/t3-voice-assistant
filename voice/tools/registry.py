"""The tool registry: from a read-only task's instruction to a real lookup, with a timeout and a clear outcome.

`Registry.route` picks the first tool whose parser accepts the instruction, or None when no tool fits. `Registry.run`
never raises. It returns a `ToolResult` whose outcome is ok, failed or timeout, and whose summary is the sentence
the voice may speak, a failure included.

    python -m voice.tools.registry "What time is it in Tokyo right now?"

routes and runs one instruction for real and prints the result.
"""

import json
import os
import shlex
import sys
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Callable

from voice.tools import lookups, open_meteo
from voice.tools.mcp import StdioClient

VOICE_HOME = Path(os.environ.get("VOICE_HOME", Path.home() / "voice"))
MCP_TIME = [str(VOICE_HOME / "venv-mcp-time" / "Scripts" / "mcp-server-time.exe")]
LABELS = {"weather": "weather lookup", "time": "time lookup"}


class Outcome(StrEnum):
    OK = "ok"
    FAILED = "failed"
    TIMEOUT = "timeout"


@dataclass(frozen=True)
class ToolCall:
    tool: str
    arguments: dict


@dataclass(frozen=True)
class ToolResult:
    tool: str
    arguments: dict
    outcome: Outcome
    summary: str
    latency_s: float
    detail: dict

    def record(self):
        return {**asdict(self), "outcome": self.outcome.value}


@dataclass(frozen=True)
class Tool:
    name: str
    parse: Callable[[str], dict | None]
    call: Callable[[dict, float], tuple[str, dict]]


def timed_out(exc):
    return isinstance(exc, TimeoutError) or isinstance(getattr(exc, "reason", None), TimeoutError)


class Registry:
    def __init__(self, tools, timeout_s=5.0, mcp=None):
        self.tools, self.timeout_s, self.mcp = {tool.name: tool for tool in tools}, timeout_s, mcp

    def route(self, instruction):
        for tool in self.tools.values():
            arguments = tool.parse(instruction)
            if arguments is not None:
                return ToolCall(tool.name, arguments)
        return None

    def run(self, call):
        label = LABELS.get(call.tool, f"{call.tool} lookup")
        started = time.perf_counter()
        try:
            summary, detail = self.tools[call.tool].call(call.arguments, time.monotonic() + self.timeout_s)
            outcome = Outcome.OK
        except lookups.LookupFailed as exc:
            outcome, summary, detail = Outcome.FAILED, f"The {label} {exc}.", {"error": repr(exc)}
        except Exception as exc:
            if timed_out(exc):
                outcome, summary = Outcome.TIMEOUT, f"The {label} timed out after {self.timeout_s:g} seconds."
            else:
                outcome, summary = Outcome.FAILED, f"The {label} failed, so there is no result."
            detail = {"error": repr(exc)}
        return ToolResult(call.tool, call.arguments, outcome, summary, time.perf_counter() - started, detail)


def build(fetch, mcp, default_place, timeout_s=5.0):
    weather = Tool("weather", *lookups.weather_tool(fetch, default_place))
    clock = Tool("time", *lookups.time_tool(fetch, mcp, default_place))
    return Registry([weather, clock], timeout_s, mcp)


def mcp_command():
    """VOICE_MCP_TIME, a command line, or the server in $VOICE_HOME/venv-mcp-time."""
    configured = os.environ.get("VOICE_MCP_TIME")
    return shlex.split(configured, posix=False) if configured else MCP_TIME


@contextmanager
def open_registry(timeout_s=5.0):
    """The live registry, with the MCP time server running for as long as the block runs.

    The default place is VOICE_DEFAULT_PLACE, used when a request names none.
    """
    with StdioClient(mcp_command()) as mcp:
        yield build(open_meteo.fetch_json, mcp, os.environ.get("VOICE_DEFAULT_PLACE"), timeout_s)


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    with open_registry() as registry:
        print(f"mcp server {registry.mcp.server_info} up in {registry.mcp.startup_s:.3f} s", file=sys.stderr)
        call = registry.route(sys.argv[1])
        print(json.dumps(registry.run(call).record() if call else None, indent=1, default=str))


if __name__ == "__main__":
    main()
