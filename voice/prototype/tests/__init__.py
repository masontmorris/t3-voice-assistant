"""Shared support for the prototype tests."""

from pathlib import Path

from voice.prototype.segmenter import use_decider_cache
from voice.segmentation import client as api


def isolate_decider_cache(directory: Path, budget: int):
    """Point the decider cache at a temp dir for one test, and return the cleanup that restores the repo cache.

    use_decider_cache sets module globals, so a test that leaves it in place sends every later test, the replay
    test included, to a directory that no longer exists.
    """
    repo_settings = api.CACHE, api.LEDGER, api.BUDGET
    use_decider_cache(directory, budget)

    def restore() -> None:
        api.CACHE, api.LEDGER, api.BUDGET = repo_settings

    return restore
