"""Every frame type and refusal code in seam.schema.json has a type in types.py.

A schema gain with no Python type fails here, so the seam cannot grow a frame
or a code the voice client cannot read.
"""

import json
import unittest
from pathlib import Path

from voice.t3 import types as T

SCHEMA = json.loads((Path(__file__).parent / "seam.schema.json").read_text(encoding="utf-8"))
DEFS = SCHEMA["$defs"]


def members(values):
    return {m.value for m in values}


def resolve(node):
    ref = node.get("$ref", "")
    if ref.startswith("#/$defs/"):
        return DEFS[ref[len("#/$defs/"):]]
    return node


def branch_type(branch):
    return branch["properties"]["type"]["enum"]


class SchemaCoverageTest(unittest.TestCase):
    def test_refusal_codes_match(self):
        self.assertEqual(set(DEFS["RefusalCode"]["enum"]), members(T.RefusalCode))

    def test_watch_end_reasons_match(self):
        self.assertEqual(set(DEFS["WatchEndReason"]["enum"]), members(T.WatchEndReason))

    def test_event_types_match(self):
        branches = DEFS["VoiceEvent"]["anyOf"]
        self.assertEqual({branch_type(b)[0] for b in branches}, set(T.EVENT_TYPES))

    def test_event_enums_match(self):
        by_type = {branch_type(b)[0]: b["properties"] for b in DEFS["VoiceEvent"]["anyOf"]}
        self.assertEqual(set(by_type["run"]["status"]["enum"]), members(T.RunStatus))
        self.assertEqual(set(by_type["needs_you"]["kind"]["enum"]), members(T.PendingKind))
        self.assertEqual(
            set(by_type["resolved"]["status"]["enum"]), members(T.ResolvedStatus)
        )
        self.assertEqual(
            set(resolve(by_type["done"]["status"])["enum"]), members(T.TerminalStatus)
        )

    def test_server_frames_match(self):
        kinds = []
        ok_values = []
        for branch in DEFS["ServerFrame"]["anyOf"]:
            node = resolve(branch)
            kinds.append(branch_type(node)[0])
            if node["properties"].get("ok", {}).get("enum") == [True]:
                ok_values.append(node["properties"]["value"])
        self.assertEqual(sorted(kinds), ["event", "hello"] + ["result"] * 8 + ["watch_ended"])
        refs = {v.get("$ref", "inline") for v in ok_values}
        self.assertIn("#/$defs/VoiceThreadList", refs)
        self.assertIn("#/$defs/VoiceThreadState", refs)
        self.assertEqual(len(ok_values), len(T.WIRE_OPS))

    def test_client_ops_match(self):
        ops = [
            branch["properties"]["op"]["enum"][0] for branch in DEFS["ClientFrame"]["anyOf"]
        ]
        self.assertEqual(ops, list(T.WIRE_OPS))

    def test_watch_input_keeps_run_id_optional(self):
        watch = next(
            b
            for b in DEFS["ClientFrame"]["anyOf"]
            if b["properties"]["op"]["enum"] == ["watch"]
        )
        request_input = watch["properties"]["input"]
        self.assertEqual(sorted(request_input["required"]), ["afterSequence", "threadId"])
        self.assertIn("runId", request_input["properties"])

    def test_ceiling_enums_match(self):
        ceiling = DEFS["HelloFrame"]["properties"]["ceiling"]
        self.assertEqual(
            set(ceiling["properties"]["maxRuntimeMode"]["enum"]), members(T.RuntimeMode)
        )
        self.assertEqual(
            set(ceiling["properties"]["operations"]["items"]["enum"]),
            members(T.Operation),
        )

    def test_activity_matches(self):
        self.assertEqual(set(DEFS["ThreadActivity"]["enum"]), members(T.ThreadActivity))


if __name__ == "__main__":
    unittest.main()
