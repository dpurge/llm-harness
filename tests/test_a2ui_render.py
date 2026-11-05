"""Tests for the NiceGUI renderer of validated A2UI v1.0 surfaces."""

import re

import pytest
from nicegui import ElementFilter, ui
from nicegui.testing import User

from llm_harness.a2ui.demo import DEMO_MESSAGES, DEMO_SURFACE_ID
from llm_harness.a2ui.validation import SurfaceValidator
from llm_harness.display import sanitize_text
from llm_harness.gui.a2ui_render import render_surface

CATALOG_ID = "https://a2ui.org/specification/v1_0/catalogs/basic/catalog.json"
FORBIDDEN_CLASS = re.compile(r"(^|:)max-w-|(^|:)mx-auto|(^|:)h-(\d|\[|px)|\d+px")


def _surface(components: list[dict]) -> SurfaceValidator:
    validator = SurfaceValidator()
    outcome = validator.submit(
        [
            {
                "version": "1.0",
                "createSurface": {
                    "surfaceId": "s",
                    "catalogId": CATALOG_ID,
                    "components": components,
                },
            }
        ]
    )
    assert outcome.ok, outcome.errors
    return validator


def _text(component_id: str, text: str) -> dict:
    return {"id": component_id, "component": "Text", "text": text}


async def _open(
    user: User, validator: SurfaceValidator, surface_id: str = "s"
) -> list[tuple[str, str]]:
    actions: list[tuple[str, str]] = []

    @ui.page("/a2ui-render-test")
    def page() -> None:
        with ui.column().classes("w-full"):
            render_surface(
                validator, surface_id, lambda s, a, c: actions.append((s, a))
            )

    await user.open("/a2ui-render-test")
    return actions


def _elements(user: User, marker: str) -> list:
    with user.client:
        return list(ElementFilter(marker=marker, local_scope=False))


def _all_elements(user: User) -> list:
    return list(user.client.elements.values())


async def test_the_demo_surface_draws_two_texts_a_column_and_a_button(
    user: User,
) -> None:
    validator = SurfaceValidator()
    assert validator.submit(DEMO_MESSAGES).ok
    actions = await _open(user, validator, DEMO_SURFACE_ID)
    assert [e.text for e in _elements(user, "a2ui-text")] == [
        "A2UI demo surface",
        "This surface is drawn from fixed A2UI v1.0 messages; no model was called.",
    ]
    assert len(_elements(user, "a2ui-column")) == 1
    (button,) = _elements(user, "a2ui-button")
    assert button.text == "Click me"
    user.find(marker="a2ui-button").click()
    assert actions == [("demo", "demo_clicked")]


async def test_an_unsupported_component_shows_a_visible_line(user: User) -> None:
    validator = _surface(
        [
            {"id": "root", "component": "Column", "children": ["a", "d"]},
            _text("a", "before"),
            _text("d", "stub"),
        ]
    )
    # The validator rejects unknown components, so the renderer's own fallback is
    # reached through a stubbed component list.
    known = validator.components
    validator.components = lambda surface_id: {  # type: ignore[method-assign]
        **known(surface_id),
        "d": {"id": "d", "component": "Hologram"},
    }
    await _open(user, validator)
    assert [e.text for e in _elements(user, "a2ui-unsupported")] == [
        "[unsupported A2UI component: Hologram]"
    ]
    assert [e.text for e in _elements(user, "a2ui-text")] == ["before"]


async def test_a_data_binding_shows_the_unsupported_value_line(user: User) -> None:
    validator = _surface(
        [{"id": "root", "component": "Text", "text": {"path": "/name"}}]
    )
    await _open(user, validator)
    assert [e.text for e in _elements(user, "a2ui-unsupported")] == [
        "[unsupported A2UI value]"
    ]
    assert _elements(user, "a2ui-text") == []


