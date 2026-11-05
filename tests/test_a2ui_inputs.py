"""A2UI input components: two-way binding, edit limits and the replay point."""

import pytest
from nicegui import ui
from nicegui.testing import User

from llm_harness.a2ui.validation import SurfaceValidator
from llm_harness.gui.a2ui_inputs import MAX_CHOICES, MAX_VALUE_CHARS
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

HOSTILE = "<img src=x onerror=alert(1)> <b>bold</b>"
OPTIONS = [
    {"label": "Red", "value": "r"},
    {"label": HOSTILE, "value": "g"},
    {"label": "Blue", "value": "b"},
]
FORM = [
    {
        "id": "root",
        "component": "Column",
        "children": ["name", "mirror", "agree", "pick", "many", "level", "when"],
    },
    {
        "id": "name",
        "component": "TextField",
        "label": HOSTILE,
        "placeholder": HOSTILE,
        "value": {"@path": "/form/name"},
    },
    text("mirror", {"@path": "/form/name"}),
    {
        "id": "agree",
        "component": "CheckBox",
        "label": HOSTILE,
        "value": {"@path": "/form/agree"},
    },
    {
        "id": "pick",
        "component": "ChoicePicker",
        "label": "One",
        "options": OPTIONS,
        "value": {"@path": "/form/one"},
    },
    {
        "id": "many",
        "component": "ChoicePicker",
        "variant": "multipleSelection",
        "displayStyle": "chips",
        "filterable": True,
        "options": OPTIONS,
        "value": {"@path": "/form/many"},
    },
    {
        "id": "level",
        "component": "Slider",
        "label": "Level",
        "min": 0,
        "max": 10,
        "steps": 10,
        "value": {"@path": "/form/level"},
    },
    {
        "id": "when",
        "component": "DateTimeInput",
        "label": "When",
        "enableDate": True,
        "value": {"@path": "/form/when"},
    },
]
DATA = {
    "form": {
        "name": "Ann",
        "agree": False,
        "one": [],
        "many": [],
        "level": 2,
        "when": "",
    }
}


def model(validator: SurfaceValidator, path: str) -> object:
    return validator.resolve("s", {"@path": path}).value


def field(user: User, marker: str):
    (element,) = found(user, marker)
    return element


async def test_every_input_renders_full_width_with_no_fixed_sizes(user: User) -> None:
    await open_redrawing(user, validator_with(FORM, DATA))
    for marker in (
        "a2ui-field",
        "a2ui-textfield",
        "a2ui-checkbox",
        "a2ui-slider",
        "a2ui-datetime",
    ):
        elements = found(user, marker)
        assert elements, marker
        assert all("w-full" in e.classes for e in elements), marker
    assert len(found(user, "a2ui-choice")) == 2
    assert all("w-full" in e.classes for e in found(user, "a2ui-choice"))
    assert bad_sizing(user) == []
    assert found(user, "a2ui-unsupported") == []
    assert found(user, "a2ui-error") == []


async def test_hostile_labels_and_options_are_only_text(user: User) -> None:
    await open_redrawing(user, validator_with(FORM, DATA))
    with user.client:
        types = {type(e) for e in surface_elements(user)}
        assert ui.html not in types and ui.markdown not in types
    assert field(user, "a2ui-textfield").props["label"] == HOSTILE
    assert field(user, "a2ui-checkbox").text == HOSTILE
    radio = next(e for e in found(user, "a2ui-choice") if isinstance(e, ui.radio))
    assert radio.options == {"r": "Red", "g": HOSTILE, "b": "Blue"}


async def test_typing_updates_the_model_and_a_bound_text_follows(user: User) -> None:
    validator = validator_with(FORM, DATA)
    await open_redrawing(user, validator)
    assert shown(user)[:1] == ["Ann"]
    user.find(marker="a2ui-textfield").type("a").trigger("blur")
    assert model(validator, "/form/name") == "Anna"
    assert shown(user)[:1] == ["Anna"]  # the surface was drawn again
    assert field(user, "a2ui-textfield").value == "Anna"


