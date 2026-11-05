"""GUI: a later reply redraws, extends or removes the surfaces it touches."""

import asyncio

import pytest
from nicegui.testing import User
from nicegui.testing.user_interaction import UserInteraction

import llm_harness.gui.app as gui_app
from llm_harness.types import UiSurface
from tests.a2ui_support import (
    FORBIDDEN_CLASS,
    call,
    components,
    create,
    data_model,
    text,
)
from tests.fake_steps import reply_events
from tests.test_gui import StepFactory, elements, open_chat, texts, wait_for
from tests.test_gui_a2ui_turn import CLICKED, drawing_reply, page, send
from tests.test_orchestrator import RESPOND, SET_GOAL, everything, make, run

TITLE = {"@path": "/title"}


@pytest.fixture(autouse=True)
def no_leftover_pages() -> None:
    gui_app._open_pages.clear()


def board_components(
    context: dict[str, object] | None = None, extra: list[str] | None = None
) -> list[dict[str, object]]:
    """A Column with a bound title and a Button whose event context is `context`."""
    return [
        {
            "id": "root",
            "component": "Column",
            "children": ["title", "go", *(extra or [])],
        },
        text("title", TITLE),
        {
            "id": "go",
            "component": "Button",
            "child": "go_label",
            "action": {"event": {"name": "refresh", "context": context or {}}},
        },
        text("go_label", "Refresh"),
    ]


def board(
    surface_id: str = "board",
    context: dict[str, object] | None = None,
    title: str = "Ann",
) -> list[dict[str, object]]:
    return [
        create(surface_id),
        data_model({"title": title, "who": "Zed"}, surface_id=surface_id),
        components(board_components(context), surface_id),
    ]


def update_reply(ui: list[dict[str, object]], text_: str = "updated") -> list:
    return reply_events({**RESPOND, "text": text_, "ui": ui})


def delete(surface_id: str) -> dict[str, object]:
    return {"version": "1.0", "deleteSurface": {"surfaceId": surface_id}}


async def draw_board(user: User, factory: StepFactory) -> None:
    await open_chat(user, factory)
    await send(user, "show me")
    await wait_for(lambda: len(elements(user, "a2ui-button")) == 1)
    await wait_for(lambda: elements(user, "prompt")[0].enabled)


async def click_and_wait(user: User, factory: StepFactory, requests: int) -> None:
    user.find(marker="a2ui-button").click()
    await wait_for(lambda: len(factory.provider.requests) == requests)
    await wait_for(lambda: elements(user, "prompt")[0].enabled)


async def test_a_bound_title_is_drawn_from_the_data_model(user: User) -> None:
    await draw_board(user, StepFactory([drawing_reply(board())]))
    assert texts(user, "a2ui-text") == ["Ann"]
    assert len(elements(user, "a2ui-surface")) == 1


async def test_update_data_model_changes_the_text_in_place(user: User) -> None:
    factory = StepFactory(
        [
            drawing_reply(board()),
            update_reply([data_model("Bob", "/title", "board")]),
        ]
    )
    await draw_board(user, factory)
    (before,) = elements(user, "a2ui-surface")
    await click_and_wait(user, factory, 2)
    await wait_for(lambda: texts(user, "a2ui-text") == ["Bob"])
    (after,) = elements(user, "a2ui-surface")
    assert after.id == before.id  # same container, redrawn, not a second surface
    assert len(elements(user, "a2ui-button")) == 1


async def test_update_components_adds_a_text(user: User) -> None:
    grown = [*board_components(extra=["added"]), text("added", "Added line")]
    factory = StepFactory(
        [drawing_reply(board()), update_reply([components(grown, "board")])]
    )
    await draw_board(user, factory)
    await click_and_wait(user, factory, 2)
    await wait_for(lambda: "Added line" in texts(user, "a2ui-text"))
    assert texts(user, "a2ui-text") == ["Ann", "Added line"]
    assert len(elements(user, "a2ui-surface")) == 1


async def test_delete_surface_removes_the_container(user: User) -> None:
    factory = StepFactory([drawing_reply(board()), update_reply([delete("board")])])
    await draw_board(user, factory)
    await click_and_wait(user, factory, 2)
    await wait_for(lambda: elements(user, "a2ui-surface") == [])
    assert elements(user, "a2ui-button") == []
    assert elements(user, "a2ui-text") == []
    assert page()._surfaces == {}
    assert texts(user, "error") == []


async def test_a_click_works_again_after_a_redraw(user: User) -> None:
    factory = StepFactory(
        [
            drawing_reply(board()),
            update_reply([data_model("Bob", "/title", "board")]),
            reply_events(CLICKED),
        ]
    )
    await draw_board(user, factory)
    await click_and_wait(user, factory, 2)
    await wait_for(lambda: texts(user, "a2ui-text") == ["Bob"])
    await click_and_wait(user, factory, 3)
    assert texts(user, "ui-action") == [
        "ui_action: surface=board action=refresh",
        "ui_action: surface=board action=refresh",
    ]


async def test_a_bound_context_is_resolved_before_it_is_sent(user: User) -> None:
    factory = StepFactory(
        [
            drawing_reply(board(context={"who": {"@path": "/who"}, "k": "lit"})),
            reply_events(CLICKED),
        ]
    )
    await draw_board(user, factory)
    await click_and_wait(user, factory, 2)
    sent = everything(factory.provider.requests[1])
    assert 'context={"k": "lit", "who": "Zed"}' in sent


