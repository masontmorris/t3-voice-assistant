"""Frozen project snapshot for the Twin A project pick.

Reads `projection_projects` from a read-only copy of a T3 `statev2.sqlite` and writes one
list with every project the voice may start work in. `spokenNames` come from one fixed rule,
frozen before any label is written:

1. the project name as shown in T3,
2. the workspace folder name, when it differs,
3. each of the above with a trailing parenthetical or bracket suffix dropped, when it differs.

Matching is case-insensitive, so no case variants are stored. Never point this at a live database.
"""

import json
import re
import sqlite3
from datetime import datetime, timezone

SUFFIX = re.compile(r"\s*[\(\[].*[\)\]]\s*$")


def spoken_names(name, folder):
	"""The fixed derivation rule: the name, the folder, and each de-suffixed, deduped."""
	out = []
	for candidate in (name, folder):
		if candidate and candidate.strip():
			out.append(candidate.strip())
	for candidate in (name, folder):
		dropped = SUFFIX.sub("", candidate or "").strip()
		if dropped:
			out.append(dropped)
	seen, names = set(), []
	for candidate in out:
		if candidate and candidate.lower() not in seen:
			seen.add(candidate.lower())
			names.append(candidate)
	return names


def folder_of(workspace_root):
	"""The last path segment, for either separator, without drive letters or empties."""
	parts = [p for p in (workspace_root or "").replace("\\", "/").split("/") if p and ":" not in p]
	return parts[-1] if parts else ""


def build_projects(db_path):
	con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
	try:
		rows = con.execute(
			"select project_id, title, workspace_root from projection_projects "
			"where deleted_at is null order by title").fetchall()
	finally:
		con.close()
	projects = []
	for pid, title, root in rows:
		folder = folder_of(root or "")
		projects.append({
			"projectId": pid,
			"name": title or "",
			"folder": folder,
			"spokenNames": spoken_names(title or "", folder),
		})
	return projects


def snapshot(db_path, taken_at=None):
	projects = build_projects(db_path)
	return {
		"takenAt": taken_at or datetime.now(timezone.utc).isoformat(timespec="seconds"),
		"allowedProjects": sorted(p["projectId"] for p in projects),
		"projects": projects,
	}


def main(argv):
	if len(argv) != 3:
		raise SystemExit("usage: python -m voice.t3.pick.project_snapshot <statev2-copy> <out.json>")
	out = snapshot(argv[1])
	with open(argv[2], "w", encoding="utf-8") as f:
		json.dump(out, f, indent=1, ensure_ascii=False)
		f.write("\n")
	print(f"{len(out['projects'])} projects")


if __name__ == "__main__":
	import sys
	main(sys.argv)
