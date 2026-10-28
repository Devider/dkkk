"""S6.5 (AR-Q18, design D7): the stream event schema ``v = 1`` changes only additively and matches the OpenAPI.

Both sides are reduced to one shape: ``{event type: {field: {type, nullable, required, enum?, items?}}}`` of the
``data`` JSON. ``event`` is left out (it is the frame's ``event:`` line). ``required`` means «always in ``data`` and
never null by default»: every field is serialized, so a field with a non-null default (``v``) is always there.

The snapshot ``contracts/stream_events_v1.json`` is rewritten only with ``UPDATE_STREAM_CONTRACT=1`` and only when the
change is additive; a breaking change needs ``v = 2``, which is a story of its own.
"""

import copy
import json
import os
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import BaseModel

from aigw_service.api.v2.stream.events import EVENT_TYPES

SNAPSHOT = Path(__file__).parent / "contracts" / "stream_events_v1.json"
OPENAPI = Path(__file__).resolve().parents[3] / "openapi_copilot.yaml"
SKIP = "event"

Fields = dict[str, dict[str, Any]]
Contract = dict[str, Fields]


# ---------------------------------------------------------------- the shape of the pydantic models


def _pydantic_field(spec: dict[str, Any], defs: dict[str, Any], required: bool) -> dict[str, Any]:
    nullable = False
    if "anyOf" in spec:
        options = [o for o in spec["anyOf"] if o.get("type") != "null"]
        nullable = len(options) < len(spec["anyOf"])
        (spec,) = options
    if "$ref" in spec:
        spec = defs[spec["$ref"].rsplit("/", 1)[-1]]
    field: dict[str, Any] = {"type": spec["type"], "nullable": nullable, "required": required}
    if "const" in spec:
        field["enum"] = [spec["const"]]
    elif "enum" in spec:
        field["enum"] = list(spec["enum"])
    if spec["type"] == "array":
        item = spec["items"]
        if "$ref" in item:
            item = defs[item["$ref"].rsplit("/", 1)[-1]]
        field["items"] = _pydantic_object(item, defs) if item.get("type") == "object" else {"type": item["type"]}
    return field


def _pydantic_object(schema: dict[str, Any], defs: dict[str, Any]) -> Fields:
    required = set(schema.get("required", []))
    out: Fields = {}
    for name, spec in schema["properties"].items():
        if name == SKIP:
            continue
        # a field with a non-null default is always serialized: on the wire it is required
        always = name in required or spec.get("default") is not None
        out[name] = _pydantic_field(spec, defs, always)
    return out


def contract(types: dict[str, type[BaseModel]] | None = None) -> Contract:
    """The ``data`` shape of every event type of the service."""
    result: Contract = {}
    for event, cls in (types or EVENT_TYPES).items():
        schema = cls.model_json_schema()
        result[event] = _pydantic_object(schema, schema.get("$defs", {}))
    return result


# ---------------------------------------------------------------- the shape of the OpenAPI description


def _openapi_field(spec: dict[str, Any], required: bool) -> dict[str, Any]:
    field: dict[str, Any] = {"type": spec["type"], "nullable": bool(spec.get("nullable")), "required": required}
    if "enum" in spec:
        field["enum"] = list(spec["enum"])
    if spec["type"] == "array":
        item = spec["items"]
        field["items"] = _openapi_object(item) if item.get("type") == "object" else {"type": item["type"]}
    return field


def _openapi_object(schema: dict[str, Any]) -> Fields:
    required = set(schema.get("required", []))
    return {
        name: _openapi_field(spec, name in required) for name, spec in schema["properties"].items() if name != SKIP
    }


def openapi_contract(document: dict[str, Any]) -> Contract:
    """``StreamEventData.oneOf`` → ``{event type: fields}``; each schema is ``allOf`` of the common part and its own."""
    schemas = document["components"]["schemas"]
    result: Contract = {}
    for ref in schemas["StreamEventData"]["oneOf"]:
        schema = schemas[ref["$ref"].rsplit("/", 1)[-1]]
        merged: dict[str, Any] = {"properties": {}, "required": []}
        for part in schema["allOf"]:
            part = schemas[part["$ref"].rsplit("/", 1)[-1]] if "$ref" in part else part
            merged["properties"].update(part.get("properties", {}))
            merged["required"] += part.get("required", [])
        event = merged["properties"][SKIP]["enum"][0]
        result[event] = _openapi_object(merged)
    return result


# ---------------------------------------------------------------- comparison


def _compare_fields(where: str, old: Fields, new: Fields) -> list[str]:
    problems: list[str] = []
    for name, was in old.items():
        path = f"{where}.{name}"
        now = new.get(name)
        if now is None:
            problems.append(f"{path}: removed")
            continue
        problems += [
            f"{path}: {key} {was.get(key)!r} -> {now.get(key)!r}"
            for key in ("type", "nullable", "enum")
            if was.get(key) != now.get(key)
        ]
        if was["required"] != now["required"]:
            problems.append(f"{path}: required {was['required']} -> {now['required']}")
        if isinstance(was.get("items"), dict) and "type" not in was["items"]:
            problems += _compare_fields(f"{path}[]", was["items"], now.get("items") or {})
        elif was.get("items") != now.get("items"):
            problems.append(f"{path}: items {was.get('items')!r} -> {now.get('items')!r}")
    for name, now in new.items():
        if name not in old and now["required"]:
            problems.append(f"{where}.{name}: new required field")
    return problems


