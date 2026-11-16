"""The backlog of task updates that arrive while LFM3 is replying, and the rule that decides whether to re-prompt.

The runner records every status change the task service reports while a reply is being generated. When the reply
ends, `to_tell` picks what the person has not heard yet, and the runner sends it as one merged update. Updates that
arrive during that update's reply go into the next backlog. A reply here ends at the model's `response.done`, as in
the rest of the prototype, not when its audio would finish playing.
"""

from dataclasses import asdict, dataclass

TOLD = ("completed", "failed")


@dataclass(frozen=True)
class Arrival:
    task_id: str
    old: str
    new: str
    summary: str | None
    at: float
    during: str


class Backlog:
    def __init__(self):
        self.entries: list[Arrival] = []
        self.history: list[dict] = []

    def record(self, task_id, old, new, summary, at, during):
        self.entries.append(Arrival(task_id, old, new, summary, at, during))

    def drain(self):
        entries, self.entries = self.entries, []
        return entries

    def decide(self, entries, executions, at):
        """The rule: re-prompt for every result the person has not heard, merged into one update. A cancellation is
        never re-prompted, because the person or the voice model asked for it."""
        tell = [e for e in entries if e.new in TOLD and not executions[e.task_id].reported]
        self.history.append({"at": round(at, 4), "backlog": [asdict(e) for e in entries],
                             "tell": [e.task_id for e in tell], "reprompt": bool(tell)})
        return tell
