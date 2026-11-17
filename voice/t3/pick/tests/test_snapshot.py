import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from voice.t3.pick.snapshot import build

SCHEMA = """
create table orchestration_v2_projection_threads (
  thread_id text, project_id text, title text, created_at text, updated_at text,
  archived_at text, deleted_at text, payload_json text);
create table orchestration_v2_projection_runs (
  run_id text, thread_id text, status text, requested_at text);
create table orchestration_v2_projection_runtime_requests (
  runtime_request_id text, thread_id text, status text);
create table projection_projects (project_id text, title text);
"""


def make_db(path):
	con = sqlite3.connect(path)
	con.executescript(SCHEMA)
	con.executemany("insert into projection_projects values (?, ?)", [
		("p1", "alpha"), ("p2", "beta"), ("p3", "outside"),
	])
	rows = [
		("t1", "p1", "Fix the flaky test", "2026-11-16T10:00:00Z", "2026-11-16T10:00:00Z", None, None),
		("t2", "p1", "Ship the release", "2026-11-16T09:00:00Z", "2026-11-16T09:00:00Z", None, None),
		("t3", "p2", "Review the pull request", "2026-11-16T08:00:00Z", "2026-11-16T08:00:00Z", None, None),
		("t4", "p2", "Old scratch thread", "2026-10-01T08:00:00Z", "2026-10-01T08:00:00Z", None, None),
		("t5", "p3", "Hidden work", "2026-11-16T07:00:00Z", "2026-11-16T07:00:00Z", None, None),
		("t6", "p1", "Archived thread", "2026-11-16T11:00:00Z", "2026-11-16T11:00:00Z", "2026-11-16T11:00:00Z", None),
	]
	con.executemany(
		"insert into orchestration_v2_projection_threads "
		"(thread_id, project_id, title, created_at, updated_at, archived_at, deleted_at, payload_json) "
		"values (?, ?, ?, ?, ?, ?, ?, '{}')", rows)
	con.executemany("insert into orchestration_v2_projection_runs values (?, ?, ?, ?)", [
		("r1", "t1", "running", "2026-11-16T10:00:00Z"),
		("r2", "t2", "completed", "2026-11-16T09:00:00Z"),
		("r3", "t3", "failed", "2026-11-16T08:00:00Z"),
	])
	con.execute("insert into orchestration_v2_projection_runtime_requests values ('q1', 't3', 'pending')")
	con.commit()
	con.close()


class SnapshotBuildTest(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		self.db = Path(self.tmp.name) / "state.sqlite"
		make_db(self.db)

	def tearDown(self):
		self.tmp.cleanup()

	def build_one(self, spec):
		return build(self.db, {"S": spec})[0]

	def test_activity_comes_from_the_newest_run_and_a_pending_request_wins(self):
		snap = self.build_one({"visible": {"projects": ["alpha", "beta"], "perProject": 5}, "outside": {"project": "outside"}})
		activity = {t["threadId"]: t["activity"] for t in snap["threads"]}
		self.assertEqual(activity["t1"], "working")
		self.assertEqual(activity["t2"], "finished")
		self.assertEqual(activity["t3"], "waiting_on_you")
		self.assertEqual(activity["t4"], "idle")

	def test_archived_threads_stay_out_and_the_outside_project_cannot_be_seen(self):
		snap = self.build_one({"visible": {"projects": ["alpha", "beta"], "perProject": 5}, "outside": {"project": "outside"}})
		ids = [t["threadId"] for t in snap["threads"]]
		self.assertNotIn("t6", ids)
		self.assertIn("t5", ids)
		self.assertNotIn("p3", snap["allowedProjects"])

	def test_a_window_keeps_newest_first_and_caps_the_list(self):
		snap = self.build_one({"visible": {"sinceDays": 7, "excludeProjects": ["outside"], "limit": 2},
		                       "outside": {"project": "outside", "limit": 1}})
		self.assertEqual([t["threadId"] for t in snap["threads"]], ["t1", "t2", "t5"])
		self.assertEqual(snap["visibleCount"], 2)
		self.assertEqual(snap["outsideCount"], 1)

	def test_a_project_block_honours_per_project_limits(self):
		snap = self.build_one({"visible": {"projects": ["alpha", "beta"], "perProject": 1}, "outside": {}})
		self.assertEqual([t["threadId"] for t in snap["threads"]], ["t1", "t3"])

	def test_snapshot_json_round_trips(self):
		snap = self.build_one({"visible": {"projects": ["alpha"]}, "outside": {"project": "outside"}})
		self.assertEqual(json.loads(json.dumps(snap))["projects"]["p1"], "alpha")


if __name__ == "__main__":
	unittest.main()
