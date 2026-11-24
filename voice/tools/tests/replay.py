"""Replay the recorded tool responses in fixtures/recorded.json. An unrecorded request raises."""

import json
from datetime import date
from pathlib import Path

from voice.tools import registry
from voice.tools.record import mcp_key

RECORDED = json.loads((Path(__file__).parents[1] / "fixtures" / "recorded.json").read_text(encoding="utf-8"))
# The recorded forecasts start on the day they were recorded, which is the caller's today in every replay.
RECORDED_TODAY = date(2026, 11, 16)


class ReplayFetch:
    def __init__(self):
        self.urls = []

    def __call__(self, url, timeout_s):
        self.urls.append(url)
        if url not in RECORDED["http"]:
            raise AssertionError(f"unrecorded request {url}")
        return RECORDED["http"][url]


class ReplayMcp:
    def __init__(self):
        self.calls = []

    def call_tool(self, name, arguments, timeout_s):
        self.calls.append((name, arguments))
        return RECORDED["mcp"][mcp_key(name, arguments)]


def replay_registry(default_place="Denver", timeout_s=5.0, chat=None, today=RECORDED_TODAY):
    fetch, mcp = ReplayFetch(), ReplayMcp()
    return registry.build(fetch, mcp, default_place, timeout_s, chat, today=lambda: today), fetch, mcp
