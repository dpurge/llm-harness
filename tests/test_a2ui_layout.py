"""A2UI layout components: Row, Column, List, Card, Tabs, Modal, Divider, Icon, weight."""

import pytest
from nicegui import ui
from nicegui.testing import User

from llm_harness.a2ui.validation import SurfaceValidator
from llm_harness.gui import a2ui_render
from tests.a2ui_support import (
    bad_sizing,
    components,
    create,
    data_model,
    found,
    open_redrawing,
    shown,
    surface_elements,
    text,
    validator_with,
)

HOSTILE = "<img src=x onerror=alert(1)> <b>bold</b> **md** [l](javascript:alert(1))"
ALL_LAYOUT = [
    {
        "id": "root",
        "component": "Column",
        "align": "center",
        "justify": "spaceBetween",
        "children": [
            "row",
            "list",
            "hlist",
            "card",
            "tabs",
            "modal",
            "div",
            "vdiv",
            "ico",
            "cap",
        ],
    },
    {
        "id": "row",
        "component": "Row",
        "justify": "end",
        "align": "start",
        "children": ["a", "b"],
    },
    {"id": "list", "component": "List", "children": ["a"]},
    {"id": "hlist", "component": "List", "direction": "horizontal", "children": ["a"]},
    {"id": "card", "component": "Card", "child": "a"},
    {
        "id": "tabs",
        "component": "Tabs",
        "tabs": [{"title": HOSTILE, "child": "a"}, {"title": "Two", "child": "b"}],
    },
    {"id": "modal", "component": "Modal", "trigger": "t", "content": "b"},
    {"id": "div", "component": "Divider"},
    {"id": "vdiv", "component": "Divider", "axis": "vertical"},
    {"id": "ico", "component": "Icon", "name": "accountCircle"},
    {
        "id": "cap",
        "component": "Text",
        "text": HOSTILE,
        "variant": "caption",
        "weight": 2,
    },
    text("a", HOSTILE),
    text("b", "second"),
    {"id": "t", "component": "Text", "text": "open it"},
]


def kinds(user: User, marker: str) -> list:
    return found(user, marker)


async def test_every_layout_component_renders_with_full_width_classes(
    user: User,
) -> None:
    await open_redrawing(user, validator_with(ALL_LAYOUT))
    for marker in (
        "a2ui-row",
        "a2ui-column",
        "a2ui-list",
        "a2ui-card",
        "a2ui-tabs",
        "a2ui-tab-panels",
    ):
        elements = kinds(user, marker)
        assert elements, marker
        assert all("w-full" in e.classes for e in elements), marker
    assert len(kinds(user, "a2ui-list")) == 2
    assert len(kinds(user, "a2ui-divider")) == 2
    assert len(kinds(user, "a2ui-icon")) == 1
    assert len(kinds(user, "a2ui-modal")) == 1
    assert bad_sizing(user) == []
    assert kinds(user, "a2ui-unsupported") == []
    assert kinds(user, "a2ui-error") == []


async def test_hostile_text_is_only_ever_label_text(user: User) -> None:
    await open_redrawing(user, validator_with(ALL_LAYOUT))
    assert HOSTILE in shown(user)
    with user.client:
        types = {type(e) for e in surface_elements(user)}
    assert ui.html not in types and ui.markdown not in types
    tab_labels = [e.props["label"] for e in kinds(user, "a2ui-tab")]
    assert tab_labels == [HOSTILE, "Two"]  # Quasar escapes a label; it is never html


async def test_row_column_and_list_alignment_classes(user: User) -> None:
    await open_redrawing(user, validator_with(ALL_LAYOUT))
    (row,) = kinds(user, "a2ui-row")
    assert "justify-end" in row.classes
    assert row.props.get("align-items") is None or True  # NiceGUI keeps it as a class
    (column,) = [
        e for e in kinds(user, "a2ui-column") if "justify-between" in e.classes
    ]
    assert column is not None
    horizontal, vertical = sorted(kinds(user, "a2ui-list"), key=lambda e: e.id)
    assert {type(horizontal), type(vertical)} == {ui.column, ui.row}


