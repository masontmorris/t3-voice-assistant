"""Check the seam's frame fields and enums against the Python client."""

import inspect
import json
import unittest
from dataclasses import MISSING, fields, is_dataclass
from pathlib import Path
from typing import get_args, get_origin, get_type_hints

from voice.t3 import seam as S
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


def wire_name(name):
    first, *rest = name.split("_")
    return first + "".join(word.title() for word in rest)


class SchemaCoverageTest(unittest.TestCase):
    def assert_fields(self, node, required, optional=()):
        actual_required = set(node.get("required", ()))
        self.assertEqual(actual_required, set(required), "required fields")
        self.assertEqual(
            node["properties"].keys() - actual_required, set(optional), "optional fields"
        )

    def assert_model_fields(self, node, model, *, envelope=()):
        node = resolve(node)
        required = set(envelope)
        optional = set()
        for f in fields(model):
            names = required if f.default is MISSING and f.default_factory is MISSING else optional
            names.add(wire_name(f.name))
        self.assert_fields(node, required, optional)

        for name, annotation in get_type_hints(model).items():
            child = resolve(node["properties"][wire_name(name)])
            if get_origin(annotation) is tuple:
                child = resolve(child["items"])
                annotation = get_args(annotation)[0]
            if type(None) in get_args(annotation):
                annotation, = (arg for arg in get_args(annotation) if arg is not type(None))
                if is_dataclass(annotation):
                    child, = (b for b in child["anyOf"] if b.get("type") != "null")
            if is_dataclass(annotation):
                self.assert_model_fields(child, annotation)

    def test_refusal_codes_match(self):
        self.assertEqual(set(DEFS["RefusalCode"]["enum"]), members(T.RefusalCode))

    def test_watch_end_reasons_match(self):
        self.assertEqual(set(DEFS["WatchEndReason"]["enum"]), members(T.WatchEndReason))

    def test_event_types_match(self):
        branches = DEFS["VoiceEvent"]["anyOf"]
        self.assertEqual({branch_type(b)[0] for b in branches}, set(T.EVENT_TYPES))
        for branch in branches:
            kind, = branch_type(branch)
            with self.subTest(event=kind):
                self.assert_model_fields(branch, T.EVENT_TYPES[kind], envelope=("type",))

    def test_event_enums_match(self):
        by_type = {branch_type(b)[0]: b["properties"] for b in DEFS["VoiceEvent"]["anyOf"]}
        self.assertEqual(set(by_type["run"]["status"]["enum"]), members(T.RunStatus))
        self.assertEqual(set(by_type["needs_you"]["kind"]["enum"]), members(T.PendingKind))
        self.assertEqual(set(by_type["needs_you"]["source"]["enum"]), members(T.NeedSource))
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
            kind, = branch_type(node)
            kinds.append(kind)
            properties = node["properties"]
            with self.subTest(frame=kind):
                if kind == "hello":
                    self.assert_model_fields(node, T.Hello, envelope=("type",))
                elif kind == "watch_ended":
                    self.assert_model_fields(node, T.WatchEnded, envelope=("type",))
                elif kind == "event":
                    self.assert_fields(node, ("type", "event"))
                    self.assertEqual(resolve(properties["event"]), DEFS["VoiceEvent"])
                elif kind == "result":
                    if properties["ok"]["enum"] == [True]:
                        self.assert_fields(node, ("type", "id", "ok", "value"))
                        ok_values.append(properties["value"])
                    else:
                        self.assertEqual(properties["ok"]["enum"], [False])
                        self.assert_fields(node, ("type", "id", "ok", "error"))
                        self.assert_model_fields(properties["error"], T.ResultErr)
        self.assertEqual(sorted(kinds), ["event", "hello"] + ["result"] * 8 + ["watch_ended"])
        self.assertEqual(len(ok_values), len(T.WIRE_OPS))
        models = {
            "list_threads": T.ThreadList,
            "read_thread": T.ThreadState,
            "watch": T.WatchStarted,
            "unwatch": T.WatchStarted,
            "launch": T.LaunchResult,
            "send": T.SendResult,
        }
        self.assertEqual(set(T.RESULT_DECODERS), set(T.WIRE_OPS))
        for op, node in zip(T.WIRE_OPS, ok_values, strict=True):
            with self.subTest(result=op):
                if op == "interrupt":
                    branches = resolve(node)["anyOf"]
                    by_kind = {b["properties"]["kind"]["enum"][0]: b for b in branches}
                    self.assertEqual(by_kind.keys(), T.INTERRUPT_KINDS.keys())
                    for kind, branch in by_kind.items():
                        self.assert_model_fields(
                            branch, T.INTERRUPT_KINDS[kind], envelope=("kind",)
                        )
                else:
                    self.assert_model_fields(node, models[op])

    def test_send_delivery_matches(self):
        delivery = DEFS["VoiceSendResult"]["properties"]["delivery"]
        self.assertEqual(set(delivery["enum"]), members(T.Delivery))

    def test_recent_records_match(self):
        recent = DEFS["VoiceThreadState"]["properties"]["recent"]
        models = {model.kind: model for model in get_args(T.RecentRecord)}
        branches = {b["properties"]["kind"]["enum"][0]: b for b in recent["items"]["anyOf"]}
        self.assertEqual(branches.keys(), models.keys())
        for kind, branch in branches.items():
            with self.subTest(kind=kind):
                self.assert_model_fields(branch, models[kind], envelope=("kind",))
        self.assertEqual(set(branches["tool"]["properties"]["outcome"]["enum"]), members(T.ToolOutcome))

    def test_client_frames_match(self):
        ops = []
        for branch in DEFS["ClientFrame"]["anyOf"]:
            properties = branch["properties"]
            op, = properties["op"]["enum"]
            ops.append(op)
            with self.subTest(op=op):
                self.assert_fields(branch, ("id", "op", "input"))
                parameters = inspect.signature(getattr(S.T3Seam, op)).parameters.values()
                required = set()
                optional = set()
                for parameter in parameters:
                    if parameter.name != "self":
                        names = required if parameter.default is inspect.Parameter.empty else optional
                        names.add(wire_name(parameter.name))
                self.assert_fields(properties["input"], required, optional)
        self.assertEqual(ops, list(T.WIRE_OPS))

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
