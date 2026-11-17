"""Build frozen picker snapshots from a read-only copy of a T3 statev2.sqlite.

The window and project choices come from a spec file, so no project name is baked into this code. Never
point this at a live database.
"""

import json
import sqlite3
from datetime import datetime, timedelta

TERMINAL = {"completed": "finished", "failed": "failed", "interrupted": "interrupted",
            "cancelled": "cancelled", "rolled_back": "failed"}
LIVE = {"preparing", "queued", "starting", "running"}


def _rows(con, sql, args=()):
	return con.execute(sql, args).fetchall()


def _thread_rows(con):
	"""Active threads with their project, pin flag, and their newest run, newest activity first."""
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
		meta = json.loads(payload) if payload else {}
		if meta.get("snoozedUntil") or meta.get("snoozedAt"):
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
		})
	return rows


def _names(con):
	return {pid: title for pid, title in _rows(con, "select project_id, title from projection_projects")}


def _since(now, days):
	start = datetime.fromisoformat(now.replace("Z", "+00:00")) - timedelta(days=days)
	return start.isoformat().replace("+00:00", "Z")


def _visible(rows, names, spec, now):
	by_name = {name: pid for pid, name in names.items()}
	if "projects" in spec:
		per = spec.get("perProject")
		out = []
		for name in spec["projects"]:
			block = [r for r in rows if r["projectId"] == by_name.get(name)]
			out += block[:per] if per else block
	else:
		since = _since(now, spec["sinceDays"]) if spec.get("sinceDays") else None
		skip = {by_name.get(n) for n in spec.get("excludeProjects", [])}
		out = [r for r in rows if (since is None or r["lastActivityAt"] >= since) and r["projectId"] not in skip]
	return out[: spec["limit"]] if spec.get("limit") else out


def _outside(rows, names, spec):
	if not spec:
		return []
	by_name = {name: pid for pid, name in names.items()}
	since = _since(spec["_now"], spec["sinceDays"]) if spec.get("sinceDays") else None
	block = [r for r in rows if r["projectId"] == by_name.get(spec["project"]) and (since is None or r["lastActivityAt"] >= since)]
	return block[: spec.get("limit", 6)]


def build(db_path, spec, now=None):
	con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
	try:
		rows = _thread_rows(con)
		names = _names(con)
	finally:
		con.close()
	now = now or max(r["lastActivityAt"] for r in rows)
	out = []
	for sid in sorted(spec):
		snap_spec = dict(spec[sid])
		visible = _visible(rows, names, snap_spec["visible"], now)
		outside_spec = dict(snap_spec.get("outside") or {})
		outside = []
		if outside_spec:
			outside_spec["_now"] = now
			outside = _outside(rows, names, outside_spec)
		allowed = sorted({t["projectId"] for t in visible})
		if snap_spec.get("allowed") == "all_except_outside":
			by_name = {name: pid for pid, name in names.items()}
			allowed = sorted(set(names) - {by_name.get((snap_spec.get("outside") or {}).get("project"))})
		projects = {pid: names.get(pid, pid) for pid in sorted({t["projectId"] for t in visible + outside})}
		out.append({
			"id": sid,
			"label": snap_spec.get("label", sid),
			"takenAt": now,
			"allowedProjects": allowed,
			"projects": projects,
			"threads": visible + outside,
			"visibleCount": len(visible),
			"outsideCount": len(outside),
		})
	return out