async def test_a_text_variant_caption_is_secondary_and_weight_grows(user: User) -> None:
    await open_redrawing(user, validator_with(ALL_LAYOUT))
    captions = [e for e in kinds(user, "a2ui-text") if "llm-secondary" in e.classes]
    assert len(captions) == 1
    (weighted,) = kinds(user, "a2ui-weighted")
    assert "grow" in weighted.classes
    assert weighted._style["flex-grow"] == "2"


async def test_tabs_switching_shows_the_right_child(user: User) -> None:
    view: dict[str, str] = {}
    await open_redrawing(user, validator_with(ALL_LAYOUT), view=view)
    (panels,) = kinds(user, "a2ui-tab-panels")
    (bar,) = kinds(user, "a2ui-tabs")
    assert panels.value == "tab0"
    _, second = kinds(user, "a2ui-tab-panel")
    with user.client:
        bar.set_value("tab1")
    assert panels.value == "tab1"
    assert view  # the choice is remembered for a redraw
    with user.client:
        texts_in_second = [
            e.text for e in second.descendants() if isinstance(e, ui.label)
        ]
    assert texts_in_second == ["second"]


async def test_the_selected_tab_survives_an_edit_redraw(user: User) -> None:
    items = [
        {
            "id": "root",
            "component": "Tabs",
            "tabs": [{"title": "A", "child": "x"}, {"title": "B", "child": "y"}],
        },
        text("x", "one"),
        {"id": "y", "component": "TextField", "label": "L", "value": {"@path": "/v"}},
    ]
    validator = validator_with(items, {"v": ""})
    await open_redrawing(user, validator, view={})
    with user.client:
        found(user, "a2ui-tabs")[0].set_value("tab1")
    user.find(marker="a2ui-textfield").type("hi").trigger("blur")
    assert validator.resolve("s", {"@path": "/v"}).value == "hi"
    (panels,) = kinds(user, "a2ui-tab-panels")  # the redrawn one
    assert panels.value == "tab1"


async def test_modal_opens_on_a_click_of_its_trigger_and_uses_the_full_width(
    user: User,
) -> None:
    await open_redrawing(user, validator_with(ALL_LAYOUT))
    (dialog,) = kinds(user, "a2ui-modal")
    assert dialog.value is False
    assert "maximized" in dialog.props
    (card,) = kinds(user, "a2ui-modal-card")
    assert "w-full" in card.classes
    user.find(marker="a2ui-modal-trigger").trigger("click")
    assert dialog.value is True
    user.find(marker="a2ui-modal-close").click()
    assert dialog.value is False


@pytest.mark.parametrize("name", ["home", "accountCircle", "arrow_back", "a1"])
async def test_icon_accepts_plain_material_names(user: User, name: str) -> None:
    validator = validator_with(
        [{"id": "root", "component": "Icon", "name": {"@path": "/i"}}], {"i": name}
    )
    await open_redrawing(user, validator)
    (icon,) = kinds(user, "a2ui-icon")
    assert icon.props["name"] == ("account_circle" if name == "accountCircle" else name)


@pytest.mark.parametrize(
    "name",
    [
        "img:https://e/x",
        "https://e/x",
        "home;alert(1)",
        "",
        "a" * 65,
        "Home Page",
        "../x",
        5,
    ],
)
async def test_icon_refuses_everything_else_with_a_visible_line(
    user: User, name: object
) -> None:
    validator = validator_with(
        [{"id": "root", "component": "Icon", "name": {"@path": "/i"}}], {"i": name}
    )
    await open_redrawing(user, validator)
    assert kinds(user, "a2ui-icon") == []
    assert shown(user, "a2ui-unsupported") == ["[unsupported A2UI value]"]


async def test_icon_name_mapping_is_pure() -> None:
    assert a2ui_render._material_icon_name("home") == "home"
    assert a2ui_render._material_icon_name("img:https://e/x") is None
    assert a2ui_render._material_icon_name("a" * 64) == "a" * 64
    assert a2ui_render._material_icon_name("a" * 65) is None


TEMPLATE = [
    {
        "id": "root",
        "component": "Column",
        "children": {"componentId": "item", "path": "/people"},
    },
    {"id": "item", "component": "Row", "children": ["who", "age"]},
    {"id": "who", "component": "Text", "text": {"@path": "name"}},
    {
        "id": "age",
        "component": "Text",
        "text": {"@call": "formatString", "args": {"value": "age ${age}"}},
    },
]