async def test_a_button_with_a_function_call_action_is_unsupported(
    user: User,
) -> None:
    validator = _surface(
        [
            {"id": "root", "component": "Column", "children": ["b", "l"]},
            {
                "id": "b",
                "component": "Button",
                "child": "l",
                "action": {"functionCall": {"call": "openUrl", "args": {"url": "x"}}},
            },
            _text("l", "go"),
        ]
    )
    await _open(user, validator)
    assert [e.text for e in _elements(user, "a2ui-unsupported")] == [
        "[unsupported A2UI value]"
    ]
    assert _elements(user, "a2ui-button") == []


@pytest.mark.parametrize(
    "hostile",
    [
        "<script>alert(1)</script>",
        "<img src=x onerror=alert(1)>",
        "\x1b[31mred" + chr(0x202E) + "txt",
    ],
)
async def test_hostile_text_is_a_sanitized_label_and_never_html(
    user: User, hostile: str
) -> None:
    validator = _surface([_text("root", hostile)])
    await _open(user, validator)
    (label,) = _elements(user, "a2ui-text")
    assert isinstance(label, ui.label)
    assert label.text == sanitize_text(hostile)
    assert not [e for e in _all_elements(user) if isinstance(e, ui.html)]
    assert not [e for e in _all_elements(user) if isinstance(e, ui.markdown)]


async def test_a_hostile_button_label_and_action_name_are_sanitized(
    user: User,
) -> None:
    validator = _surface(
        [
            {
                "id": "root",
                "component": "Button",
                "child": "l",
                "action": {"event": {"name": "go\x1b[0m", "context": {}}},
            },
            _text("l", "<b>x</b>\x07"),
        ]
    )
    actions = await _open(user, validator)
    (button,) = _elements(user, "a2ui-button")
    assert button.text == "<b>x</b>"
    user.find(marker="a2ui-button").click()
    assert actions == [("s", "go[0m")]


async def test_a_reference_cycle_shows_an_error_line_and_stops(user: User) -> None:
    validator = _surface([_text("root", "x")])
    # The validator rejects cycles; give the renderer a cyclic view directly.
    validator.components = lambda surface_id: {  # type: ignore[method-assign]
        "root": {"id": "root", "component": "Column", "children": ["a"]},
        "a": {"id": "a", "component": "Column", "children": ["b"]},
        "b": {"id": "b", "component": "Column", "children": ["a"]},
    }
    await _open(user, validator)
    assert [e.text for e in _elements(user, "a2ui-error")] == [
        "[A2UI error: cycle at component 'a']"
    ]


async def test_a_missing_child_shows_an_error_line(user: User) -> None:
    validator = _surface([_text("root", "x")])
    validator.components = lambda surface_id: {  # type: ignore[method-assign]
        "root": {"id": "root", "component": "Column", "children": ["gone"]},
    }
    await _open(user, validator)
    assert [e.text for e in _elements(user, "a2ui-error")] == [
        "[A2UI error: missing component 'gone']"
    ]


async def test_an_unknown_surface_shows_an_error_line(user: User) -> None:
    await _open(user, SurfaceValidator(), "nope")
    assert [e.text for e in _elements(user, "a2ui-error")] == [
        "[A2UI error: surface 'nope' has no root]"
    ]


async def test_the_rendered_elements_are_full_width_with_no_fixed_sizes(
    user: User,
) -> None:
    validator = SurfaceValidator()
    assert validator.submit(DEMO_MESSAGES).ok
    await _open(user, validator, DEMO_SURFACE_ID)
    markers = ("a2ui-column", "a2ui-text", "a2ui-button")
    rendered = [e for m in markers for e in _elements(user, m)]
    assert len(rendered) == 4
    (column,) = _elements(user, "a2ui-column")
    assert "w-full" in column.classes
    for text in _elements(user, "a2ui-text"):
        assert "w-full" in text.classes
    for element in rendered:
        for css_class in element.classes:
            assert not FORBIDDEN_CLASS.search(css_class), css_class
        assert "height" not in element.style and "width" not in element.style
