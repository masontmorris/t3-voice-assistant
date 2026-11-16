"""The tool registry: from a read-only task's instruction to a real lookup, with a timeout and a clear outcome.

`Registry.route` picks the first tool whose parser accepts the instruction. Given the earlier `ToolCall` a follow-up
continues, that tool also accepts a request with no subject word that states a value of its own, and fills what the
request leaves out from the earlier call. With the general lookup worker configured (`voice/tools/agent.py`), a
request no parser claims goes to it, and so does a request that names a subject but states no place and no day of its
own while an earlier lookup exists, since the parsers would only guess the place. Without the worker such a request
keeps the parsers' answer, or None. `Registry.run` never raises. It returns a `ToolResult` whose outcome is ok, failed
or timeout, and whose summary is the sentence the voice may speak, a failure included.

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

from voice.tools import agent, lookups, open_meteo
from voice.tools.mcp import StdioClient

VOICE_HOME = Path(os.environ.get("VOICE_HOME", Path.home() / "voice"))
MCP_TIME = [str(VOICE_HOME / "venv-mcp-time" / "Scripts" / "mcp-server-time.exe")]
LABELS = {"weather": "weather lookup", "time": "time lookup", "agent": "lookup"}


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
    parse: Callable[[str, dict | None], dict | None]
    call: Callable[[dict, float], tuple[str, dict]]


def timed_out(exc):
    return isinstance(exc, TimeoutError) or isinstance(getattr(exc, "reason", None), TimeoutError)


class Registry:
    def __init__(self, tools, timeout_s=5.0, mcp=None, agent=None):
        self.tools, self.timeout_s, self.mcp = {tool.name: tool for tool in tools}, timeout_s, mcp
        self.agent = agent

    def route(self, instruction, earlier=None, context=None):
        """A tool whose subject the instruction names wins, so "what time is it in Tokyo" after a forecast is a time
        lookup. Only an instruction no tool claims continues `earlier`'s tool. `context` is the call's recent turns,
        for the general worker."""
        for tool in self.tools.values():
            if tool.parse(instruction) is not None:
                same = earlier is not None and earlier.tool == tool.name
                own = lookups.find_place(instruction) or (same and lookups.stated_when(instruction))
                if self.agent is not None and earlier is not None and not own:
                    break
                return ToolCall(tool.name, tool.parse(instruction, earlier.arguments if same else None))
        else:
            if earlier is not None and earlier.tool in self.tools:
                arguments = self.tools[earlier.tool].parse(instruction, earlier.arguments)
                if arguments is not None:
                    return ToolCall(earlier.tool, arguments)
        return self.ask(instruction, context)

    def ask(self, instruction, context=None):
        """A call to the general worker, or None when none is configured."""
        if self.agent is None:
            return None
        return ToolCall("agent", {"instruction": instruction, "context": context or []})

    def timeout_for(self, call):
        return self.agent.timeout_s if call.tool == "agent" and self.agent is not None else self.timeout_s

    def run(self, call):
        label = LABELS.get(call.tool, f"{call.tool} lookup")
        timeout_s = self.timeout_for(call)
        started = time.perf_counter()
        try:
            run = self.agent.call if call.tool == "agent" else self.tools[call.tool].call
            summary, detail = run(call.arguments, time.monotonic() + timeout_s)
            outcome = Outcome.OK
        except agent.Failed as exc:
            outcome, summary, detail = Outcome.FAILED, exc.summary, exc.detail
        except lookups.LookupFailed as exc:
            outcome, summary, detail = Outcome.FAILED, f"The {label} {exc}.", {"error": repr(exc)}
        except Exception as exc:
            if timed_out(exc):
                outcome, summary = Outcome.TIMEOUT, f"The {label} timed out after {timeout_s:g} seconds."
            else:
                outcome, summary = Outcome.FAILED, f"The {label} failed, so there is no result."
            detail = {"error": repr(exc)}
        return ToolResult(call.tool, call.arguments, outcome, summary, time.perf_counter() - started, detail)


def build(fetch, mcp, default_place, timeout_s=5.0, chat=None, agent_timeout_s=agent.TIMEOUT_S):
    """`chat` is the general worker's model, as `agent.Agent` takes it. Without one there is no worker."""
    weather = Tool("weather", *lookups.weather_tool(fetch, default_place))
    clock = Tool("time", *lookups.time_tool(fetch, mcp, default_place))
    registry = Registry([weather, clock], timeout_s, mcp)
    if chat is not None:
        registry.agent = agent.Agent(chat, lambda tool, arguments: registry.run(ToolCall(tool, arguments)),
                                     default_place, agent_timeout_s)
    return registry


def mcp_command():
    """VOICE_MCP_TIME, a command line, or the server in $VOICE_HOME/venv-mcp-time."""
    configured = os.environ.get("VOICE_MCP_TIME")
    return shlex.split(configured, posix=False) if configured else MCP_TIME


@contextmanager
def open_registry(timeout_s=5.0, lock=None):
    """The live registry, with the MCP time server running for as long as the block runs.

    The default place is VOICE_DEFAULT_PLACE, used when a request names none. The general worker is on when
    VOICE_AGENT is "on" and an OpenRouter key is configured; its model is VOICE_AGENT_MODEL or `agent.MODEL`. It is
    off by default because no model has been measured for it yet: its one live call drew a 429 that tripped the
    billing stop. `lock` keeps its requests from overlapping the decider's.
    """
    key = agent.read_key() if os.environ.get("VOICE_AGENT", "").lower() == "on" else None
    chat = agent.OpenRouterChat(key, lock=lock) if key else None
    with StdioClient(mcp_command()) as mcp:
        yield build(open_meteo.fetch_json, mcp, os.environ.get("VOICE_DEFAULT_PLACE"), timeout_s, chat)


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    with open_registry() as registry:
        print(f"mcp server {registry.mcp.server_info} up in {registry.mcp.startup_s:.3f} s", file=sys.stderr)
        call = registry.route(sys.argv[1])
        print(json.dumps(registry.run(call).record() if call else None, indent=1, default=str))


if __name__ == "__main__":
    main()
