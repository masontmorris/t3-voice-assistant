"""Record the real tool responses the offline tests replay.

    python -m voice.tools.record

For each place in PLACES it fetches the geocoding and forecast responses from Open-Meteo, and for each time zone it
asks the running `mcp-server-time` for the current time. It writes them to fixtures/recorded.json, keyed by the
exact URL or by tool and arguments, so a test that sends any other request fails.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

from voice.tools import open_meteo
from voice.tools.mcp import StdioClient
from voice.tools.registry import mcp_command

FIXTURE = Path(__file__).parent / "fixtures" / "recorded.json"
PLACES = ["Denver", "Tokyo", "Atlantis Nowhere"]


def mcp_key(name, arguments):
    return name + " " + json.dumps(arguments, sort_keys=True)


def main():
    http, mcp_results = {}, {}
    for name in PLACES:
        url = open_meteo.geocode_url(name)
        http[url] = open_meteo.fetch_json(url, 10)
        place = open_meteo.parse_place(http[url])
        if place is None:
            continue
        forecast = open_meteo.forecast_url(place)
        http[forecast] = open_meteo.fetch_json(forecast, 10)
        with StdioClient(mcp_command()) as mcp:
            arguments = {"timezone": place.timezone}
            mcp_results[mcp_key("get_current_time", arguments)] = mcp.call_tool("get_current_time", arguments, 10)
    FIXTURE.parent.mkdir(exist_ok=True)
    recorded = {"recorded_at": datetime.now(UTC).isoformat(timespec="seconds"), "http": http, "mcp": mcp_results}
    FIXTURE.write_text(json.dumps(recorded, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {len(http)} HTTP and {len(mcp_results)} MCP responses to {FIXTURE.name}")


if __name__ == "__main__":
    main()
