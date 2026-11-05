"""GUI: typed input reaches the data model, other components, and (sendDataModel) the model."""

import pytest
from nicegui.testing import User

import llm_harness.gui.app as gui_app
from tests.a2ui_support import (
    CATALOG_ID,
    FORBIDDEN_CLASS,
    components,
    data_model,
    text,
)
from tests.fake_steps import reply_events
from tests.test_gui import StepFactory, elements, open_chat, texts, wait_for
from tests.test_gui_a2ui_turn import CLICKED, drawing_reply, page, send
from tests.test_orchestrator import RESPOND, everything

ACTION = {"event": {"name": "save", "context": {}}}


@pytest.fixture(autouse=True)
def no_leftover_pages() -> None:
    gui_app._open_pages.clear()


def form_components() -> list[dict[str, object]]:
    return [
        {"id": "root", "component": "Column", "children": ["name", "mirror", "go"]},
        {
            "id": "name",
            "component": "TextField",
            "label": "Name",
            "value": {"@path": "/form/name"},
        },
        text("mirror", {"@path": "/form/name"}),
        {"id": "go", "component": "Button", "child": "go_label", "action": ACTION},
        text("go_label", "Save"),
    ]


def form(send_data_model: bool | None, initial: str = "Ann") -> list[dict[str, object]]:
    create: dict[str, object] = {"surfaceId": "form", "catalogId": CATALOG_ID}
    if send_data_model is not None:
        create["sendDataModel"] = send_data_model
    return [
        {"version": "1.0", "createSurface": create},
        data_model({"form": {"name": initial}}, surface_id="form"),
        components(form_components(), "form"),
    ]


async def draw_form(user: User, factory: StepFactory) -> None:
    await open_chat(user, factory)
    await send(user, "show me")
    await wait_for(lambda: len(elements(user, "a2ui-button")) == 1)
    await wait_for(lambda: elements(user, "prompt")[0].enabled)


def type_and_leave(user: User, more: str) -> None:
    user.find(marker="a2ui-textfield").type(more).trigger("blur")


def model_value(path: str = "/form/name") -> object:
    orchestrator = page()._orchestrator
    assert orchestrator is not None
    return orchestrator.ui_validator.resolve("form", {"@path": path}).value


async def click_and_read(user: User, factory: StepFactory) -> str:
    user.find(marker="a2ui-button").click()
    await wait_for(lambda: len(factory.provider.requests) == 2)
    await wait_for(lambda: elements(user, "prompt")[0].enabled)
    return everything(factory.provider.requests[1])


def observation_line(seen: str) -> str:
    return next(line for line in seen.splitlines() if line.startswith("ui_action:"))


async def test_typing_updates_the_model_and_the_bound_text_in_the_chat(
    user: User,
) -> None:
    await draw_form(user, StepFactory([drawing_reply(form(True))]))
    assert texts(user, "a2ui-text") == ["Ann"]
    type_and_leave(user, "!")
    assert model_value() == "Ann!"
    await wait_for(lambda: texts(user, "a2ui-text") == ["Ann!"])
    assert len(elements(user, "a2ui-surface")) == 1
    assert elements(user, "a2ui-textfield")[0].value == "Ann!"


async def test_the_typed_text_survives_a_later_reply_that_updates_components(
    user: User,
) -> None:
    grown = [*form_components(), text("extra", "more")]
    grown[0] = {**grown[0], "children": [*grown[0]["children"], "extra"]}  # type: ignore[misc]
    factory = StepFactory(
        [
            drawing_reply(form(True)),
            reply_events(
                {**RESPOND, "text": "grown", "ui": [components(grown, "form")]}
            ),
        ]
    )
    await draw_form(user, factory)
    type_and_leave(user, "!")
    await click_and_read(user, factory)
    await wait_for(lambda: "more" in texts(user, "a2ui-text"))
    assert model_value() == "Ann!"
    assert texts(user, "a2ui-text")[:2] == ["Ann!", "more"] or "Ann!" in texts(
        user, "a2ui-text"
    )


async def test_a_click_on_a_send_data_model_surface_carries_the_typed_text(
    user: User,
) -> None:
    factory = StepFactory([drawing_reply(form(True)), reply_events(CLICKED)])
    await draw_form(user, factory)
    type_and_leave(user, "!")
    line = observation_line(await click_and_read(user, factory))
    assert (
        line
        == 'ui_action: surface=form action=save context={} data={"form": {"name": "Ann!"}}'
    )


@pytest.mark.parametrize("flag", [False, None])
async def test_a_surface_without_the_flag_keeps_the_old_text(
    user: User, flag: bool | None
) -> None:
    factory = StepFactory([drawing_reply(form(flag)), reply_events(CLICKED)])
    await draw_form(user, factory)
    type_and_leave(user, "SECRET")
    seen = await click_and_read(user, factory)
    assert observation_line(seen) == "ui_action: surface=form action=save context={}"
    assert "SECRET" not in seen
    assert " data=" not in seen


async def test_the_data_in_the_observation_is_cut_at_1000_characters(
    user: User,
) -> None:
    factory = StepFactory([drawing_reply(form(True)), reply_events(CLICKED)])
    await draw_form(user, factory)
    type_and_leave(user, "x" * 3000)
    seen = await click_and_read(user, factory)
    data = observation_line(seen).split(" data=", 1)[1]
    assert len(data) == 1000
    assert data.endswith("...")
    assert data.startswith('{"form": {"name": "Annxxx')


async def test_a_refused_edit_leaves_the_model_and_the_next_click_unchanged(
    user: User,
) -> None:
    factory = StepFactory([drawing_reply(form(True)), reply_events(CLICKED)])
    await draw_form(user, factory)
    type_and_leave(user, "x" * 10_001)
    assert model_value() == "Ann"
    assert len(texts(user, "a2ui-error")) == 1
    line = observation_line(await click_and_read(user, factory))
    assert line.endswith('data={"form": {"name": "Ann"}}')


async def test_input_elements_in_the_chat_are_full_width(user: User) -> None:
    await draw_form(user, StepFactory([drawing_reply(form(True))]))
    for marker in ("a2ui-field", "a2ui-textfield"):
        assert all("w-full" in e.classes for e in elements(user, marker))
    for element in [*elements(user, "a2ui-field"), *elements(user, "a2ui-textfield")]:
        assert not [c for c in element.classes if FORBIDDEN_CLASS.search(c)]
