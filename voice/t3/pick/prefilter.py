"""Deterministic prefilter. It resolves a thread only when exactly one listed title contains every
content word the utterance names. It never picks the closest title."""

import re

# Function words plus the words that name the container ("thread", "progress") rather than its subject.
STOPWORDS = frozenset(
	"""a an the and or but if then that this these those it its it's is are was were be been being am
	do does did doing done have has had having i me my mine we us our you your he she they them their
	of in on at to for from by with without about into over after before up down out off again still
	just now so very really quite too also as than there here what whats which who whom whose when where
	why how hows s t not no yes can could should would will shall may might must
	thread threads task tasks thing things work works working going progress status update updates
	tell show give check talk ask say please thanks thank ok okay well any some all much many more
	one ones latest recent last""".split()
)

TOKEN = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")


def content_words(utterance):
	words = []
	for raw in TOKEN.findall(utterance.lower()):
		word = raw[:-2] if raw.endswith("'s") else raw
		if word in STOPWORDS or not word:
			continue
		if word not in words:
			words.append(word)
	return words


def _singular(word):
	return word[:-1] if len(word) > 3 and word.endswith("s") else word


def _title_tokens(title):
	return {_singular(t) for t in TOKEN.findall((title or "").lower())}


def matches(title, words):
	tokens = _title_tokens(title)
	return all(_singular(w) in tokens for w in words)


def prefilter(utterance, threads):
	"""`threads` is a list of dicts with `threadId` and `title`. Returns (candidates, words)."""
	words = content_words(utterance)
	if not words:
		return [], words
	hits = [t["threadId"] for t in threads if matches(t.get("title", ""), words)]
	return hits, words
