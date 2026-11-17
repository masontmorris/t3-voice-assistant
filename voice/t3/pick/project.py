"""Deterministic project pick for Twin A. It matches `spokenNames` only and asks whenever the
match is not exact or names more than one project.

An exact match is one spoken name appearing as a contiguous word run in the request. A partial
match shares a content word with a spoken name but never picks. With no overlap at all, an
explicit "project X" names something the list does not carry and is none; a request with no
project wording asks which project. The tokenizer and stopwords are Twin B's, imported, not
forked.
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


def _says(phrase_tokens, utter_tokens):
	"""True when the phrase appears as one contiguous word run in the utterance."""
	n = len(phrase_tokens)
	return bool(n) and any(utter_tokens[i:i + n] == phrase_tokens for i in range(len(utter_tokens) - n + 1))


EXPLICIT_PROJECT_WORDS = frozenset({"project", "projects"})


def pick_project(utterance, projects):
	"""Match one instruction against projects with `projectId`, `name` and `spokenNames`.

	Returns a `ProjectPick`: one exact project, a question with the tied or partial projects,
	or none when the request explicitly names a project the list does not carry.
	"""
	listed = [p["projectId"] for p in projects]
	names = {p["projectId"]: p.get("name", "") for p in projects}
	utter_tokens = _phrase_tokens(utterance)
	words = content_words(utterance)
	exact, partial = [], []
	for p in projects:
		pid = p["projectId"]
		spoken = [s for s in p.get("spokenNames", []) if s]
		if any(_says(_phrase_tokens(s), utter_tokens) for s in spoken):
			exact.append(pid)
			continue
		for s in spoken:
			if any(w in content_words(s) for w in words):
				partial.append(pid)
				break
	if len(exact) == 1:
		return ProjectPick.one(exact[0], listed, "exact spoken name")
	if exact:
		return ProjectPick.ask(exact, question_for_projects([names[i] for i in exact]), "exact tie")
	if partial:
		return ProjectPick.ask(partial, question_for_projects([names[i] for i in partial]),
		                       "inexact match")
	if EXPLICIT_PROJECT_WORDS & set(_phrase_tokens(utterance)):
		return ProjectPick.none("names a project no listing carries")
	return ProjectPick.ask([], question_for_projects([]), "no project named")
