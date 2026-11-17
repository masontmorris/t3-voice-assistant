"""Deterministic project pick for Twin A. It matches `spokenNames` only and asks whenever the
match is not a lone target or names more than one project.

An exact match is one spoken name appearing as a contiguous word run in the request. A partial
match shares a content word with a spoken name, singularized both sides, and never picks. A
match counts toward a pick only from the target seat: after "in", "for" or "project", after
"in the" or "for the", or right before "project". A weak spoken name, one token or all
stopwords, counts only from that seat and is invisible elsewhere. A lone exact match outside
the seat never picks, and neither does a strict prefix of another project's longer spoken
name. With no overlap at all, an explicit "project X" the list does not carry is none; a
request with no project wording asks which project. The tokenizer and stopwords are Twin B's,
imported, not forked.
"""

from dataclasses import dataclass, field
from typing import Literal

from .pick import Pick
from .prefilter import TOKEN, _bare, _singular, content_words


@dataclass(frozen=True)
class ProjectPick:
	kind: Literal["one", "ask", "none"]
	project_id: str | None = None
	candidates: tuple[str, ...] = ()
	question: str | None = None
	reason: str = field(default="", compare=False)

	def __post_init__(self):
		if self.kind == "one" and not self.project_id:
			raise ValueError("a one pick needs a project id")
		if self.kind == "ask" and not self.question:
			raise ValueError("an ask needs a question")
		if self.kind == "none" and (self.project_id or self.candidates):
			raise ValueError("a none pick carries no project")

	@classmethod
	def one(cls, project_id, listed_ids, reason=""):
		if project_id not in set(listed_ids):
			raise ValueError(f"{project_id!r} is not in the listing")
		return cls(kind="one", project_id=project_id, reason=reason)

	@classmethod
	def ask(cls, candidates, question, reason=""):
		return cls(kind="ask", candidates=tuple(candidates), question=question, reason=reason)

	@classmethod
	def none(cls, reason=""):
		return cls(kind="none", reason=reason)

	@classmethod
	def coerce(cls, project_id, listed_ids, question, reason=""):
		"""Turn a model answer id into a pick, downgrading an unknown id to a question."""
		listed = tuple(listed_ids)
		if project_id in listed:
			return cls.one(project_id, listed, reason)
		return cls.ask(listed, question, f"unlisted id {project_id!r}")

	def as_thread_pick(self, listed_ids):
		"""View this pick through Twin B's result type, so Twin B scoring reads it unchanged."""
		if self.kind == "one":
			return Pick.one(self.project_id, listed_ids, self.reason)
		if self.kind == "ask":
			return Pick.ask(self.candidates, self.question, self.reason)
		return Pick.none(self.reason)


def question_for_projects(names):
	"""One spoken question naming the candidate projects, from their display names."""
	if not names:
		return "Which project do you mean?"
	shown = "; ".join(names[:3])
	more = f" and {len(names) - 3} more" if len(names) > 3 else ""
	return f"Which project do you mean: {shown}{more}?"


def _phrase_tokens(text):
	"""Every word in order, apostrophes dropped and singularized, stopwords kept for contiguity."""
	return [_singular(_bare(t)) for t in TOKEN.findall((text or "").lower())]


EXPLICIT_PROJECT_WORDS = frozenset({"project", "projects"})

_TARGET_BEFORE = frozenset({"in", "for", "project"})
_TARGET_PAIRS = (("in", "the"), ("for", "the"))


def _occurrences(phrase_tokens, utter_tokens):
	"""Every start index where the phrase appears as one contiguous word run."""
	n = len(phrase_tokens)
	if not n:
		return []
	return [i for i in range(len(utter_tokens) - n + 1)
	        if utter_tokens[i:i + n] == phrase_tokens]


def _is_weak(spoken):
	"""A spoken name too common to trust outside the target seat."""
	tokens = _phrase_tokens(spoken)
	return len(tokens) <= 1 or not content_words(spoken)


def _is_target(tokens, start, end):
	"""True when the span sits where the request names its target project."""
	if start > 0 and tokens[start - 1] in _TARGET_BEFORE:
		return True
	if start > 1 and (tokens[start - 2], tokens[start - 1]) in _TARGET_PAIRS:
		return True
	return end < len(tokens) and tokens[end] in EXPLICIT_PROJECT_WORDS