async def test_typing_alone_does_not_redraw_or_write(user: User) -> None:
    validator = validator_with(FORM, DATA)
    await open_redrawing(user, validator)
    before = field(user, "a2ui-textfield")
    user.find(marker="a2ui-textfield").type("zzz")
    assert field(user, "a2ui-textfield") is before  # same element: focus is kept
    assert model(validator, "/form/name") == "Ann"


async def test_enter_commits_a_single_line_field_but_not_a_textarea(user: User) -> None:
    items = [
        {"id": "root", "component": "Column", "children": ["one", "many"]},
        {"id": "one", "component": "TextField", "label": "a", "value": {"@path": "/a"}},
        {
            "id": "many",
            "component": "TextField",
            "label": "b",
            "variant": "longText",
            "value": {"@path": "/b"},
        },
    ]
    validator = validator_with(items, {"a": "", "b": ""})
    await open_redrawing(user, validator)
    with user.client:
        one, many = (found(user, "a2ui-textfield") + [None])[:2]
    assert isinstance(many, ui.textarea) and isinstance(one, ui.input)
    user.find(kind=ui.input).type("x").trigger("keydown.enter")
    assert model(validator, "/a") == "x"
    user.find(kind=ui.textarea).type("y\n").trigger("keydown.enter")
    assert model(validator, "/b") == ""
    user.find(kind=ui.textarea).trigger("blur")
    assert model(validator, "/b") == "y\n"


async def test_the_edit_survives_a_later_update_components_replay(user: User) -> None:
    validator = validator_with(FORM, DATA)
    await open_redrawing(user, validator)
    user.find(marker="a2ui-textfield").type("!").trigger("blur")
    assert model(validator, "/form/name") == "Ann!"
    grown = [*FORM, text("extra", "more")]
    grown[0] = {**FORM[0], "children": [*FORM[0]["children"], "extra"]}
    assert validator.submit([components(grown)]).ok  # replays every accepted message
    assert model(validator, "/form/name") == "Ann!"
    assert validator.submit([data_model("Zed", "/form/other")]).ok
    assert model(validator, "/form/name") == "Ann!"


async def test_a_text_over_the_cap_is_refused_with_a_line_and_changes_nothing(
    user: User,
) -> None:
    validator = validator_with(FORM, DATA)
    await open_redrawing(user, validator)
    user.find(marker="a2ui-textfield").type("x" * MAX_VALUE_CHARS).trigger("blur")
    assert model(validator, "/form/name") == "Ann"
    assert shown(user, "a2ui-error") == [
        f"[A2UI input rejected: text longer than {MAX_VALUE_CHARS} characters]"
    ]
    assert shown(user)[:1] == ["Ann"]


async def test_a_text_exactly_at_the_cap_is_stored(user: User) -> None:
    validator = validator_with(FORM, DATA)
    await open_redrawing(user, validator)
    user.find(marker="a2ui-textfield").type("x" * (MAX_VALUE_CHARS - 3)).trigger("blur")
    assert len(model(validator, "/form/name")) == MAX_VALUE_CHARS  # type: ignore[arg-type]


async def test_an_unset_path_starts_empty_and_a_write_creates_it(user: User) -> None:
    validator = validator_with(FORM, {})
    await open_redrawing(user, validator)
    assert field(user, "a2ui-textfield").value == ""
    user.find(marker="a2ui-textfield").type("new").trigger("blur")
    assert model(validator, "/form/name") == "new"


async def test_a_literal_value_is_an_initial_value_and_nothing_is_written(
    user: User,
) -> None:
    items = [{"id": "root", "component": "TextField", "label": "L", "value": "start"}]
    validator = validator_with(items, {})
    await open_redrawing(user, validator)
    assert field(user, "a2ui-textfield").value == "start"
    user.find(marker="a2ui-textfield").type("!").trigger("blur")
    assert field(user, "a2ui-textfield").value == "start!"  # the UI keeps it
    assert validator.data_model("s") == {}
    assert found(user, "a2ui-error") == []


def one_field(variant: str) -> list[dict[str, object]]:
    return [
        {
            "id": "root",
            "component": "TextField",
            "label": "L",
            "variant": variant,
            "value": {"@path": "/v"},
        }
    ]


