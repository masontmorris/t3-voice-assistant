"""Deterministic prefilter. It resolves a thread only when exactly one listed thread fits every
word the utterance names. It never picks the closest title.

A state word ("failed", "waiting", "finished") is not a title word. When the utterance carries
only state words, the match runs over activity, so "the one that failed" can never land on a
title that merely contains "failed".

A request whose topic words all come from one project's name asks among that project's threads
when it has two or more, even if one title also carries the name.
"""

import re
from typing import NamedTuple

# Function words, the words that name the container ("thread", "progress"), filler that says how
# the request was asked ("wondering"), and contraction roots after the apostrophe is dropped.
STOPWORDS = frozenset(
	"""a an the and or but if then that this these those it its is are was were be been being am
	do does did doing have has had having i me my mine we us our you your he she they them their
	of in on at to for from by with without about into over after before up down out off again still
	just now so very really quite too also as than there here what whats which who whom whose when
	where why how hows s t not no yes can could should would will shall may might must
	thread threads task tasks project projects thing things work works going progress status update updates
	tell show give check talk ask say please thanks thank ok okay well any some all much many more
	one ones latest recent last back
	let lets don dont isn aren wasn weren hasn haven hadn doesn didn won wouldn couldn shouldn mustn
	needn ain i'm we're they're you're he's she's it's that's what's how's here's there's who's
	wondering wonder curious hey hi hello um uh hmm right quick actually basically definitely maybe
	probably kinda sorta guess happening happened news""".split()
)

# Utterance word -> the activity values it names. Only used when a word is not a title word.
STATE_WORDS = {
	"failed": {"failed"}, "failing": {"failed"}, "broke": {"failed"}, "broken": {"failed"},
	"finished": {"finished"}, "done": {"finished"}, "complete": {"finished"}, "completed": {"finished"},
	"cancelled": {"cancelled"}, "canceled": {"cancelled"},
	"interrupted": {"interrupted"}, "stopped": {"interrupted", "cancelled"},
	"rolled": {"rolled_back"}, "rolledback": {"rolled_back"},
	"working": {"working"}, "running": {"working"}, "busy": {"working"}, "active": {"working"},
	"waiting": {"waiting_on_you"}, "blocked": {"waiting_on_you"}, "stuck": {"waiting_on_you"},
	"idle": {"idle"},
}

TOKEN = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")


class Match(NamedTuple):
	candidates: list
	words: list
	state_only: bool
	by_project: bool = False


def _bare(raw):
	"""Drop any apostrophe suffix: mason's -> mason, let's -> let, don't -> don."""
	return raw.split("'", 1)[0]


def content_words(utterance):
	words = []
	for raw in TOKEN.findall(utterance.lower()):
		word = _bare(raw)
		if not word or word in STOPWORDS or word in words:
			continue
		words.append(word)
	return words


def _singular(word):
	return word[:-1] if len(word) > 3 and word.endswith("s") else word


def _tokens(text):
	return [_bare(t) for t in TOKEN.findall((text or "").lower())]


def _title_tokens(title):
	"""Each word, plus each adjacent pair joined, so "play test" also matches "playtest"."""
	words = _tokens(title)
	return {_singular(w) for w in words + [a + b for a, b in zip(words, words[1:])]}


def matches(title, words):
	tokens = _title_tokens(title)
	return all(_singular(w) in tokens for w in words)


def _activities(state):
	out = set()
	for word in state:
		out |= STATE_WORDS[word]
	return out


def _project_threads(topic, threads, projects):
	"""Every thread of each listed project with two or more threads whose name carries every topic word."""
	by_project = {}
	for t in threads:
		by_project.setdefault(t["projectId"], []).append(t["threadId"])
	out = []
	for pid, ids in by_project.items():
		name = {_singular(w) for w in _tokens(projects.get(pid, ""))}
		if len(ids) >= 2 and all(_singular(w) in name for w in topic):
			out += ids
	return out


def prefilter(utterance, threads, projects=None):
	"""`threads` are dicts with `threadId`, `projectId`, `title` and `activity`; `projects` maps a
	project id to its name. Returns the fit and why."""
	words = content_words(utterance)
	topic = [w for w in words if w not in STATE_WORDS]
	state = [w for w in words if w in STATE_WORDS]
	if not words:
		return Match([], words, False)
	if not topic:
		activities = _activities(state)
		hits = [t["threadId"] for t in threads if t.get("activity") in activities]
		return Match(hits, words, True)
	if not state:
		by_project = _project_threads(topic, threads, projects or {})
		if by_project:
			return Match(by_project, words, False, by_project=True)
	hits = [t["threadId"] for t in threads if matches(t.get("title", ""), topic)]
	if state and hits:
		# A title that carries the state word ("Fix broken login") stays a candidate, so it ties
		# with the thread whose activity matches instead of losing to it.
		activities = _activities(state)
		narrowed = [t["threadId"] for t in threads
		            if t["threadId"] in hits and (t.get("activity") in activities
		                                          or any(matches(t.get("title", ""), [w]) for w in state))]
		if narrowed:
			hits = narrowed
	return Match(hits, words, False)
