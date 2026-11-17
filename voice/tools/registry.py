"""The tool registry: from a read-only task's instruction to a real lookup, with a timeout and a clear outcome.

Each tool declares two lines: `label`, the short name a failure sentence speaks, and `line`, what it does and when to
use it. `Registry.choices` is that table plus the general worker and `none`, and it is the option set of the decider's
tool question (`voice/segmentation/contract.py`), so a tool added here appears there with no other edit.

`Registry.dispatch` runs the tool the decider picked when that answer clears `TOOL_THRESHOLD`, and otherwise falls
back to the word rules. `Registry.corrected` is the call a correction of an earlier task runs: that task's own tool
and arguments, with what the amended words change. `Registry.route` is the word rules on their own: the first tool
whose parser accepts the instruction. Given the earlier `ToolCall` a follow-up continues, that tool also accepts a
request with no subject word that states a value of its own, and fills what the request leaves out from the earlier
call. With the general lookup worker configured (`voice/tools/agent.py`), a request no parser claims goes to it, and
so does a request that names a subject but states no place and no day of its own while an earlier lookup exists, since
the parsers would only guess the place. Without the worker such a request keeps the parsers' answer, or None.
`Registry.waiting` says what a call would have to ask the person first, because a task whose lookup cannot answer
waits on that question instead of running and failing, and `Registry.answered` is the call that person's answer
completes. `Registry.run` never raises. It returns a `ToolResult` whose outcome is ok, failed or timeout, and whose
summary is the sentence the voice may speak, a failure included.

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
NONE, GENERAL = "none", "agent"
# The two options that are not a registered tool. `none` starts nothing; `agent` is the general worker.
NONE_LINE = "No tool: conversation, a reaction, or a question the assistant answers from this call's own words."
GENERAL_LINE = ("General lookup: information from outside this call that no tool above covers, including a request "
                "that asks the assistant to choose a detail first, such as a city.")
# The decider's tool answer decides the dispatch only at or above this margin. Frozen on the live gate eval's
# training items (`python -m voice.research.segmentation.live_gate_eval --tools --tool-sweep`), on two independent samples:
# each is flat from 0.00 to 0.45, so 0.30 sits inside both with room either side, and above 0.50 the fall back to the
# word rules costs accuracy in both. Train 91% at 0.30 in both samples, held-out 88% and 92%; the word rules alone are
# 70% train and 62% held-out.
TOOL_THRESHOLD = 0.30


class Outcome(StrEnum):
    OK = "ok"
    FAILED = "failed"
    TIMEOUT = "timeout"


@dataclass(frozen=True)
class ToolCall:
    tool: str
    arguments: dict


@dataclass(frozen=True)
class ToolChoice:
    """The decider's tool answer for one segment: a registry name or `none`, the margin it answered at, and the
    arguments it read (`place`, `when`), when the request asked it to read them."""
    tool: str
    confidence: float
    arguments: dict | None = None


@dataclass(frozen=True)
class Routed:
    """One routing: the call to run, or None, and how the registry got there.

    `via` is `decider` when the tool answer cleared the bar, `correction` when the call re-runs the task a correction
    amended, or `rules` when the answer was absent or under the bar. A `rules` routing that a choice under the bar
    disagreed with is a logged disagreement.
    """
    call: ToolCall | None
    via: str
    choice: ToolChoice | None = None

    def record(self):
        tool = self.call.tool if self.call is not None else None
        disagreement = self.via == "rules" and self.choice is not None and self.choice.tool != (tool or NONE)
        return {"tool": tool, "via": self.via, "disagreement": disagreement,
                "choice": None if self.choice is None else
                {"tool": self.choice.tool, "confidence": round(self.choice.confidence, 4),
                 "arguments": self.choice.arguments}}


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
    """`label` is the short name a failure sentence speaks; `line` is what the decider's tool question offers;
    `needs` are the arguments a call cannot answer without (`Registry.waiting`)."""
    name: str
    label: str
    line: str
    parse: Callable[[str, dict | None], dict | None]
    call: Callable[[dict, float], tuple[str, dict]]
    needs: tuple[str, ...] = ()


def timed_out(exc):
    return isinstance(exc, TimeoutError) or isinstance(getattr(exc, "reason", None), TimeoutError)


class Registry:
    def __init__(self, tools, timeout_s=5.0, mcp=None, agent=None, tool_threshold=TOOL_THRESHOLD, default_place=None):
        self.tools, self.timeout_s, self.mcp = {tool.name: tool for tool in tools}, timeout_s, mcp
        self.agent, self.tool_threshold, self.default_place = agent, tool_threshold, default_place

    def choices(self):
        """The tool question's options: every registered tool with its line, the general worker, and `none`."""
        return {tool.name: tool.line for tool in self.tools.values()} | {GENERAL: GENERAL_LINE, NONE: NONE_LINE}

    def dispatch(self, instruction, earlier=None, context=None, choice=None, corrected=None):
        """The tool that carries out a task: the call a correction of an earlier task runs, else the decider's choice
        when it clears `tool_threshold`, else the word rules.

        `corrected` is `Registry.corrected`'s call, which wins because a correction is about that task and the words
        of an amended instruction are no evidence of which tool it needs. A choice naming `none` runs nothing, and so
        does `agent` when no general worker is configured: a confident choice is not overruled by the rules, since the
        rules are what misrouted the requests this replaced. Only a choice under the bar falls back to them.
        """
        if corrected is not None:
            return Routed(corrected, "correction", choice)
        if choice is not None and choice.confidence >= self.tool_threshold:
            if choice.tool == NONE:
                return Routed(None, "decider", choice)
            call = self.ask(instruction, context) if choice.tool == GENERAL else self.named(choice.tool, instruction,
                                                                                          earlier, choice.arguments)
            return Routed(call, "decider", choice)
        return Routed(self.route(instruction, earlier, context), "rules", choice)

    def corrected(self, earlier, instruction):
        """The call a correction of `earlier` runs: that task's own tool, with the argument this correction changes
        read from the amended `instruction` and the rest kept.

        A tool the registry does not hold is the general worker's, which is the only route that can act on the amended
        words; with no worker configured the call still names it, so a caller reads the same answer, and `run` fails
        it rather than raising.
        """
        if earlier is None:
            return None
        if earlier.tool not in self.tools:
            return ToolCall(GENERAL, {"instruction": instruction, "context": []})
        return ToolCall(earlier.tool, self.filled(earlier.tool, None, instruction, earlier))

    def named(self, name, instruction, earlier, reading=None):
        """A call to the tool `name`, with the arguments `filled` takes from the decider's reading and the words."""
        if name not in self.tools:
            return None
        return ToolCall(name, self.filled(name, reading, instruction, earlier))

    def continues(self, name, instruction, earlier):
        """Whether `instruction` continues an earlier call of `name`: the tool's own parser takes it with that call's
        arguments, which needs a subject word of its own or a value of its own such as a place or a day. "Wait, do it
        again." states neither, so it continues nothing, and "And what about Saturday?" states a day, so it does."""
        return (name in self.tools and earlier is not None and earlier.tool == name
                and self.tools[name].parse(instruction, earlier.arguments) is not None)

    def filled(self, name, reading, instruction, earlier):
        """The arguments one tool takes, in this order: what the decider read of this segment's own words, what the
        instruction states, the earlier lookup's own when this is a follow-up of it, and the default place.

        The reading is kept only where the words say it (`lookups.supported`), so a place or day the model inferred
        from the call cannot overrule an elliptical follow-up's own day or repeat an earlier forecast nobody asked
        for."""
        reading = lookups.supported(reading, instruction)
        prior = earlier.arguments if self.continues(name, instruction, earlier) else {}
        place = reading.get("place") or lookups.find_place(instruction) or prior.get("place") or self.default_place
        if name != "weather":
            return {"place": place}
        when = (reading.get("when") or lookups.resolved(lookups.stated_when(instruction), prior.get("when"))
                or prior.get("when") or "today")
        return {"place": place, "when": when}

    def waiting(self, name, instruction, earlier=None, reading=None):
        """What a call to `name` would have to ask the person first: its tool, the arguments it would run with, and the
        ones it lacks. None when it lacks nothing.

        A task whose call lacks an argument does not run: it waits for the person's answer
        (`voice/segmentation/tasks.py`, `NEEDS_CLARIFICATION`), and `answered` is the call that answer completes.
        Measured: turn 016's "Wait, do it again." picked the weather tool with nothing to look up and spoke a failure,
        "needs a place"."""
        if name not in self.tools:
            return None
        arguments = self.filled(name, reading, instruction, earlier)
        missing = tuple(field for field in self.tools[name].needs if not arguments.get(field))
        return {"tool": name, "arguments": arguments, "missing": missing} if missing else None

    def answered(self, waiting, instruction):
        """The call a person's answer completes: the waiting tool with the arguments the answer states, or None when
        these words are not that answer.

        `waiting` is what this registry's `waiting` gave for the task. The words must not name another tool's subject
        ("What's the time in Tokyo?" answers nothing a forecast asked) and must state a value for every kind the task
        lacked."""
        name = waiting.get("tool")
        if name not in self.tools:
            return None
        if any(other.name != name and other.parse(instruction) is not None for other in self.tools.values()):
            return None
        arguments = dict(waiting.get("arguments") or {})
        for field in waiting.get("missing") or ():
            value = lookups.answer(field, instruction)
            if value is None:
                return None
            arguments[field] = value
        return ToolCall(name, arguments)

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
        label = self.tools[call.tool].label if call.tool in self.tools else "lookup"
        timeout_s = self.timeout_for(call)
        started = time.perf_counter()
        if call.tool == GENERAL and self.agent is None:
            return ToolResult(call.tool, call.arguments, Outcome.FAILED, "The lookup failed, so there is no result.",
                              0.0, {"error": "the general worker is not configured"})
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


def build(fetch, mcp, default_place, timeout_s=5.0, chat=None, agent_timeout_s=agent.TIMEOUT_S,
          tool_threshold=TOOL_THRESHOLD):
    """`chat` is the general worker's model, as `agent.Agent` takes it. Without one there is no worker."""
    weather = Tool("weather", "weather lookup",
                   "Weather forecast for a named city on a named day: the high temperature and the chance of "
                   "precipitation. Use it for a forecast, or for anything that depends on one.",
                   *lookups.weather_tool(fetch, default_place), needs=("place",))
    clock = Tool("time", "time lookup",
                 "The current local time in a named city. Use it when the speaker asks what time it is.",
                 *lookups.time_tool(fetch, mcp, default_place), needs=("place",))
    registry = Registry([weather, clock], timeout_s, mcp, tool_threshold=tool_threshold, default_place=default_place)
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
    an OpenRouter key is configured, unless VOICE_AGENT is "off". Its model is VOICE_AGENT_MODEL or `agent.MODEL`, a
    cheap paid model capped at `agent.DAILY_CAP_USD` a day. `lock` keeps its requests from overlapping the decider's.
    """
    key = agent.read_key() if os.environ.get("VOICE_AGENT", "on").lower() != "off" else None
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
