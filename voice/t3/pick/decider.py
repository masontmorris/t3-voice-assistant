"""Build one typed choice from the picker's request evidence."""

from dataclasses import dataclass

from voice.liquid_decider.client import MODEL

from .pick import Pick
from .prefilter import _singular, _title_tokens, content_words

NARROW_LIMIT = 8
FULL_LIMIT = 12
MIN_CONFIDENCE = 0.20


@dataclass(frozen=True)
class Candidate:
	id: str
	name: str
	project: str = ""


def shortlist(utterance, items):
	words = {_singular(word) for word in content_words(utterance)}
	return sorted(items, key=lambda item: len(words & (_title_tokens(item.name) | _title_tokens(item.project))),
	              reverse=True)[:NARROW_LIMIT]


def _options(items):
	return {f"item_{i:02d}": item for i, item in enumerate(items, 1)}


def choice_request(kind, evidence, items, task_record_thread_id=None):
	state = {"request": evidence}
	context = ""
	if task_record_thread_id:
		state["task_record_thread_id"] = task_record_thread_id
		context = "If the request refers back to the task record, use its recorded thread id. "
	return {
		"model": MODEL,
		"state": state,
		"questions": {"pick": {
			"type": "choice",
			"instructions": (
				f"Which {kind} did the speaker mean? Read the spoken request and the listing in state. "
				f"Use the listing details to resolve references. {context}"
				"Choose one only when the request identifies one option. "
				"If the request has no clear target or several fit, ask. "
				"If it identifies a target outside the offered choices, choose none."
			),
			"criteria": {
				**{key: f"{item.name} (listed id: {item.id})" for key, item in _options(items).items()},
				"none": "None of these. The speaker means something outside this list.",
				"ask": "Ambiguous, ask. There is no clear target among these options.",
			},
		}},
	}


def interpret(answer, items, kind):
	options = _options(items)
	listed = [item.id for item in items]
	names = " or ".join(item.name for item in items)
	question = f"Do you mean {names}?" if names else f"Which {kind} do you mean?"
	if isinstance(answer, dict) and answer.get("type") == "choice":
		choice, confidence = answer.get("choice"), answer.get("confidence")
		if type(confidence) in (int, float) and 0 <= confidence <= 1 and isinstance(choice, str):
			if choice == "none":
				return Pick.none("decider"), "ok"
			if choice == "ask" or (choice in options and confidence < MIN_CONFIDENCE):
				reason = "decider" if choice == "ask" else "decider low confidence"
				return Pick.ask(listed, question, reason), "ok"
			if choice in options:
				return Pick.one(options[choice].id, listed, "decider"), "ok"
	return Pick.ask(listed, question, "invalid decider choice"), "unparseable"