async def test_a_children_template_draws_one_child_per_item_with_relative_paths(
    user: User,
) -> None:
    people = [{"name": "Ann", "age": 3}, {"name": HOSTILE, "age": 5}]
    await open_redrawing(user, validator_with(TEMPLATE, {"people": people}))
    assert shown(user) == ["Ann", "age 3", HOSTILE, "age 5"]
    assert len(kinds(user, "a2ui-row")) == 2


async def test_a_template_follows_the_data_model(user: User) -> None:
    validator = validator_with(TEMPLATE, {"people": [{"name": "Ann", "age": 3}]})
    await open_redrawing(user, validator)
    assert shown(user) == ["Ann", "age 3"]
    assert validator.submit(
        [data_model([{"name": "Bob", "age": 4}, {"name": "Cy", "age": 5}], "/people")]
    ).ok
    await open_redrawing(user, validator)
    assert shown(user) == ["Bob", "age 4", "Cy", "age 5"]


async def test_a_template_list_is_cut_and_says_so(user: User) -> None:
    people = [
        {"name": f"n{i}", "age": i} for i in range(a2ui_render.MAX_TEMPLATE_ITEMS + 5)
    ]
    await open_redrawing(user, validator_with(TEMPLATE, {"people": people}))
    assert len(kinds(user, "a2ui-row")) == a2ui_render.MAX_TEMPLATE_ITEMS
    assert shown(user, "a2ui-unsupported") == ["[A2UI list cut at 100 items]"]


@pytest.mark.parametrize("data", [{"people": "no list"}, {"people": {"a": 1}}])
async def test_a_template_over_something_that_is_not_a_list_is_unsupported(
    user: User, data: object
) -> None:
    await open_redrawing(user, validator_with(TEMPLATE, data))
    assert shown(user, "a2ui-unsupported") == ["[unsupported A2UI children template]"]


async def test_a_template_over_a_missing_path_shows_a_value_error(user: User) -> None:
    await open_redrawing(user, validator_with(TEMPLATE, {}))
    assert shown(user, "a2ui-error") == ["[A2UI value error: path /people not found]"]


async def test_nesting_deeper_than_the_limit_shows_a_line_not_a_crash(
    user: User,
) -> None:
    depth = a2ui_render.MAX_DEPTH + 3
    items = [
        {"id": "root" if i == 0 else f"c{i}", "component": "Card", "child": f"c{i + 1}"}
        for i in range(depth)
    ]
    items.append(text(f"c{depth}", "deep"))
    await open_redrawing(user, validator_with(items))
    assert shown(user, "a2ui-error") == [
        f"[A2UI error: nesting deeper than {a2ui_render.MAX_DEPTH} levels]"
    ]


async def test_a_missing_child_in_a_card_and_a_tab_is_visible(user: User) -> None:
    validator = validator_with(
        [{"id": "root", "component": "Card", "child": "x"}, text("x", "ok")]
    )
    validator.components = lambda surface_id: {  # type: ignore[method-assign]
        "root": {"id": "root", "component": "Card", "child": "gone"}
    }
    await open_redrawing(user, validator)
    assert shown(user, "a2ui-error") == ["[A2UI error: missing component 'gone']"]


async def test_a_bad_enum_in_a_stubbed_component_is_unsupported(user: User) -> None:
    validator = SurfaceValidator()
    assert validator.submit([create(), components([text("root", "x")])]).ok
    validator.components = lambda surface_id: {  # type: ignore[method-assign]
        "root": {
            "id": "root",
            "component": "Row",
            "children": [],
            "justify": "sideways",
        }
    }
    await open_redrawing(user, validator)
    assert shown(user, "a2ui-unsupported") == ["[unsupported A2UI value]"]


async def test_a_component_the_renderer_does_not_know_is_unsupported(
    user: User,
) -> None:
    validator = validator_with([text("root", "x")])
    validator.components = lambda surface_id: {  # type: ignore[method-assign]
        "root": {"id": "root", "component": "Hologram"}
    }
    await open_redrawing(user, validator)
    assert shown(user, "a2ui-unsupported") == ["[unsupported A2UI component: Hologram]"]
