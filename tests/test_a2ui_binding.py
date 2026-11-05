"""Data binding: values resolve against the CURRENT data model of a surface."""

from nicegui import ui
from nicegui.testing import User

from llm_harness.a2ui.validation import SurfaceValidator
from tests.a2ui_support import (
    CATALOG_ID,
    DATA,
    FORBIDDEN_CLASS,
    call,
    components,
    create,
    data_model,
    found,
    open_surface,
    shown,
    text,
    text_validator,
    validator_with,
)


def bound(path: str) -> dict[str, str]:
    return {"@path": path}


def test_literals_and_absolute_paths_resolve() -> None:
    validator = text_validator("x")
    assert validator.resolve("s", "lit").value == "lit"
    assert validator.resolve("s", bound("/name")).value == "Ann"
    assert validator.resolve("s", bound("/user/first")).value == "Zed"
    assert validator.resolve("s", bound("/n")).value == 3


def test_a_relative_path_resolves_against_the_root_scope() -> None:
    # a2ui-core DataContext: a path without a leading slash is joined to "/".
    assert text_validator("x").resolve("s", bound("name")).value == "Ann"


def test_nested_values_are_resolved_recursively() -> None:
    result = text_validator("x").resolve("s", {"a": bound("/n"), "b": [bound("/name")]})
    assert result.ok
    assert result.value == {"a": 3, "b": ["Ann"]}


def test_a_missing_path_is_an_error_not_a_silent_none() -> None:
    result = text_validator("x").resolve("s", bound("/nope"))
    assert not result.ok
    assert result.error == "path /nope not found"


def test_an_unknown_surface_and_unknown_reserved_key_do_not_raise() -> None:
    validator = text_validator("x")
    assert validator.resolve("other", "x").error == "unknown surface"
    result = validator.resolve("s", {"@foo": 1})
    assert not result.ok
    assert "@foo" in result.error


def test_an_error_reason_is_one_short_line() -> None:
    result = text_validator("x").resolve("s", {"@foo": "x" * 1000})
    assert len(result.error) <= 200
    assert "\n" not in result.error


def test_the_processor_adopted_by_submit_holds_the_current_state() -> None:
    validator = validator_with(
        [
            {"id": "root", "component": "Column", "children": ["a"]},
            text("a", bound("/name")),
        ]
    )
    assert validator.resolve("s", bound("/name")).value == "Ann"
    assert validator.submit([data_model("Bob", "/name")]).ok
    assert validator.resolve("s", bound("/name")).value == "Bob"
    grown = [
        {"id": "root", "component": "Column", "children": ["a", "extra"]},
        text("extra", "more"),
    ]
    assert validator.submit([components(grown)]).ok
    assert list(validator.components("s")) == ["root", "a", "extra"]
    assert validator.submit(
        [{"version": "1.0", "deleteSurface": {"surfaceId": "s"}}]
    ).ok
    assert validator.surface_ids == ()
    assert validator.components("s") == {}
    assert validator.resolve("s", "x").error == "unknown surface"


def test_submit_does_not_change_the_callers_messages() -> None:
    value = {"name": "Ann"}
    validator = SurfaceValidator()
    assert validator.submit(
        [create(), data_model(value), data_model("Bob", "/name")]
    ).ok
    assert value == {"name": "Ann"}
    assert validator.resolve("s", bound("/name")).value == "Bob"


def test_a_rejected_batch_leaves_the_data_model_unchanged() -> None:
    validator = text_validator(bound("/name"))
    bad = {"version": "0.9", "updateDataModel": {"surfaceId": "s", "value": {}}}
    assert not validator.submit([data_model("Bob", "/name"), bad]).ok
    assert validator.resolve("s", bound("/name")).value == "Ann"


async def test_a_bound_text_shows_the_value(user: User) -> None:
    await open_surface(user, text_validator(bound("/name")))
    assert shown(user) == ["Ann"]
    assert [e.__class__ for e in found(user, "a2ui-text")] == [ui.label]


async def test_a_number_and_a_boolean_show_as_text(user: User) -> None:
    validator = text_validator(bound("/n"))
    await open_surface(user, validator)
    assert shown(user) == ["3"]


async def test_an_unresolved_path_shows_the_error_line(user: User) -> None:
    await open_surface(user, text_validator(bound("/missing")))
    assert shown(user, "a2ui-error") == ["[A2UI value error: path /missing not found]"]
    assert shown(user) == []


async def test_a_failing_function_shows_the_error_line(user: User) -> None:
    await open_surface(user, text_validator(call("regex", value="a", pattern="(")))
    assert shown(user, "a2ui-error") == [
        "[A2UI value error: invalid regex pattern: missing ) at position 1]"
    ]


async def test_an_object_value_is_unsupported_not_dumped(user: User) -> None:
    # v0.9 wording `{"path": ...}` is a plain object in v1.0, not a binding.
    await open_surface(user, text_validator({"path": "/name"}))
    assert shown(user, "a2ui-unsupported") == ["[unsupported A2UI value]"]
    assert shown(user) == []


async def test_open_url_as_a_text_value_is_an_error(user: User) -> None:
    await open_surface(user, text_validator(call("openUrl", url="https://example.org")))
    assert shown(user, "a2ui-error") == [
        "[A2UI value error: openUrl is only allowed as a button action]"
    ]


async def test_hostile_resolved_text_is_shown_as_text(user: User) -> None:
    hostile = "<script>alert(1)</script><img src=x onerror=alert(1)>\x1b[31m"
    validator = text_validator(bound("/name"), {"name": hostile})
    await open_surface(user, validator)
    (label,) = found(user, "a2ui-text")
    assert isinstance(label, ui.label)
    assert label.text == "<script>alert(1)</script><img src=x onerror=alert(1)>[31m"
    assert "\x1b" not in label.text
    assert found(user, "a2ui-error") == []


async def test_hostile_error_reasons_are_sanitized(user: User) -> None:
    validator = text_validator(bound("/evil\x1b[31m<b>"))
    await open_surface(user, validator)
    (line,) = shown(user, "a2ui-error")
    assert "\x1b" not in line
    assert line.startswith("[A2UI value error: ")


async def test_value_lines_are_full_width_with_no_fixed_sizes(user: User) -> None:
    validator = SurfaceValidator()
    assert validator.submit(
        [
            create(),
            data_model(DATA),
            components(
                [
                    {"id": "root", "component": "Column", "children": ["a", "b"]},
                    text("a", bound("/name")),
                    text("b", bound("/missing")),
                ]
            ),
        ]
    ).ok
    await open_surface(user, validator)
    elements = [*found(user, "a2ui-text"), *found(user, "a2ui-error")]
    assert len(elements) == 2
    for element in elements:
        assert "w-full" in element.classes
        assert not [c for c in element.classes if FORBIDDEN_CLASS.search(c)]


def test_the_catalog_id_constant_matches_the_library() -> None:
    from a2ui.core.basic_catalog import v1_0

    assert CATALOG_ID == v1_0.BasicCatalog().catalog_id
