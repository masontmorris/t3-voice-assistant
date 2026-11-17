"""The picker's result type. One thread, a question with candidates, or nothing found.

`Pick.one` and `Pick.coerce` both take the listing and refuse an id that is not in it, so no
caller can speak about a thread that was never listed. `Pick.coerce` is what a model's answer
passes through, and an unknown id becomes a question.
"""

from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True)
class Pick:
	kind: Literal["one", "ask", "none"]
	thread_id: str | None = None
	candidates: tuple[str, ...] = ()
	question: str | None = None
	reason: str = field(default="", compare=False)

	def __post_init__(self):
		if self.kind == "one" and not self.thread_id:
			raise ValueError("a one pick needs a thread id")
		if self.kind == "ask" and not self.question:
			raise ValueError("an ask needs a question")
		if self.kind == "none" and (self.thread_id or self.candidates):
			raise ValueError("a none pick carries no thread")

	@classmethod
	def one(cls, thread_id, listed_ids, reason=""):
		if thread_id not in set(listed_ids):
			raise ValueError(f"{thread_id!r} is not in the listing")
		return cls(kind="one", thread_id=thread_id, reason=reason)

	@classmethod
	def ask(cls, candidates, question, reason=""):
		"""Candidates may be empty when nothing in the list is worth naming ("which thread?")."""
		return cls(kind="ask", candidates=tuple(candidates), question=question, reason=reason)

	@classmethod
	def none(cls, reason=""):
		return cls(kind="none", reason=reason)

	@classmethod
	def coerce(cls, thread_id, listed_ids, question, reason=""):
		"""Turn an answer id into a pick, downgrading an unknown id to a question."""
		listed = tuple(listed_ids)
		if thread_id in listed:
			return cls.one(thread_id, listed, reason)
		return cls.ask(listed, question, f"unlisted id {thread_id!r}")


def question_for(titles):
	"""One spoken question naming the candidates, from their titles."""
	if not titles:
		return "Which thread do you mean?"
	names = "; ".join(titles[:3])
	more = f" and {len(titles) - 3} more" if len(titles) > 3 else ""
	return f"Which thread do you mean: {names}{more}?"
