import json
import tempfile
import unittest
from pathlib import Path

from voice.t3.pick.manifest import MANIFEST, verify, write_once


class ManifestTest(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		self.root = Path(self.tmp.name)
		(self.root / "snapshots").mkdir()
		(self.root / "labels").mkdir()
		(self.root / "snapshot-spec.json").write_text("{}", encoding="utf-8")
		(self.root / "snapshots" / "snap-A.json").write_text("{\"id\": \"A\"}", encoding="utf-8")
		(self.root / "labels" / "known-failures.jsonl").write_text("{}\n", encoding="utf-8")

	def tearDown(self):
		self.tmp.cleanup()

	def test_a_second_write_is_refused(self):
		write_once(self.root, commit="a" * 40)
		with self.assertRaises(FileExistsError):
			write_once(self.root, commit="a" * 40)

	def test_the_manifest_hashes_every_input_and_names_a_commit(self):
		body = write_once(self.root, repo=Path(__file__).resolve().parents[3])
		self.assertEqual(sorted(body["files"]), ["labels/known-failures.jsonl", "snapshot-spec.json", "snapshots/snap-A.json"])
		self.assertRegex(body["builderCommit"], r"^[0-9a-f]{40}$")
		self.assertNotIn(MANIFEST, body["files"])

	def test_verify_detects_a_changed_label(self):
		write_once(self.root, commit="a" * 40)
		(self.root / "labels" / "known-failures.jsonl").write_text("{}\n{}\n", encoding="utf-8")
		with self.assertRaises(ValueError):
			verify(self.root)

	def test_verify_accepts_matching_inputs(self):
		write_once(self.root, commit="a" * 40)
		self.assertTrue(verify(self.root)["files"])

	def test_verify_refuses_without_a_manifest(self):
		with self.assertRaises(FileNotFoundError):
			verify(self.root)


if __name__ == "__main__":
	unittest.main()
