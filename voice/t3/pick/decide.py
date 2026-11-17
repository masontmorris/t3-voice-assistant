"""The deployed pick, in the order the seam will call it.

A back-reference resolves from the task record before anything else, then the deterministic prefilter
runs, and only a prefilter miss or tie reaches the model. The model never sees a thread the ceiling
hides, because it is handed the same listing the prefilter saw.
"""

from .model import pick_thread
from .pick import Pick, question_for
from .prefilter import prefilter


def prefilter_pick(utterance, threads):
	hits, words = prefilter(utterance, threads)
	titles = {t["threadId"]: t.get("title", "") for t in threads}
	if len(hits) == 1:
		return Pick.one(hits[0], "prefilter")
	if hits:
		return Pick.ask(hits, question_for([titles[i] for i in hits]), "prefilter tie")
	return Pick.none("no listed title contains " + " ".join(words))


def pick(utterance, threads, projects, now, client=None):
	if client is None:
		return prefilter_pick(utterance, threads), {"ms": 0, "model": False}
	first = prefilter_pick(utterance, threads)
	if first.kind == "one":
		return first, {"ms": 0, "model": False}
	shortlist = threads if first.kind == "none" else [t for t in threads if t["threadId"] in first.candidates]
	picked, meta = pick_thread(utterance, shortlist, projects, now, client)
	return picked, {**meta, "model": True, "shortlist": len(shortlist)}
