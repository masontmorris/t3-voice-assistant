"""The deployed pick, in the order the seam will call it.

The deterministic prefilter runs first. A hit is the pick and no model is called. A tie asks
by default. Only a miss, or a tie under the arm that lets a model break one, reaches the model,
which sees the same listing the ceiling allows. A model that fails or cannot be reached becomes
a question, never a guess.
"""

from urllib.error import URLError

from .model import ModelFailed, pick_project_via_model, pick_thread
from .pick import Pick, question_for
from .prefilter import prefilter
from .project import ProjectPick, pick_project, question_for_projects

FAILURES = (ModelFailed, URLError, OSError, TimeoutError)


def _titles(threads):
	return {t["threadId"]: t.get("title", "") for t in threads}


def prefilter_pick(utterance, threads, projects=None):
	listed = [t["threadId"] for t in threads]
	match = prefilter(utterance, threads, projects)
	titles = _titles(threads)
	hits = match.candidates
	if len(hits) == 1:
		return Pick.one(hits[0], listed, "prefilter")
	if hits:
		reason = "project name" if match.by_project else "prefilter tie"
		return Pick.ask(hits, question_for([titles[i] for i in hits]), reason)
	if match.state_only:
		return Pick.ask([], question_for([]), "state word with no matching activity")
	return Pick.none("no listed title contains " + " ".join(match.words))


def decide_project(utterance, projects, client=None):
	"""Return (ProjectPick, meta). A deterministic hit wins; anything else reaches the model."""
	first = pick_project(utterance, projects)
	if first.kind == "one" or client is None:
		return first, {"ms": 0, "model": False, "parse": "ok"}
	names = {p["projectId"]: p.get("name", "") for p in projects}
	try:
		picked, meta = pick_project_via_model(utterance, projects, client, list(first.candidates))
		return picked, {**meta, "model": True, "shortlist": len(projects)}
	except FAILURES as e:
		candidates = list(first.candidates)
		return (ProjectPick.ask(candidates, question_for_projects([names[i] for i in candidates]),
		                        f"picker failed: {type(e).__name__}"),
		        {"ms": 0, "model": True, "parse": "failed", "error": str(e)[:200],
		         "shortlist": len(projects)})


def pick(utterance, threads, projects, now, client=None, break_tie=False):
	"""Return (Pick, meta). Meta carries `model`, `parse`, `shortlist`, latency and tokens."""
	first = prefilter_pick(utterance, threads, projects)
	if first.kind == "one" or client is None:
		return first, {"ms": 0, "model": False, "parse": "ok"}
	if first.kind == "ask":
		if not break_tie or len(first.candidates) < 2:
			return first, {"ms": 0, "model": False, "parse": "ok"}
		shortlist = [t for t in threads if t["threadId"] in first.candidates]
	else:
		shortlist = threads
	titles = _titles(shortlist)
	try:
		picked, meta = pick_thread(utterance, shortlist, projects, now, client)
	except FAILURES as e:
		candidates = [t["threadId"] for t in shortlist[:3]]
		return (Pick.ask(candidates, question_for([titles[i] for i in candidates]),
		                 f"picker failed: {type(e).__name__}"),
		        {"ms": 0, "model": True, "parse": "failed", "error": str(e)[:200],
		         "shortlist": len(shortlist)})
	return picked, {**meta, "model": True, "shortlist": len(shortlist)}
