"""Word error rate of a transcript against the line it should say, and the slot words it got wrong.

A slot word is part of a thread name (a capitalised word that does not open a plain sentence), a number, or a status.
The brief counts any substitution or loss of one as a failed line, whatever the WER.
"""

import re

ONES = ("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
        "seventeen eighteen nineteen").split()
TENS = "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()
STATUSES = {"running", "finished", "failed", "stopped", "waiting", "needs", "approve", "completed", "started",
            "nothing", "stop", "start"}
OPENERS = {"i", "i've", "is", "should", "the", "it", "nothing", "started", "stopped"}


def spoken_number(n):
    if n < 20:
        return [ONES[n]]
    if n < 100:
        return [TENS[n // 10]] + ([ONES[n % 10]] if n % 10 else [])
    return [str(n)]


def tokens(text):
    """Lower-case words with numbers spelled out, so "14" and "fourteen" match."""
    out = []
    for word in re.findall(r"[A-Za-z0-9']+", text.replace("’", "'")):
        out += spoken_number(int(word)) if word.isdigit() else [word.lower().strip("'")]
    return out


def slots(line):
    """The positions in `tokens(line)` that hold a slot word."""
    marked = []
    for word in re.findall(r"[A-Za-z0-9']+", line.replace("’", "'")):
        number = word.isdigit()
        name = word[0].isupper() and word.lower() not in OPENERS
        status = word.lower() in STATUSES
        count = len(spoken_number(int(word))) if number else 1
        marked += [number or name or status] * count
    return marked


def align(ref, hyp):
    """Edit operations turning `ref` into `hyp`: (op, ref_index) for every reference word, plus insertions."""
    rows, cols = len(ref) + 1, len(hyp) + 1
    cost = [[0] * cols for _ in range(rows)]
    for i in range(rows):
        cost[i][0] = i
    for j in range(cols):
        cost[0][j] = j
    for i in range(1, rows):
        for j in range(1, cols):
            cost[i][j] = min(cost[i - 1][j] + 1, cost[i][j - 1] + 1,
                             cost[i - 1][j - 1] + (ref[i - 1] != hyp[j - 1]))
    ops, i, j = [], len(ref), len(hyp)
    while i or j:
        if i and j and cost[i][j] == cost[i - 1][j - 1] + (ref[i - 1] != hyp[j - 1]):
            ops.append(("ok" if ref[i - 1] == hyp[j - 1] else "sub", i - 1))
            i, j = i - 1, j - 1
        elif i and cost[i][j] == cost[i - 1][j] + 1:
            ops.append(("del", i - 1))
            i -= 1
        else:
            ops.append(("ins", None))
            j -= 1
    return ops[::-1], cost[-1][-1]


def score(line, transcript):
    ref, hyp = tokens(line), tokens(transcript)
    ops, errors = align(ref, hyp)
    marked = slots(line)
    missed = [ref[i] for op, i in ops if op in ("sub", "del") and marked[i]]
    return {"ref_words": len(ref), "errors": errors, "wer": round(errors / len(ref), 4) if ref else None,
            "slot_misses": missed, "slot_ok": not missed}
