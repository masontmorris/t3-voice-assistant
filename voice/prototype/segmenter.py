"""The segmenters behind the prototype. The default is the hybrid (`voice/segmentation/hybrid.py`, plan A).

`BaselineSegmenter` is round 2's option (a) with one bundled decisions call, kept as the comparison. Both keep the
uniform side-effect hold: a side-effect task dispatches only on the final transcript.
"""

import json
from pathlib import Path
from typing import Protocol

from voice.segmentation import client as api
from voice.segmentation.contract import TranscriptUpdate
from voice.segmentation.hybrid import HybridSegmenter
from voice.segmentation.run import Runner
from voice.segmentation.spans import clause_segments
from voice.segmentation.tasks import Orchestrator

# The prototype's ledger held 80 attempts when the end-of-utterance handoff round began, and that round used 97. The
# task gate round adds its own 150.
DECIDER_BUDGET = 177 + 150
MAX_429_SHARE = 0.2


class Segmenter(Protocol):
    dispatch_log: list[dict]
    orch: Orchestrator

    def update(self, update: TranscriptUpdate) -> dict | None: ...

    def submit_delegate(self, instruction: str) -> dict | None: ...

    def records(self) -> list[dict]: ...


def use_decider_cache(directory: Path, budget: int):
    """Keep this run's decider cache and call ledger out of the repo, and cap its live calls at `budget`.

    The cache keys hold transcripts, and Mason's recordings' transcripts must stay local.
    """
    directory.mkdir(parents=True, exist_ok=True)
    api.CACHE, api.LEDGER, api.BUDGET = directory, directory / "calls.jsonl", budget


def coalesce(items):
    """Keep only the newest of each run of queued transcript updates. Delegates keep their place.

    Each update carries the whole utterance, so an older one queued behind a newer one adds nothing.
    """
    kept, dropped = [], []
    for i, (kind, payload) in enumerate(items):
        if kind == "update" and i + 1 < len(items) and items[i + 1][0] == "update":
            dropped.append(payload)
        else:
            kept.append((kind, payload))
    return kept, dropped


def live_calls():
    return api.ledger_attempts()


def rate_limited_share(since):
    """(attempts, share that drew a 429) among the ledger's attempts after the first `since`."""
    if not api.LEDGER.exists():
        return 0, 0.0
    entries = [json.loads(line) for line in api.LEDGER.read_text(encoding="utf-8").splitlines()]
    recent = [e for e in entries if e["route"] in api.ROUTES][since:]
    return len(recent), (sum(e["status"] == 429 for e in recent) / len(recent) if recent else 0.0)


class FreshClient:
    """Sends every request live, even when cached, so a timing run pays each call's real latency."""

    def __init__(self, client):
        self.client = client

    def call(self, route, body, label=""):
        return self.client.call(route, body, label=label, fresh=True)


class BaselineSegmenter:
    """Round 2's call policy: decide when the segment count or a closed segment changes, and on the final text."""

    def __init__(self, client, label):
        self.runner, self.orch, self.label = Runner("a", client), Orchestrator(), label
        self.last, self.signature = None, None

    @property
    def dispatch_log(self):
        return self.orch.dispatch_log

    def update(self, update):
        self.last = update
        cheap = clause_segments(update.text, update.is_final)
        signature = (len(cheap), tuple(s.text for s in cheap if not s.open))
        changed = update.is_final or signature != self.signature or self.orch.pending_delegates
        self.signature = signature
        return self.runner.decide(self.orch, update, f"{self.label}:r{update.revision}") if changed else None

    def submit_delegate(self, instruction):
        self.orch.submit_delegate(instruction)
        if self.last is None:
            return None
        return self.runner.decide(self.orch, self.last, f"{self.label}:r{self.last.revision}:delegate")

    def records(self):
        return self.orch.task_records()


SEGMENTERS = {"hybrid": HybridSegmenter, "baseline": BaselineSegmenter}