def breaking(old: Contract, new: Contract) -> list[str]:
    """Every change from ``old`` to ``new`` that is not additive (AR-Q18); a new event type is additive."""
    problems: list[str] = []
    for event, fields in old.items():
        if event not in new:
            problems.append(f"{event}: event type removed")
        else:
            problems += _compare_fields(event, fields, new[event])
    return problems


def differences(a: Contract, b: Contract) -> list[str]:
    """Anything that differs, both ways: new types and new optional fields included."""
    found = breaking(a, b) + breaking(b, a)
    found += [f"{event}: only on one side" for event in set(a) ^ set(b)]
    for event in set(a) & set(b):
        found += [f"{event}.{name}: only on one side" for name in set(a[event]) ^ set(b[event])]
    return sorted(set(found))


def load_snapshot() -> Contract:
    return json.loads(SNAPSHOT.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- tests


class TestAdditiveOnly:
    def test_schema_matches_snapshot_additively(self) -> None:
        current = contract()
        problems = breaking(load_snapshot(), current)
        assert not problems, "breaking change of stream schema v = 1:\n" + "\n".join(problems)
        if os.environ.get("UPDATE_STREAM_CONTRACT") == "1":
            SNAPSHOT.write_text(json.dumps(current, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def test_snapshot_is_the_current_schema(self) -> None:
        """New fields and types land in the snapshot in the same change (``UPDATE_STREAM_CONTRACT=1``)."""
        assert differences(load_snapshot(), contract()) == []


def mutated(change: Any) -> Contract:
    snapshot = copy.deepcopy(load_snapshot())
    change(snapshot)
    return snapshot


class TestComparator:
    """The comparator catches each breaking change of the spec and lets additive ones through."""

    @pytest.mark.parametrize(
        ("change", "expected"),
        [
            (lambda c: c["tool.finished"].pop("label"), "tool.finished.label: removed"),
            (lambda c: c.update({"tool.ended": c.pop("tool.finished")}), "tool.finished: event type removed"),
            (lambda c: c["tool.finished"]["summary"].update(required=True), "tool.finished.summary: required"),
            (lambda c: c["tool.started"]["call"].update(type="string"), "tool.started.call: type"),
            (lambda c: c["phase"]["phase"]["enum"].append("preparing"), "phase.phase: enum"),
            (lambda c: c["answer"]["outcome"]["enum"].remove("clarify"), "answer.outcome: enum"),
            (lambda c: c["plan"]["steps"]["items"].pop("reason"), "plan.steps[].reason: removed"),
            (
                lambda c: c["summary"].update(total={"type": "integer", "nullable": False, "required": True}),
                "summary.total: new required field",
            ),
        ],
    )
    def test_breaking(self, change: Any, expected: str) -> None:
        problems = breaking(load_snapshot(), mutated(change))
        assert any(p.startswith(expected) for p in problems), problems

    @pytest.mark.parametrize(
        "change",
        [
            lambda c: c["tool.finished"].update(unit={"type": "string", "nullable": True, "required": False}),
            lambda c: c.update({"note": {"text": {"type": "string", "nullable": False, "required": True}}}),
        ],
        ids=["new optional field", "new event type"],
    )
    def test_additive(self, change: Any) -> None:
        assert breaking(load_snapshot(), mutated(change)) == []


def openapi_document() -> dict[str, Any]:
    return yaml.safe_load(OPENAPI.read_text(encoding="utf-8"))


class TestOpenApi:
    def test_openapi_matches_the_schema(self) -> None:
        problems = differences(openapi_contract(openapi_document()), contract())
        assert not problems, "openapi_copilot.yaml differs from api/v2/stream/events.py:\n" + "\n".join(problems)

    def test_every_event_type_is_described(self) -> None:
        assert set(openapi_contract(openapi_document())) == set(EVENT_TYPES)

    def test_missing_field_is_caught(self) -> None:
        document = openapi_document()
        own = document["components"]["schemas"]["StreamEventToolFinished"]["allOf"][1]
        del own["properties"]["summary"]
        problems = differences(openapi_contract(document), contract())
        assert any(p.startswith("tool.finished.summary") for p in problems), problems

    def test_extra_enum_value_is_caught(self) -> None:
        document = openapi_document()
        own = document["components"]["schemas"]["StreamEventPhase"]["allOf"][1]
        own["properties"]["phase"]["enum"].append("preparing")
        problems = differences(openapi_contract(document), contract())
        assert any(p.startswith("phase.phase: enum") for p in problems), problems
