"""Build one title-only choice and map the answer back to a local pick."""

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


def choice_request(kind, utterance, items, visible):
	# Only existing name words may leave the machine. The utterance and all listing metadata stay local.
	vocabulary = {word for item in visible for name in (item.name, item.project) for word in content_words(name)}
	return {
		"model": MODEL,
		"state": {"heard_name_words": [word for word in content_words(utterance) if word in vocabulary]},
		"questions": {"pick": {
			"type": "choice",
			"instructions": (
				f"Which {kind} did the speaker mean? Use the words they said that also occur in listed names. "
				"Choose one only when those words identify it. If there is no evidence or several fit, ask. "
				"If the words point to something outside the offered choices, choose none."
			),
			"criteria": {
				**{key: f"{item.name} (project: {item.project})" if item.project else item.name
				   for key, item in _options(items).items()},
				"none": "None of these. The speaker means something outside this list.",
				"ask": "Ambiguous, ask. There is no clear target among these options.",
			},
		}},
	}


def interpret(answer, items, kind):
	question = f"Which {kind} do you mean?"
	options = _options(items)
	if isinstance(answer, dict) and answer.get("type") == "choice":
		choice, confidence = answer.get("choice"), answer.get("confidence")
		if type(confidence) in (int, float) and 0 <= confidence <= 1 and isinstance(choice, str):
			if choice == "none":
				return Pick.none("decider"), "ok"
			if choice == "ask" or (choice in options and confidence < MIN_CONFIDENCE):
				reason = "decider" if choice == "ask" else "decider low confidence"
				return Pick.ask([], question, reason), "ok"
			if choice in options:
				return Pick.one(options[choice].id, [item.id for item in items], "decider"), "ok"
	return Pick.ask([], question, "invalid decider choice"), "unparseable"
