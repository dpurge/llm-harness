"""GUI tests for the `/ui-demo` command: drawing, clicking, errors and help."""

import pytest
from nicegui.testing import User

import llm_harness.gui.app as gui_app
from llm_harness.commands import help_answer, help_detail, help_overview, parse_help
from tests.test_gui import REPLY, Factory, elements, open_chat, texts, wait_for

DESCRIPTION = "draw a demo A2UI surface (no model call)"


@pytest.fixture(autouse=True)
def no_leftover_pages() -> None:
    gui_app._open_pages.clear()


async def send(user: User, text: str) -> None:
    user.find(marker="prompt").type(text).trigger("keydown.enter")


async def draw_demo(user: User) -> Factory:
    factory = Factory(REPLY)
    await open_chat(user, factory)
    await send(user, "/ui-demo")
    await wait_for(lambda: len(elements(user, "a2ui-button")) == 1)
    return factory


async def test_ui_demo_draws_heading_body_and_button_without_a_model_call(
    user: User,
) -> None:
    factory = await draw_demo(user)
    assert texts(user, "a2ui-text") == [
        "A2UI demo surface",
        "This surface is drawn from fixed A2UI v1.0 messages; no model was called.",
    ]
    assert texts(user, "a2ui-button") == ["Click me"]
    (log,) = elements(user, "log")
    assert all(e in log.descendants() for e in elements(user, "a2ui-column"))
    assert texts(user, "user") == []
    assert elements(user, "prompt")[0].value == ""
    assert all(not p.requests for p in factory.providers)


async def test_clicking_the_demo_button_shows_the_action_line(user: User) -> None:
    await draw_demo(user)
    user.find(marker="a2ui-button").click()
    await wait_for(lambda: texts(user, "ui-action") != [])
    assert texts(user, "ui-action") == ["ui_action: surface=demo action=demo_clicked"]


async def test_an_invalid_message_set_shows_an_error_line_and_draws_nothing(
    user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        gui_app,
        "DEMO_MESSAGES",
        [{"version": "1.0", "createSurface": {"surfaceId": "x", "catalogId": "bad"}}],
    )
    await open_chat(user, Factory(REPLY))
    await send(user, "/ui-demo")
    await wait_for(lambda: texts(user, "error") != [])
    (error,) = texts(user, "error")
    assert error.startswith("error: A2UI message rejected: ")
    for marker in ("a2ui-column", "a2ui-text", "a2ui-button"):
        assert elements(user, marker) == []


async def test_gui_help_lists_and_explains_ui_demo(user: User) -> None:
    await open_chat(user, Factory(REPLY))
    await send(user, "/help")
    await wait_for(lambda: texts(user, "help") != [])
    assert f"/ui-demo - {DESCRIPTION}" in texts(user, "help")[0]


def test_help_lists_ui_demo_for_the_gui_only() -> None:
    assert f"/ui-demo - {DESCRIPTION}" in help_overview("gui")
    assert "/ui-demo" not in help_overview("plain")
    assert "/ui-demo" not in help_overview("tui")


def test_help_detail_of_ui_demo_exists_for_the_gui_only() -> None:
    request = parse_help("/help ui-demo")
    assert request is not None
    assert help_answer("gui", request) == help_detail("gui", "/UI-demo")
    assert help_detail("gui", "ui-demo").startswith("usage: /ui-demo")
    for front_end in ("plain", "tui"):
        assert help_detail(front_end, "ui-demo") == 'no help for "ui-demo"; type /help'
