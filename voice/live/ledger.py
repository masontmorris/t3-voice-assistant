"""Call-scoped conversation history and task evidence.

The task service remains authoritative for execution. This ledger retains the
events and outcomes it reported, alongside conversation turns, for later turns.
"""

from dataclasses import asdict, dataclass, field

from voice.prototype.run import carried_tasks


@dataclass
class Turn:
    id: str
    transcript: str | None
    revisions: list[dict]
    generated: list[str]
    delivery: str
    proposals: list[dict]
    executions: dict
    error: str | None = None


@dataclass
class ConversationLedger:
    turns: list[Turn] = field(default_factory=list)
    snapshot: dict | None = None
    tasks: list[dict] = field(default_factory=list)

    def record(self, record, *, muted):
        turn_id = record.get("clip") or f"turn_{len(self.turns) + 1:03d}"
        revisions = [{k: event[k] for k in ("revision", "text", "is_final") if k in event}
                     for event in record.get("timeline", []) if event.get("kind") == "transcript_update"]
        proposals = [{**task, "ref": f"{turn_id}:{task['task_id']}"} for task in record.get("tasks", [])]
        executions = {f"{turn_id}:{task_id}": execution
                      for task_id, execution in record.get("executions", {}).items()}
        generated = [r["text"] for r in record.get("replies", []) if r.get("text")]
        turn = Turn(turn_id, record.get("final_transcript"), revisions, generated,
                    "interrupted" if muted else "queued" if generated else "none",
                    proposals, executions, record.get("error") or record.get("cancelled"))
        self.turns.append(turn)
        self.snapshot = record.get("snapshot_after_tools") or record.get("snapshot") or self.snapshot
        self.tasks.extend(carried_tasks(record))
        return turn

    def conversation(self):
        result = []
        for turn in self.turns:
            item = {}
            if turn.transcript:
                item["user"] = turn.transcript
            if item:
                result.append(item)
        return result

    def record_dict(self):
        return {"turns": [asdict(turn) for turn in self.turns]}
