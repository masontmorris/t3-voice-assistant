"""Answer reuse: keep each answer keyed by its question and its piece's text, and ask only what is new.

A segmenter request repeats most of its questions from one decision point to the next. Each question names one
segment by its id (`s3_kind`, `s3_at`). An answer is reused when the same utterance asked the same question, word for
word, about a segment with the same text and the same still-being-spoken flag. Two kinds are always asked again:

- the break questions (`_finished`, `_at`) of the newest piece and the one before it, because a new word can turn the
  earlier piece into a correction or move where it ends;
- any question that names no segment, such as a voice delegate's `_same_as`.

The rest of the state (the transcript, the task list) can still change under a reused answer. The bench measures how
often that changes the answer.
"""

import hashlib
import json
from dataclasses import dataclass

BREAKS = ("_finished", "_at")


@dataclass(frozen=True)
class Plan:
    ask: dict
    keys: dict
    reused: dict


def segment_of(name, segments):
    sid = name.split("_", 1)[0]
    return sid if sid in segments else None


def question_key(name, question, state):
    sid = segment_of(name, state["segments"])
    material = [name, question, state["segments"][sid], sid == state.get("still_being_spoken")]
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()


def plan(body, store):
    """Split a request's questions into the ones to ask and the answers `store` already holds."""
    state, questions = body["state"], body["questions"]
    boundary = set(list(state["segments"])[-2:])
    ask, keys, reused = {}, {}, {}
    for name, question in questions.items():
        sid = segment_of(name, state["segments"])
        if sid is None or (sid in boundary and name.endswith(BREAKS)):
            ask[name] = question
            continue
        key = question_key(name, question, state)
        if key in store:
            reused[name] = store[key]
        else:
            ask[name], keys[name] = question, key
    return Plan(ask, keys, reused)


class ReusingClient:
    """Wraps a decider client. Per utterance (the label before its first colon), asks only the questions `plan` keeps."""

    def __init__(self, inner):
        self.inner = inner
        self.stores: dict[str, dict] = {}

    def call(self, route, body, label=""):
        store = self.stores.setdefault(label.split(":", 1)[0], {})
        p = plan(body, store)
        reuse = {"asked": sorted(p.ask), "reused": sorted(p.reused)}
        if not p.ask:
            return {"route": route, "label": label, "request": body, "status": 200,
                    "response": {"answers": dict(p.reused), "usage": {"input_tokens": 0, "output_tokens": 0}},
                    "timing": {"started_at": None, "ms": 0.0, "connect_ms": None}, "attempts": [], "reuse": reuse}
        record = self.inner.call(route, {**body, "questions": p.ask}, label=label)
        answers = (record.get("response") or {}).get("answers")
        if record["status"] != 200 or not isinstance(answers, dict):
            return {**record, "request": body, "reuse": reuse}
        for name, key in p.keys.items():
            if isinstance(answers.get(name), dict):
                store[key] = answers[name]
        response = {**record["response"], "answers": {**p.reused, **answers}}
        return {**record, "request": body, "response": response, "reuse": reuse}