async def test_a_context_is_resolved_against_the_data_model_at_click_time(
    user: User,
) -> None:
    factory = StepFactory(
        [
            drawing_reply(board(context={"who": {"@path": "/who"}})),
            update_reply([data_model("Yan", "/who", "board")]),
            reply_events(CLICKED),
        ]
    )
    await draw_board(user, factory)
    await click_and_wait(user, factory, 2)
    await wait_for(lambda: len(texts(user, "a2ui-text")) == 1)
    await click_and_wait(user, factory, 3)
    assert 'context={"who": "Yan"}' in everything(factory.provider.requests[2])


async def test_an_unresolvable_context_binding_sends_nothing(user: User) -> None:
    factory = StepFactory([drawing_reply(board(context={"x": {"@path": "/missing"}}))])
    await draw_board(user, factory)
    user.find(marker="a2ui-button").click()
    await asyncio.sleep(0.2)
    assert texts(user, "a2ui-error") == ["[A2UI value error: path /missing not found]"]
    assert texts(user, "ui-action") == []
    assert len(factory.provider.requests) == 1
    assert elements(user, "prompt")[0].enabled


async def test_only_the_touched_surface_is_redrawn(user: User) -> None:
    both = [*board("a", title="Ann"), *board("b", title="Bea")]
    factory = StepFactory(
        [
            drawing_reply(both),
            update_reply([data_model("Cy", "/title", "b")]),
        ]
    )
    await open_chat(user, factory)
    await send(user, "show me")
    await wait_for(lambda: len(elements(user, "a2ui-surface")) == 2)
    await wait_for(lambda: elements(user, "prompt")[0].enabled)
    first, second = elements(user, "a2ui-surface")
    first_children = [child.id for child in first.descendants()]
    first_button = elements(user, "a2ui-button")[0]  # belongs to surface "a"
    UserInteraction(user, {first_button}, None).click()
    await wait_for(lambda: len(factory.provider.requests) == 2)
    await wait_for(lambda: elements(user, "prompt")[0].enabled)
    await wait_for(lambda: "Cy" in texts(user, "a2ui-text"))
    again_first, again_second = elements(user, "a2ui-surface")
    assert (again_first.id, again_second.id) == (first.id, second.id)
    assert [child.id for child in again_first.descendants()] == first_children
    assert texts(user, "a2ui-text") == ["Ann", "Cy"]


async def test_delete_and_create_in_one_batch_keeps_one_container(user: User) -> None:
    factory = StepFactory(
        [
            drawing_reply(board()),
            update_reply([delete("board"), *board(title="Cleo")]),
        ]
    )
    await draw_board(user, factory)
    await click_and_wait(user, factory, 2)
    await wait_for(lambda: "Cleo" in texts(user, "a2ui-text"))
    assert len(elements(user, "a2ui-surface")) == 1
    assert texts(user, "a2ui-text") == ["Cleo"]


async def test_new_chat_clears_the_container_mapping(user: User) -> None:
    factory = StepFactory([drawing_reply(board())])
    await draw_board(user, factory)
    assert list(page()._surfaces) == ["board"]
    user.find(marker="new-chat").click()
    await wait_for(lambda: elements(user, "a2ui-surface") == [])
    assert page()._surfaces == {}


async def test_redrawn_containers_are_full_width_with_no_fixed_sizes(
    user: User,
) -> None:
    grown = [*board_components(extra=["added"]), text("added", "Added line")]
    factory = StepFactory(
        [drawing_reply(board()), update_reply([components(grown, "board")])]
    )
    await draw_board(user, factory)
    await click_and_wait(user, factory, 2)
    await wait_for(lambda: "Added line" in texts(user, "a2ui-text"))
    drawn = [
        element
        for marker in ("a2ui-surface", "a2ui-column", "a2ui-text")
        for element in elements(user, marker)
    ]
    assert len(drawn) == 4
    for element in drawn:
        assert "w-full" in element.classes
    for element in [*drawn, *elements(user, "a2ui-button")]:
        assert not [c for c in element.classes if FORBIDDEN_CLASS.search(c)]


async def test_a_value_error_after_an_update_is_shown_and_the_page_survives(
    user: User,
) -> None:
    broken = [
        {"id": "root", "component": "Column", "children": ["title", "go"]},
        text("title", call("regex", value="a", pattern="(")),
        board_components()[2],
        text("go_label", "Refresh"),
    ]
    factory = StepFactory(
        [drawing_reply(board()), update_reply([components(broken, "board")])]
    )
    await draw_board(user, factory)
    await click_and_wait(user, factory, 2)
    await wait_for(lambda: texts(user, "a2ui-error") != [])
    assert texts(user, "a2ui-error") == [
        "[A2UI value error: invalid regex pattern: missing ) at position 1]"
    ]
    assert len(elements(user, "a2ui-button")) == 1
    assert texts(user, "error") == []


async def test_the_orchestrator_reports_updated_and_deleted_surfaces() -> None:
    first = [
        create("a"),
        create("c"),
        components([text("root", "x")], "a"),
        components([text("root", "y")], "c"),
    ]
    mixed = [
        components([text("root", "z")], "a"),
        data_model(1, "/n", "a"),
        create("b"),
        components([text("root", "w")], "b"),
        delete("c"),
    ]
    orchestrator, _ = make(
        [
            reply_events({**RESPOND, "ui": first}, SET_GOAL),
            update_reply(mixed),
        ]
    )
    await run(orchestrator, "hi")
    events = await run(orchestrator, "again")
    assert UiSurface(surface_ids=("a", "b"), deleted_surface_ids=("c",)) in events
    assert orchestrator.ui_validator.surface_ids == ("a", "b")
