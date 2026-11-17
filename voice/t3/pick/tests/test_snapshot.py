import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from voice.t3.pick.snapshot import build, is_listed

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

TOP = {"parentThreadId": None, "relationshipToParent": None, "rootThreadId": "self"}


def payload(**extra):
	return json.dumps({"lineage": dict(TOP), **extra})


def make_db(path):
	con = sqlite3.connect(path)
	con.executescript(SCHEMA)
	con.executemany("insert into projection_projects values (?, ?)", [
		("p1", "alpha"), ("p2", "beta"), ("p3", "outside"),
	])
	rows = [
		("t1", "p1", "Fix the flaky test", "2026-11-16T10:00:00Z", payload()),
		("t2", "p1", "Ship the release", "2026-11-16T09:00:00Z", payload(settledOverride="settled", settledAt="2026-11-10T09:00:00Z")),
		("t3", "p2", "Review the pull request", "2026-11-16T08:00:00Z", payload()),
		("t4", "p2", "Old scratch thread", "2026-10-01T08:00:00Z", payload(settledOverride="settled", settledAt="2026-10-01T08:00:00Z")),
		("t5", "p3", "Hidden work", "2026-11-16T07:00:00Z", payload()),
		("t6", "p1", "Archived thread", "2026-11-16T11:00:00Z", payload(), "2026-11-16T11:00:00Z"),
		("t7", "p1", "Subagent child", "2026-11-16T06:00:00Z",
		 json.dumps({"lineage": {**TOP, "relationshipToParent": "subagent"}})),
		("t8", "p1", "Side chat", "2026-11-16T05:00:00Z",
		 json.dumps({"lineage": {**TOP, "sideChat": True}})),
		("t9", "p1", "Delegated work", "2026-11-16T04:00:00Z",
		 json.dumps({"lineage": dict(TOP), "delegatedFrom": {"environmentId": "e1", "threadId": "x", "title": "y"}})),
		("t10", "p1", "Forked idea", "2026-11-16T03:00:00Z",
		 json.dumps({"lineage": {**TOP, "relationshipToParent": "fork", "parentThreadId": "t1"}})),
		("t11", "p1", "Never settled", "2026-11-16T02:00:00Z", "{"),
	]
	con.executemany(
		"insert into orchestration_v2_projection_threads "
		"(thread_id, project_id, title, created_at, updated_at, archived_at, deleted_at, payload_json) "
		"values (?, ?, ?, ?, ?, ?, NULL, ?)",
		[(r[0], r[1], r[2], r[3], r[3], r[5] if len(r) > 5 else None, r[4]) for r in rows])
	con.executemany("insert into orchestration_v2_projection_runs values (?, ?, ?, ?)", [
		("r1", "t1", "running", "2026-11-16T10:00:00Z"),
		("r2", "t2", "completed", "2026-11-16T09:00:00Z"),
		("r3", "t3", "failed", "2026-11-16T08:00:00Z"),
	])
	con.execute("insert into orchestration_v2_projection_runtime_requests values ('q1', 't3', 'pending')")
	con.commit()
	con.close()


class ListingRuleTest(unittest.TestCase):
	def test_subagents_side_chats_and_delegated_threads_are_not_listed(self):
		self.assertFalse(is_listed(json.loads(json.dumps({"lineage": {**TOP, "relationshipToParent": "subagent"}}))))
		self.assertFalse(is_listed({"lineage": {**TOP, "sideChat": True}}))
		self.assertFalse(is_listed({"lineage": dict(TOP), "delegatedFrom": {"threadId": "x"}}))

	def test_top_level_threads_and_forks_are_listed(self):
		self.assertTrue(is_listed({"lineage": dict(TOP)}))
		self.assertTrue(is_listed({"lineage": {**TOP, "relationshipToParent": "fork"}}))
		self.assertTrue(is_listed({"lineage": {**TOP, "sideChat": False}}))


class SnapshotBuildTest(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		self.db = Path(self.tmp.name) / "state.sqlite"
		make_db(self.db)

	def tearDown(self):
		self.tmp.cleanup()

	def build_one(self, spec):
		return build(self.db, {"S": spec})[0]

	def all_spec(self):
		return {"visible": {"projects": ["alpha", "beta"], "perProject": 10}, "outside": {"project": "outside"}}

	def test_activity_comes_from_the_newest_run_and_a_pending_request_wins(self):
		snap = self.build_one(self.all_spec())
		activity = {t["threadId"]: t["activity"] for t in snap["threads"]}
		self.assertEqual(activity["t1"], "working")
		self.assertEqual(activity["t2"], "finished")
		self.assertEqual(activity["t3"], "waiting_on_you")
		self.assertEqual(activity["t4"], "idle")

	def test_only_listed_lineages_reach_the_snapshot(self):
		snap = self.build_one(self.all_spec())
		ids = [t["threadId"] for t in snap["threads"]]
		for hidden in ("t7", "t8", "t9"):
			self.assertNotIn(hidden, ids)
		self.assertIn("t10", ids)

	def test_a_settled_thread_older_than_every_window_stays_in(self):
		narrow = self.build_one({"visible": {"sinceDays": 7, "excludeProjects": ["outside"]}, "outside": {}})
		self.assertNotIn("t4", [t["threadId"] for t in narrow["threads"]])
		wide = self.build_one({"visible": {"sinceDays": 60, "excludeProjects": ["outside"]}, "outside": {}})
		self.assertIn("t4", [t["threadId"] for t in wide["threads"]])
		self.assertEqual([t["settledAt"] for t in wide["threads"] if t["threadId"] == "t4"], ["2026-10-01T08:00:00Z"])

	def test_a_cut_list_reports_total_and_truncated(self):
		snap = self.build_one({"visible": {"projects": ["alpha", "beta"], "perProject": 10, "limit": 2}, "outside": {}})
		self.assertEqual([t["threadId"] for t in snap["threads"]], ["t1", "t2"])
		self.assertEqual(snap["total"], 3)
		self.assertTrue(snap["truncated"])

	def test_archived_threads_stay_out_and_the_outside_project_cannot_be_seen(self):
		snap = self.build_one(self.all_spec())
		ids = [t["threadId"] for t in snap["threads"]]
		self.assertNotIn("t6", ids)
		self.assertIn("t5", ids)
		self.assertNotIn("p3", snap["allowedProjects"])

	def test_a_project_block_honours_per_project_limits(self):
		snap = self.build_one({"visible": {"projects": ["alpha", "beta"], "perProject": 1}, "outside": {}})
		self.assertEqual([t["threadId"] for t in snap["threads"]], ["t1", "t3"])

	def test_snapshot_json_round_trips(self):
		snap = self.build_one({"visible": {"projects": ["alpha"]}, "outside": {"project": "outside"}})
		self.assertEqual(json.loads(json.dumps(snap))["projects"]["p1"], "alpha")

	def test_a_broken_payload_is_skipped_not_crashed(self):
		snap = self.build_one(self.all_spec())
		self.assertNotIn("t11", [t["threadId"] for t in snap["threads"]])


if __name__ == "__main__":
	unittest.main()