async def test_the_number_variant_refuses_text_that_is_not_a_number(user: User) -> None:
    validator = validator_with(one_field("number"), {"v": ""})
    await open_redrawing(user, validator)
    assert field(user, "a2ui-textfield").props["inputmode"] == "decimal"
    user.find(marker="a2ui-textfield").type("12x").trigger("blur")
    assert model(validator, "/v") == ""
    assert shown(user, "a2ui-error") == ["[A2UI input rejected: not a number]"]
    user.find(marker="a2ui-textfield").clear().type("12.5").trigger("blur")
    assert model(validator, "/v") == "12.5"


async def test_the_obscured_variant_is_a_password_field(user: User) -> None:
    validator = validator_with(one_field("obscured"), {"v": ""})
    await open_redrawing(user, validator)
    assert field(user, "a2ui-textfield").props["type"] == "password"


async def test_check_box_writes_a_bool(user: User) -> None:
    validator = validator_with(FORM, DATA)
    await open_redrawing(user, validator)
    with user.client:
        field(user, "a2ui-checkbox").set_value(True)
    assert model(validator, "/form/agree") is True
    assert field(user, "a2ui-checkbox").value is True


async def test_choice_picker_writes_lists_of_strings(user: User) -> None:
    validator = validator_with(FORM, DATA)
    await open_redrawing(user, validator)
    radio = next(e for e in found(user, "a2ui-choice") if isinstance(e, ui.radio))
    with user.client:
        radio.set_value("g")
    assert model(validator, "/form/one") == ["g"]
    select = next(e for e in found(user, "a2ui-choice") if isinstance(e, ui.select))
    assert select.multiple and "use-chips" in select.props
    with user.client:
        select.set_value(["r", "b"])
    assert model(validator, "/form/many") == ["r", "b"]


async def test_choice_picker_ignores_values_that_are_not_options(user: User) -> None:
    validator = validator_with(
        FORM, {"form": {**DATA["form"], "one": ["zzz"], "many": ["r", "zzz"]}}
    )  # type: ignore[dict-item]
    await open_redrawing(user, validator)
    radio, select = sorted(
        found(user, "a2ui-choice"), key=lambda e: isinstance(e, ui.select)
    )
    assert radio.value is None
    assert select.value == ["r"]


async def test_slider_writes_a_number_when_the_thumb_is_released(user: User) -> None:
    validator = validator_with(FORM, DATA)
    await open_redrawing(user, validator)
    slider = field(user, "a2ui-slider")
    assert (slider.props["min"], slider.props["max"], slider.props["step"]) == (
        0.0,
        10.0,
        1.0,
    )
    with user.client:
        slider.set_value(7)
    assert model(validator, "/form/level") == 2  # dragging stores nothing
    user.find(marker="a2ui-slider").trigger("change")
    assert model(validator, "/form/level") == 7
    assert isinstance(model(validator, "/form/level"), (int, float))
    assert field(user, "a2ui-slider").value == 7


async def test_slider_with_a_bad_range_is_unsupported(user: User) -> None:
    items = [{"id": "root", "component": "Slider", "min": 5, "max": 5, "value": 1}]
    await open_redrawing(user, validator_with(items, {}))
    assert shown(user, "a2ui-unsupported") == ["[unsupported A2UI value]"]


async def test_date_time_input_stores_valid_iso_text_and_refuses_the_rest(
    user: User,
) -> None:
    validator = validator_with(FORM, DATA)
    await open_redrawing(user, validator)
    user.find(marker="a2ui-datetime").type("2025-02-30").trigger("blur")
    assert model(validator, "/form/when") == ""
    (line,) = shown(user, "a2ui-error")
    assert line.startswith("[A2UI input rejected: not an ISO 8601 value")
    user.find(marker="a2ui-datetime").clear().type("2025-02-28").trigger("blur")
    assert model(validator, "/form/when") == "2025-02-28"


