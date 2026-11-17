"""Build frozen picker snapshots from a read-only copy of a T3 statev2.sqlite.

The listing rule is the seam's: a thread is listed only when its lineage is listed (never a
subagent, never a side chat) and it was not delegated. Older settled threads stay in, because
the seam reads every active shell rather than the client's trimmed 25-settled view. Windows and
project subsets come from a spec file, so no project name is baked into this code. Never point
this at a live database.
"""

import json
import sqlite3
from datetime import datetime, timedelta

TERMINAL = {
	"completed": "finished",
	"failed": "failed",
	"interrupted": "interrupted",
	"cancelled": "cancelled",
	"rolled_back": "rolled_back",
}
LIVE = {"preparing", "queued", "starting", "running"}
SEAM_LIMIT = 200


def _rows(con, sql, args=()):
	return con.execute(sql, args).fetchall()


def is_listed(payload):
	"""`isListedThreadLineage(row.lineage) && !isDelegatedAppThread(row)` from the T3 contracts."""
	meta = payload or {}
	lineage = meta.get("lineage") or {}
	if lineage.get("relationshipToParent") == "subagent":
		return False
	if lineage.get("sideChat") is True:
		return False
	return not meta.get("delegatedFrom")


def _thread_rows(con):
	"""Listed active threads with project, activity and settle time, newest activity first."""
	newest_run = {}
	for thread_id, status in _rows(
			con, "select thread_id, status from orchestration_v2_projection_runs order by requested_at"):
		newest_run[thread_id] = status
	pending = {tid for (tid,) in _rows(
		con, "select distinct thread_id from orchestration_v2_projection_runtime_requests "
		     "where status not in ('resolved', 'cancelled')")}
	rows = []
	for thread_id, project_id, title, updated_at, payload in _rows(
			con,
			"""select t.thread_id, t.project_id, t.title, t.updated_at, t.payload_json
			   from orchestration_v2_projection_threads t
			   where t.archived_at is null and t.deleted_at is null
			   order by t.updated_at desc""",
	):
		try:
			meta = json.loads(payload) if payload else {}
		except json.JSONDecodeError:
			continue
		if not is_listed(meta):
			continue
		status = newest_run.get(thread_id)
		if thread_id in pending or status == "waiting":
			activity = "waiting_on_you"
		elif status in LIVE:
			activity = "working"
		elif status in TERMINAL:
			activity = TERMINAL[status]
		else:
			activity = "idle"
		rows.append({
			"threadId": thread_id,
			"projectId": project_id,
			"title": title or "",
			"activity": activity,
			"lastActivityAt": updated_at,
			"pinned": bool(meta.get("pinnedAt")),
			"settledAt": meta.get("settledAt") if meta.get("settledOverride") == "settled" else None,
		})
	return rows


def _names(con):
	return {pid: title for pid, title in _rows(con, "select project_id, title from projection_projects")}


def _since(now, days):
	start = datetime.fromisoformat(now.replace("Z", "+00:00")) - timedelta(days=days)
	return start.isoformat().replace("+00:00", "Z")


def _in_window(row, since):
	return since is None or row["lastActivityAt"] >= since


def _visible(rows, names, spec, now):
	by_name = {name: pid for pid, name in names.items()}
	since = _since(now, spec["sinceDays"]) if spec.get("sinceDays") else None
	skip = {by_name.get(n) for n in spec.get("excludeProjects", [])}
	if "projects" in spec:
		out = []
		for name in spec["projects"]:
			block = [r for r in rows if r["projectId"] == by_name.get(name) and _in_window(r, since)]
			out += block[: spec.get("perProject") or None]
	else:
		out = [r for r in rows if _in_window(r, since) and r["projectId"] not in skip]
	return out[: spec["limit"]] if spec.get("limit") else out


def _outside(rows, names, spec, now):
	if not spec:
		return []
	by_name = {name: pid for pid, name in names.items()}
	since = _since(now, spec["sinceDays"]) if spec.get("sinceDays") else None
	block = [r for r in rows if r["projectId"] == by_name.get(spec["project"]) and _in_window(r, since)]
	return block[: spec.get("limit", 6)]


def build(db_path, spec, now=None):
	con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
	try:
		rows = _thread_rows(con)
		names = _names(con)
	finally:
		con.close()
	if not rows:
		raise ValueError(f"no listed threads in {db_path}")
	now = now or max(r["lastActivityAt"] for r in rows)
	out = []
	for sid in sorted(spec):
		snap_spec = dict(spec[sid])
		visible = _visible(rows, names, snap_spec["visible"], now)
		outside = _outside(rows, names, snap_spec.get("outside"), now)
		allowed = sorted({t["projectId"] for t in visible})
		projects = {pid: names.get(pid, pid) for pid in sorted({t["projectId"] for t in visible + outside})}
		# `total` is every listed thread the ceiling allows, at any age; the ceiling the seam
		# reports is what makes "no such thread" untrue.
		kept = [r for r in rows if r["projectId"] in set(allowed)]
		threads = visible + outside
		out.append({
			"id": sid,
			"label": snap_spec.get("label", sid),
			"takenAt": now,
			"allowedProjects": allowed,
			"projects": projects,
			"threads": threads,
			"total": len(kept),
			"truncated": len(kept) > len(threads),
		})
	return out