def _content_set(text):
	return {_singular(w) for w in content_words(text)}


def _names_listed_project(tokens, idx, spoken_tokens, spoken_content):
	"""True when the "project" word at idx touches a listed name or its words."""
	for start in range(max(0, idx - 4), idx):
		if any(tokens[start:idx] == s for s in spoken_tokens if len(s) == idx - start):
			return True
	for length in range(1, 5):
		if any(tokens[idx + 1:idx + 1 + length] == s for s in spoken_tokens if len(s) == length):
			return True
	near = _content_set(" ".join(tokens[max(0, idx - 2):idx] + tokens[idx + 1:idx + 3]))
	return bool(near & spoken_content)


def pick_project(utterance, projects):
	"""Match one instruction against projects with `projectId`, `name` and `spokenNames`.

	Returns a `ProjectPick`: one exact target project, a question with the tied or partial
	projects, or none when the request explicitly names a project the list does not carry.
	"""
	listed = [p["projectId"] for p in projects]
	names = {p["projectId"]: p.get("name", "") for p in projects}
	utter_tokens = _phrase_tokens(utterance)
	spoken_names = [s for p in projects for s in p.get("spokenNames", []) if s]
	spoken_tokens = [_phrase_tokens(s) for s in spoken_names]
	spoken_content = set().union(*[_content_set(s) for s in spoken_names]) if spoken_names else set()

	weak_spans = []
	target_hits, task_exact, exact_spans = {}, {}, []
	for p in projects:
		pid = p["projectId"]
		for s in [s for s in p.get("spokenNames", []) if s]:
			phrase = _phrase_tokens(s)
			runs = _occurrences(phrase, utter_tokens)
			if not runs:
				continue
			if _is_weak(s) and not any(_is_target(utter_tokens, i, i + len(phrase)) for i in runs):
				weak_spans += [(i, i + len(phrase)) for i in runs]
				continue
			slot = (target_hits if any(_is_target(utter_tokens, i, i + len(phrase)) for i in runs)
			        else task_exact)
			slot.setdefault(pid, []).append(phrase)
			exact_spans += [(i, i + len(phrase)) for i in runs]
	ignored = {i for start, end in weak_spans for i in range(start, end)}
	for pid in list(task_exact):
		if pid in target_hits:
			target_hits[pid] += task_exact.pop(pid)
	for pid, runs in list(target_hits.items()) + list(task_exact.items()):
		for s in runs:
			for other in projects:
				if other["projectId"] == pid:
					continue
				prefix_tied = [t2 for s2 in other.get("spokenNames", []) if s2
				               for t2 in [_phrase_tokens(s2)]
				               if len(t2) > len(s) and t2[:len(s)] == s]
				if prefix_tied:
					(task_exact if pid in task_exact else target_hits).setdefault(
						other["projectId"], []).extend(prefix_tied)
	covered = ignored | {i for start, end in exact_spans for i in range(start, end)}
	words = content_words(" ".join(t for i, t in enumerate(utter_tokens) if i not in covered))
	task_partial = set()
	for p in projects:
		pid = p["projectId"]
		if pid in target_hits or pid in task_exact:
			continue
		if any(any(_singular(w) in _content_set(s) for w in words)
		       for s in p.get("spokenNames", []) if s):
			task_partial.add(pid)

	exact_target = [pid for pid in listed if pid in target_hits]
	exact_task = [pid for pid in listed if pid in task_exact]
	if len(exact_target) == 1 and not exact_task:
		return ProjectPick.one(exact_target[0], listed, "exact spoken name")
	project_idx = [i for i, t in enumerate(utter_tokens)
	               if t in EXPLICIT_PROJECT_WORDS and i not in ignored]
	if not exact_target and any(not _names_listed_project(utter_tokens, i, spoken_tokens,
	                                                       spoken_content)
	                            for i in project_idx):
		return ProjectPick.none("names a project no listing carries")
	candidates = exact_target + exact_task + [pid for pid in listed if pid in task_partial]
	if candidates:
		return ProjectPick.ask(candidates, question_for_projects([names[i] for i in candidates]),
		                       "exact tie" if exact_target or exact_task else "inexact match")
	if project_idx:
		return ProjectPick.none("names a project no listing carries")
	return ProjectPick.ask([], question_for_projects([]), "no project named")