@pytest.mark.parametrize(
    ("date", "time", "good", "bad"),
    [
        (True, True, "2025-01-02T03:04:05", "2025-01-02"),
        (False, True, "03:04:05", "2025-01-02T03:04"),
        (True, False, "2025-01-02", "2025-13-01"),
    ],
)
async def test_date_time_input_follows_enable_date_and_enable_time(
    user: User, date: bool, time: bool, good: str, bad: str
) -> None:
    items = [
        {
            "id": "root",
            "component": "DateTimeInput",
            "enableDate": date,
            "enableTime": time,
            "value": {"@path": "/w"},
        }
    ]
    validator = validator_with(items, {"w": ""})
    await open_redrawing(user, validator)
    user.find(marker="a2ui-datetime").type(bad).trigger("blur")
    assert model(validator, "/w") == ""
    user.find(marker="a2ui-datetime").clear().type(good).trigger("blur")
    assert model(validator, "/w") == good


async def test_an_input_with_checks_is_drawn_and_the_checks_are_not_evaluated(
    user: User,
) -> None:
    items = [
        {
            "id": "root",
            "component": "TextField",
            "label": "L",
            "value": {"@path": "/v"},
            "checks": [
                {
                    "condition": {
                        "@call": "required",
                        "args": {"value": {"@path": "/v"}},
                    },
                    "message": "needed",
                }
            ],
        }
    ]
    await open_redrawing(user, validator_with(items, {"v": ""}))
    assert len(found(user, "a2ui-textfield")) == 1
    assert found(user, "a2ui-error") == []


async def test_a_modal_input_stores_the_edit_without_closing_the_dialog(
    user: User,
) -> None:
    items = [
        {"id": "root", "component": "Modal", "trigger": "t", "content": "f"},
        text("t", "open"),
        {"id": "f", "component": "TextField", "label": "L", "value": {"@path": "/v"}},
    ]
    validator = validator_with(items, {"v": ""})
    await open_redrawing(user, validator)
    dialog = field(user, "a2ui-modal")
    with user.client:
        dialog.open()
    user.find(marker="a2ui-textfield").type("in").trigger("blur")
    assert model(validator, "/v") == "in"
    assert field(user, "a2ui-modal") is dialog and dialog.value is True


def test_edit_problem_limits() -> None:
    from llm_harness.gui.a2ui_inputs import edit_problem

    assert edit_problem("x" * MAX_VALUE_CHARS) is None
    assert edit_problem("x" * (MAX_VALUE_CHARS + 1))
    assert edit_problem(True) is None and edit_problem(1.5) is None
    assert edit_problem(float("nan")) and edit_problem(float("inf"))
    assert edit_problem(["a"] * MAX_CHOICES) is None
    assert edit_problem(["a"] * (MAX_CHOICES + 1))
    assert edit_problem(["x" * (MAX_VALUE_CHARS + 1)])
    assert edit_problem([1])
    assert edit_problem({"a": 1}) and edit_problem(None)


def test_set_value_goes_through_the_atomic_submit_and_is_kept() -> None:
    validator = validator_with([text("root", {"@path": "/v"})], {"v": "a"})
    assert validator.set_value("s", "/v", "b").ok
    assert model(validator, "/v") == "b"
    assert validator.submit([components([text("root", {"@path": "/v"})])]).ok
    assert model(validator, "/v") == "b"


def test_set_value_refuses_unknown_surfaces_and_relative_paths_without_changes() -> (
    None
):
    validator = validator_with([text("root", {"@path": "/v"})], {"v": "a"})
    for surface, path in (("nope", "/v"), ("s", "v"), ("s", "")):
        outcome = validator.set_value(surface, path, "b")
        assert not outcome.ok and outcome.errors[0].startswith(
            "A2UI message rejected: "
        )
    assert model(validator, "/v") == "a"
    assert SurfaceValidator().set_value("s", "/v", 1).ok is False


def test_the_data_model_flag_and_copy() -> None:
    validator = SurfaceValidator()
    assert validator.submit(
        [
            {
                **create("on"),
                "createSurface": {
                    **create("on")["createSurface"],
                    "sendDataModel": True,
                },
            },
            create("off"),
            data_model({"a": 1}, surface_id="on"),
        ]
    ).ok
    assert validator.sends_data_model("on") is True
    assert validator.sends_data_model("off") is False
    assert validator.sends_data_model("none") is False
    copy_of = validator.data_model("on")
    assert copy_of == {"a": 1}
    copy_of["a"] = 2  # type: ignore[index]
    assert validator.data_model("on") == {"a": 1}
    assert validator.data_model("none") is None
