"""Tests for the Twin A snapshot rule, on synthetic fixtures only."""

import json
import sqlite3
import unittest

from voice.t3.pick.project_snapshot import build_projects, folder_of, spoken_names


class SpokenNamesTest(unittest.TestCase):
	def test_name_and_folder_when_they_differ(self):
		self.assertEqual(spoken_names("Harbor Light", "harbor-light"),
		                 ["Harbor Light", "harbor-light"])

	def test_a_fork_suffix_adds_the_bare_name(self):
		self.assertEqual(spoken_names("Delta (fork)", "delta-fork"),
		                 ["Delta (fork)", "delta-fork", "Delta"])

	def test_identical_name_and_folder_dedupe(self):
		self.assertEqual(spoken_names("Quartz", "Quartz"), ["Quartz"])

	def test_empty_folder_falls_back_to_the_name(self):
		self.assertEqual(spoken_names("Quartz", ""), ["Quartz"])

	def test_bracket_suffixes_drop_the_same_way(self):
		self.assertEqual(spoken_names("Atlas [old]", "atlas"),
		                 ["Atlas [old]", "atlas"])


class FolderTest(unittest.TestCase):
	def test_windows_roots_give_the_last_segment(self):
		self.assertEqual(folder_of("C:\\Users\\someone\\Documents\\harbor-light"), "harbor-light")

	def test_posix_roots_give_the_last_segment(self):
		self.assertEqual(folder_of("/home/someone/harbor-light"), "harbor-light")

	def test_empty_roots_give_empty_folders(self):
		self.assertEqual(folder_of(""), "")


class BuilderTest(unittest.TestCase):
	def test_deleted_projects_stay_out(self):
		import os
		import tempfile
		handle, path = tempfile.mkstemp(suffix=".sqlite")
		os.close(handle)
		os.remove(path)
		con = sqlite3.connect(path)
		try:
			con.execute("create table projection_projects "
			            "(project_id text, title text, workspace_root text, deleted_at text)")
			con.execute("insert into projection_projects values ('p1', 'Quartz', 'C:\\w\\quartz', null)")
			con.execute("insert into projection_projects values ('p2', 'Gone', 'C:\\w\\gone', '2026-01-01')")
			con.commit()
		finally:
			con.close()
		try:
			projects = build_projects(path)
		finally:
			import os
			os.remove(path)
		self.assertEqual([p["projectId"] for p in projects], ["p1"])
		self.assertEqual(projects[0]["spokenNames"], ["Quartz"])
		self.assertEqual(json.loads(json.dumps(projects))[0]["folder"], "quartz")


if __name__ == "__main__":
	unittest.main()
