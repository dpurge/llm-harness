"""The reply schema pins `state_patch`; offline.

`jsonschema` is not installed (and not a dependency), so `conforms` below is a
small validator for exactly the keywords `reply_schema()` uses.
"""

import re

import pytest

from llm_harness.state import (
    PatchError,
    apply_patch,
    reply_schema,
)
from tests.test_state import FINDING, make_state

STEP = {"text": "t", "status": "todo"}

# Accepted by apply_patch (happy paths from test_state.py).
ACCEPTED = [
    {},
    {"goal": "g"},
    {"goal": None},
    {"findings": None},
    {"plan": None},
    {"findings": {"$append": [FINDING]}},
    {"findings": {"$replace": []}},
    {"plan": {"$append": [STEP]}},
    {"goal": "g", "findings": {"$replace": [FINDING]}, "plan": {"$replace": [STEP]}},
]

# Rejected by apply_patch for shape; the schema must reject them too.
REJECTED = [
    {"goal": {"$replace": "x"}},
    {"goal": ["a"]},
    {"findings": [FINDING]},
    {"plan": [STEP]},
    {"findings": {"$append": [FINDING], "$replace": [FINDING]}},
    {"findings": {"$extend": [FINDING]}},
    {"findings": {"$append": [{"text": "t"}]}},
    {"findings": {"$append": [{"text": "t", "source": "   "}]}},
    {"findings": {"$append": [{"text": "   ", "source": "s"}]}},
    {"plan": {"$append": [{"text": "t", "status": "bogus"}]}},
    {"turn": 3},
    {"environment": {}},
]


def _resolve(root: dict, ref: str) -> dict:
    assert ref.startswith("#/")
    node = root
    for part in ref[2:].split("/"):
        node = node[part]
    return node


def conforms(root: dict, schema: dict, value: object) -> bool:
    if "$ref" in schema:
        return conforms(root, _resolve(root, schema["$ref"]), value)
    if "anyOf" in schema:
        return any(conforms(root, option, value) for option in schema["anyOf"])
    if "oneOf" in schema:
        discriminator = schema.get("discriminator")
        if discriminator:
            if not isinstance(value, dict):
                return False
            target = discriminator["mapping"].get(
                value.get(discriminator["propertyName"])
            )
            return target is not None and conforms(root, {"$ref": target}, value)
        return sum(conforms(root, option, value) for option in schema["oneOf"]) == 1
    if "const" in schema:
        return value == schema["const"]
    if "enum" in schema:
        return value in schema["enum"]
    kind = schema.get("type")
    if kind == "null":
        return value is None
    if kind == "string":
        return (
            isinstance(value, str)
            and len(value) >= schema.get("minLength", 0)
            and re.search(schema.get("pattern", ""), value) is not None
        )
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "array":
        return isinstance(value, list) and all(
            conforms(root, schema.get("items", {}), item) for item in value
        )
    if kind == "object":
        if not isinstance(value, dict):
            return False
        properties = schema.get("properties", {})
        if any(key not in value for key in schema.get("required", [])):
            return False
        if schema.get("additionalProperties") is False and set(value) - set(properties):
            return False
        return all(
            conforms(root, properties[key], item)
            for key, item in value.items()
            if key in properties
        )
    return True


def _refs(node: object):
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "$ref":
                yield value
            else:
                yield from _refs(value)
    elif isinstance(node, list):
        for item in node:
            yield from _refs(item)


def _patch_schema() -> dict:
    return reply_schema()["properties"]["state_patch"]


def test_schema_is_cached_single_object() -> None:
    assert reply_schema() is reply_schema()


def test_every_ref_resolves() -> None:
    schema = reply_schema()
    refs = set(_refs(schema))
    assert {"#/$defs/Finding", "#/$defs/PlanStep"} <= refs
    for ref in refs:
        assert _resolve(schema, ref)  # KeyError when the target is missing


def test_state_patch_is_closed_and_pins_fields() -> None:
    patch = _patch_schema()
    assert patch["type"] == "object"
    assert patch["additionalProperties"] is False
    assert set(patch["properties"]) == {"goal", "findings", "plan"}


def test_goal_allows_string_or_null_only() -> None:
    assert _patch_schema()["properties"]["goal"] == {
        "anyOf": [{"type": "string"}, {"type": "null"}]
    }


@pytest.mark.parametrize(
    ("key", "item"), [("findings", "Finding"), ("plan", "PlanStep")]
)
def test_list_fields_allow_null_or_exactly_one_form(key: str, item: str) -> None:
    options = _patch_schema()["properties"][key]["anyOf"]
    assert options[0] == {"type": "null"}
    forms = [(next(iter(o["properties"])), o) for o in options[1:]]
    assert [name for name, _ in forms] == ["$append", "$replace"]
    for name, option in forms:
        assert list(option["properties"]) == [name]
        assert option["required"] == [name]
        assert option["additionalProperties"] is False
        assert option["properties"][name]["items"] == {"$ref": f"#/$defs/{item}"}


def test_action_part_of_reply_is_kept() -> None:
    schema = reply_schema()
    assert schema["required"] == ["state_patch", "action"]
    assert "action" in schema["properties"]


@pytest.mark.parametrize("patch", ACCEPTED)
def test_patches_the_parser_accepts_conform(patch: dict) -> None:
    apply_patch(make_state(), patch)
    assert conforms(reply_schema(), _patch_schema(), patch)


@pytest.mark.parametrize("patch", REJECTED)
def test_patches_the_parser_rejects_do_not_conform(patch: dict) -> None:
    with pytest.raises(PatchError):
        apply_patch(make_state(), patch)
    assert not conforms(reply_schema(), _patch_schema(), patch)


def test_whole_reply_example_conforms() -> None:
    reply = {
        "state_patch": {"goal": "g", "findings": {"$append": [FINDING]}},
        "action": {"type": "respond", "text": "hi"},
    }
    assert conforms(reply_schema(), reply_schema(), reply)


def test_action_schema_is_a_discriminated_union_the_validator_understands() -> None:
    action = reply_schema()["properties"]["action"]
    assert "oneOf" in action
    assert set(action["discriminator"]["mapping"]) == {
        "respond",
        "ask_user",
        "call_tool",
        "delegate",
    }


@pytest.mark.parametrize(
    "action",
    [
        {"type": "dance", "text": "x"},
        {"type": "respond", "text": "x", "extra": 1},
        {"type": "respond"},
        {"text": "x"},
        {"type": "ask_user", "text": "x"},
    ],
)
def test_whole_reply_with_a_bad_action_does_not_conform(action: dict) -> None:
    reply = {"state_patch": {}, "action": action}
    assert not conforms(reply_schema(), reply_schema(), reply)
