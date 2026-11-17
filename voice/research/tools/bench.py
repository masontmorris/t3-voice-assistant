"""Time the live lookups on their own, with no voice model.

    python -m voice.research.tools.bench [-n 10]

Routes and runs each instruction in INSTRUCTIONS n times, one at a time, against Open-Meteo and the running
`mcp-server-time`. Prints the median, min and max latency per tool, the MCP call's share of each time lookup, and
the MCP server's startup time.
"""

import argparse
import json
import statistics

from voice.tools.registry import open_registry

INSTRUCTIONS = ["What's the weather in Denver tomorrow?", "What's the weather going to be in Denver this weekend",
                "and what time is it in Tokyo right now?"]


def spread(values):
    return {"n": len(values), "median_s": statistics.median(values), "min_s": min(values), "max_s": max(values)}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-n", type=int, default=10)
    args = parser.parse_args()
    by_instruction, mcp_s, outcomes = {}, [], []
    with open_registry() as registry:
        for _ in range(args.n):
            for instruction in INSTRUCTIONS:
                result = registry.run(registry.route(instruction))
                outcomes.append(result.outcome.value)
                by_instruction.setdefault(instruction, []).append(result.latency_s)
                if "mcp_latency_s" in result.detail:
                    mcp_s.append(result.detail["mcp_latency_s"])
        startup = registry.mcp.startup_s
    print(json.dumps({"mcp_startup_s": startup, "outcomes": {o: outcomes.count(o) for o in set(outcomes)},
                      "lookups": {i: spread(v) for i, v in by_instruction.items()}, "mcp_call": spread(mcp_s)},
                     indent=1))


if __name__ == "__main__":
    main()
