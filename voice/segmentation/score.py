"""Score final task records against a scenario's hand-written expectations."""

DISPATCHED_FAMILY = {"dispatched", "cancel_requested"}
LIVE = {"proposed", "ready", "needs_clarification", "dispatched"}
CORRECTED = {"superseded", "withdrawn", "cancel_requested"}


def _matches(expected, record):
    text = record["segment_text"].lower()
    return all(word in text for word in expected["match"])


def score(expected, records, dispatch_log, snapshot):
    dispatches = {}
    for d in dispatch_log:
        dispatches.setdefault(d["task_id"], []).append(d)
    claimed, key_of, failures = {}, {}, []
    for exp in expected["tasks"]:
        record = next((r for r in records if r["task_id"] not in key_of and _matches(exp, r)), None)
        if record is None:
            failures.append(f"{exp['key']}: no task matches {exp['match']}")
            continue
        claimed[exp["key"]] = record
        key_of[record["task_id"]] = exp["key"]

    premature = duplicate = missed = 0
    for exp in expected["tasks"]:
        record = claimed.get(exp["key"])
        if record is None:
            if set(exp["status"]) & CORRECTED:
                missed += 1
            continue
        k, sent = exp["key"], dispatches.get(record["task_id"], [])
        if record["status"] not in exp["status"]:
            failures.append(f"{k}: status {record['status']}, expected one of {exp['status']}")
            if set(exp["status"]) <= CORRECTED and record["status"] in LIVE:
                missed += 1
        if "depends_on" in exp:
            got = sorted(key_of.get(d, d) for d in record["depends_on"])
            if got not in [sorted(x) for x in exp["depends_on"]]:
                failures.append(f"{k}: depends_on {got}, expected one of {exp['depends_on']}")
        if "revises" in exp and key_of.get(record["revises"]) != exp["revises"]:
            failures.append(f"{k}: revises {key_of.get(record['revises'], record['revises'])}, "
                            f"expected {exp['revises']}")
            missed += 1
        if sorted(record["sources"]) != sorted(exp.get("sources", ["orchestrator"])):
            failures.append(f"{k}: sources {record['sources']}")
        if sent and (exp.get("never_dispatched") or not set(exp["status"]) & DISPATCHED_FAMILY):
            failures.append(f"{k}: dispatched but should never dispatch")
            premature += len(sent)
        if exp.get("dispatch_only_on_final"):
            early = [d for d in sent if not d["is_final"]]
            if early:
                failures.append(f"{k}: dispatched on partial revision {early[0]['revision']}")
                premature += len(early)
        for word in exp.get("instruction_contains", []):
            if sent and word not in sent[-1]["instruction"].lower():
                failures.append(f"{k}: dispatched instruction lacks {word!r}")
                premature += 1
        if len(sent) > 1:
            failures.append(f"{k}: dispatched {len(sent)} times")
            duplicate += len(sent) - 1

    extra = []
    for record in records:
        if record["task_id"] in key_of:
            continue
        sent = dispatches.get(record["task_id"], [])
        if not sent and record["status"] not in LIVE:
            continue
        twin = next((e["key"] for e in expected["tasks"] if _matches(e, record)), None)
        if sent and twin is not None:
            failures.append(f"{record['task_id']}: duplicate dispatch of {twin}")
            duplicate += len(sent)
        else:
            failures.append(f"{record['task_id']}: unexpected {record['status']} task \"{record['segment_text']}\"")
            premature += len(sent)
        extra.append(record["task_id"])

    clarification = snapshot["clarification_needed"] is not None
    if clarification != expected["clarification_needed"]:
        failures.append(f"clarification_needed {clarification}")
    return {"pass": not failures, "failures": failures, "premature_dispatches": premature,
            "duplicate_dispatches": duplicate, "missed_corrections": missed, "extra_tasks": extra,
            "dispatch_count": len(dispatch_log)}
