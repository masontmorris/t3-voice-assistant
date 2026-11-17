import json
import tempfile
import unittest
from pathlib import Path

from voice.archive import synthetic
from voice.archive.export import export
from voice.archive.schema import EVENTS
from voice.archive.validate import check

EXAMPLE = Path(__file__).resolve().parents[1] / "example"


def timeline(root):
    return [json.loads(line) for line in (root / "timeline.jsonl").read_text(encoding="utf-8").splitlines()]


def rewrite(root, events):
    (root / "timeline.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")


class ArchiveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.work = Path(self.tmp.name)
        self.server, self.client = synthetic.build(self.work / "run")
        self.archive = export(self.server, self.client, self.work / "archive")

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_scripted_run_exports_to_a_valid_archive_with_every_server_event_type(self):
        report = check(self.archive)
        self.assertEqual(report["errors"], [])
        self.assertEqual(set(report["counts"]), set(EVENTS) - {"backlog_reprompt"})
        self.assertTrue(all(abs(row["lag_ms"]) <= 20 for row in report["alignment"]))

    def test_events_land_on_the_replay_clock_where_their_audio_sounds(self):
        events = timeline(self.archive)
        starts = [e for e in events if e["type"] == "speech_start"]
        self.assertEqual([e["turn"] for e in starts], [1, 2, 3])
        barge = next(e for e in events if e["type"] == "barge_in")
        cut = next(e for e in events if e["type"] == "reply_end" and e["interrupted"])
        self.assertAlmostEqual(barge["t"], cut["t"], places=2)
        gates = [(e["passed"], e["threshold"]) for e in events if e["type"] == "gate_verdict"]
        self.assertIn((False, 0.8), gates)

    def test_a_reply_moved_by_50_ms_fails_validation(self):
        events = timeline(self.archive)
        reply = next(e for e in events if e["type"] == "reply_start")
        reply["audio"]["at"] += 0.05
        rewrite(self.archive, events)
        errors = check(self.archive)["errors"]
        self.assertTrue(any("ms from where the timeline puts it" in e or "not found" in e for e in errors), errors)

    def test_an_edited_stem_fails_its_hash(self):
        with open(self.archive / "stems" / "conversation.wav", "r+b") as f:
            f.seek(2000)
            f.write(b"\x7f\x7f")
        self.assertIn("stems/conversation.wav: SHA-256 does not match the manifest", check(self.archive)["errors"])

    def test_out_of_order_and_malformed_events_fail(self):
        events = timeline(self.archive)
        events[3]["t"] = events[2]["t"] - 1
        del events[5]["turn"]
        rewrite(self.archive, events)
        errors = check(self.archive)["errors"]
        self.assertTrue(any("is before the previous event" in e for e in errors), errors)
        self.assertTrue(any("missing ['turn']" in e for e in errors), errors)

    def test_a_late_client_log_still_places_the_turn_by_its_audio(self):
        log = self.client / "client.jsonl"
        entries = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        for entry in entries:
            if entry["kind"] == "turn_end":
                entry["t"] += 0.3
        log.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")
        moved = export(self.server, self.client, self.work / "moved")
        before = {e["seq"]: e["t"] for e in timeline(self.archive) if e["type"] == "speech_start"}
        after = {e["seq"]: e["t"] for e in timeline(moved) if e["type"] == "speech_start"}
        self.assertEqual(before, after)
        self.assertEqual(check(moved)["errors"], [])


class ExampleTest(unittest.TestCase):
    def test_the_committed_example_is_valid_and_reproducible(self):
        self.assertEqual(check(EXAMPLE)["errors"], [])
        with tempfile.TemporaryDirectory() as tmp:
            server, client = synthetic.build(Path(tmp) / "run")
            fresh = export(server, client, Path(tmp) / "archive", created_at=synthetic.CREATED_AT)
            committed = json.loads((EXAMPLE / "manifest.json").read_text(encoding="utf-8"))["files"]
            rebuilt = json.loads((fresh / "manifest.json").read_text(encoding="utf-8"))["files"]
            self.assertEqual(committed, rebuilt)


if __name__ == "__main__":
    unittest.main()
