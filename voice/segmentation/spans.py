"""Candidate segment proposers. Each returns segments as [start, end) spans of the current utterance text."""

import re

from voice.segmentation.client import MODEL
from voice.segmentation.contract import Segment

MARKERS = re.compile(r"\b(?:and then|and also|oh and|no wait|never mind|nevermind|and|but|actually|anyway)\b",
                     re.IGNORECASE)
SENTENCE_END = re.compile(r"[.?!;]\s+(?=\S)")
COMMA = re.compile(r",\s*(?=\S)")
FILLER = {"um", "uh", "er", "ah", "hmm", "so", "okay", "ok", "well", "and", "then", "but", "oh", "also", "like"}


def _is_filler(text):
    words = re.findall(r"[a-z']+", text.lower())
    return all(w in FILLER for w in words)


def _trim(text, start, end):
    """Trim spaces and commas from a span. Return None for an empty or filler-only span."""
    raw = text[start:end]
    lead = len(raw) - len(raw.lstrip(" ,"))
    body = raw.strip(" ,")
    return None if not body or _is_filler(body) else (start + lead, start + lead + len(body))


def _pieces(text, cuts):
    bounds = sorted({0, len(text), *cuts})
    return [p for p in (_trim(text, s, e) for s, e in zip(bounds, bounds[1:])) if p]


def _segments(text, spans, is_final):
    return [Segment(f"s{i + 1}", s, e, text[s:e], open=not is_final and i == len(spans) - 1)
            for i, (s, e) in enumerate(spans)]


def clause_cuts(text):
    return {m.end() for m in SENTENCE_END.finditer(text)} | {m.start() for m in MARKERS.finditer(text)}


def clause_segments(text, is_final):
    """Option (a): split at sentence punctuation and at spoken boundary markers."""
    return _segments(text, _pieces(text, clause_cuts(text)), is_final)


def fine_pieces(text):
    """Option (b) candidates: option (a)'s cuts plus every comma."""
    return _pieces(text, clause_cuts(text) | {m.end() for m in COMMA.finditer(text)})


def boundary_request(text, pieces):
    marked = text[pieces[0][0]:pieces[0][1]]
    for k, (start, end) in enumerate(pieces[1:], 1):
        marked += f" [{k}] " + text[start:end]
    questions = {f"b{k}": {
        "type": "noul",
        "instructions": f"Does a separate request, a correction, or a new subject begin at marker [{k}]?",
        "criteria": {"true": "The words after the marker start a separate request, a correction, a withdrawal, or "
                             "a new subject.",
                     "false": "The words after the marker continue the same request, or are filler or a false "
                              "start."}} for k in range(1, len(pieces))}
    return {"model": MODEL, "state": {"transcript_with_markers": marked}, "questions": questions}


def merge_pieces(text, pieces, response, is_final):
    """Merge fine pieces where the decider said no boundary. A malformed answer keeps the boundary."""
    answers = (response or {}).get("answers") or {}
    problems, spans = [], [list(pieces[0])]
    for k, (start, end) in enumerate(pieces[1:], 1):
        answer = answers.get(f"b{k}")
        value = answer.get("noul") if isinstance(answer, dict) else None
        if not isinstance(value, (int, float)):
            problems.append(f"b{k}: missing")
            value = 1.0
        if value >= 0.5:
            spans.append([start, end])
        else:
            spans[-1][1] = end
    return _segments(text, [tuple(s) for s in spans], is_final), problems


EXTRACT_INSTRUCTIONS = ("Each separate thing the speaker asks for, each correction or withdrawal of an earlier "
                        "request, and each sentence that only describes a task, copied exactly from the transcript. "
                        "Two requests joined by \"and\" are two items. Skip filler and small talk.")


def extraction_request(text):
    return {"model": MODEL, "state": text,
            "fields": {"segments": {"type": "list", "items": "text", "instructions": EXTRACT_INSTRUCTIONS}}}


def extracted_segments(text, response, is_final):
    """Option (c): segments from the extraction's spans, falling back to locating the value text."""
    answer = ((response or {}).get("answers") or {}).get("segments")
    if not isinstance(answer, dict) or not isinstance(answer.get("value"), list):
        return [], [f"segments: no list value ({answer!r:.60})"]
    values, spans = answer["value"], answer.get("spans") or []
    problems, found, cursor = [], [], 0
    for i, value in enumerate(values):
        span = spans[i] if i < len(spans) else None
        if isinstance(span, list) and len(span) == 2 and 0 <= span[0] < span[1] <= len(text):
            start, end = span
        else:
            start = text.lower().find(str(value).lower(), cursor)
            if start < 0:
                problems.append(f"segments[{i}]: no span and value not in transcript")
                continue
            problems.append(f"segments[{i}]: no span, located by text")
            end = start + len(str(value))
        found.append((start, end))
        cursor = end
    kept = []
    for start, end in sorted(found):
        if kept and start < kept[-1][1]:
            problems.append(f"overlapping span at {start}")
            continue
        if piece := _trim(text, start, end):
            kept.append(piece)
    return _segments(text, kept, is_final), problems
