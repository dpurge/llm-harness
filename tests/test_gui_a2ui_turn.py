"""GUI: model surfaces are drawn in the chat log and a click starts a turn."""

import asyncio
import copy
import re
from collections.abc import AsyncIterator

import pytest
from nicegui.testing import User

import llm_harness.gui.app as gui_app
from llm_harness.a2ui.demo import DEMO_MESSAGES
from llm_harness.types import ChatRequest, StreamEvent
from tests.fake_steps import ScriptedSteps, reply_events
from tests.test_gui import MODEL_2, StepFactory, elements, open_chat, texts, wait_for
from tests.test_orchestrator import RESPOND, SET_GOAL, everything

FORBIDDEN_CLASS = re.compile(r"(^|:)max-w-|(^|:)mx-auto|(^|:)h-(\d|\[|px)|\d+px")
CLICKED = {"type": "respond", "text": "clicked reply"}
ACTION_LINE = "ui_action: surface=demo action=demo_clicked"


@pytest.fixture(autouse=True)
def no_leftover_pages() -> None:
    gui_app._open_pages.clear()


def surface(context: object | None = None) -> list[dict[str, object]]:
    messages = copy.deepcopy(DEMO_MESSAGES)
    if context is not None:
        components = messages[0]["createSurface"]["components"]  # type: ignore[index]
        button = next(c for c in components if c["id"] == "button")
        button["action"]["event"]["context"] = context
    return messages


def drawing_reply(ui: list[dict[str, object]]) -> list[StreamEvent]:
    return reply_events({**RESPOND, "ui": ui}, SET_GOAL)


async def send(user: User, text: str) -> None:
    user.find(marker="prompt").type(text).trigger("keydown.enter")


async def draw(user: User, factory: StepFactory) -> None:
    await open_chat(user, factory)
    await send(user, "show me")
    await wait_for(lambda: len(elements(user, "a2ui-button")) == 1)
    await wait_for(lambda: elements(user, "prompt")[0].enabled)


def page() -> gui_app.ChatPage:
    (chat,) = gui_app._open_pages
    return chat


def reply_html(user: User) -> str:
    return "".join(e.props["innerHTML"] for e in elements(user, "reply"))


async def test_a_reply_with_ui_draws_the_surface_in_the_log(user: User) -> None:
    await draw(user, StepFactory([drawing_reply(surface())]))
    assert texts(user, "a2ui-text") == [
        "A2UI demo surface",
        "This surface is drawn from fixed A2UI v1.0 messages; no model was called.",
    ]
    assert texts(user, "a2ui-button") == ["Click me"]
    (log,) = elements(user, "log")
    assert all(e in log.descendants() for e in elements(user, "a2ui-column"))
    assert "hello" in reply_html(user)
    assert texts(user, "error") == []


async def test_clicking_the_button_starts_a_turn_with_the_action_observation(
    user: User,
) -> None:
    factory = StepFactory([drawing_reply(surface()), reply_events(CLICKED)])
    await draw(user, factory)
    user.find(marker="a2ui-button").click()
    await wait_for(lambda: "clicked reply" in reply_html(user))
    await wait_for(lambda: elements(user, "prompt")[0].enabled)
    requests = factory.provider.requests
    assert len(requests) == 2
    assert f"{ACTION_LINE} context={{}}" in everything(requests[1])
    assert texts(user, "ui-action") == [ACTION_LINE]
    assert texts(user, "error") == []
    assert elements(user, "prompt")[0].value == ""


async def test_the_context_reaches_the_observation_as_sorted_json(
    user: User,
) -> None:
    factory = StepFactory(
        [drawing_reply(surface({"k": "v", "a": [1, True]})), reply_events(CLICKED)]
    )
    await draw(user, factory)
    user.find(marker="a2ui-button").click()
    await wait_for(lambda: len(factory.provider.requests) == 2)
    assert 'context={"a": [1, true], "k": "v"}' in everything(
        factory.provider.requests[1]
    )


async def test_a_context_longer_than_500_characters_is_cut(user: User) -> None:
    factory = StepFactory(
        [drawing_reply(surface({"k": "x" * 2000})), reply_events(CLICKED)]
    )
    await draw(user, factory)
    user.find(marker="a2ui-button").click()
    await wait_for(lambda: len(factory.provider.requests) == 2)
    seen = everything(factory.provider.requests[1])
    context = seen.split("context=", 1)[1].split("\n", 1)[0]
    assert len(context) == 500
    assert context.endswith("...")


async def test_a_data_binding_in_the_context_still_draws_the_button(
    user: User,
) -> None:
    # Since slice 3a the context is resolved at click time, so it may hold bindings.
    factory = StepFactory([drawing_reply(surface({"k": {"@path": "/form/name"}}))])
    await draw(user, factory)
    assert elements(user, "a2ui-unsupported") == []


class StallSecondCall(ScriptedSteps):
    """Like ScriptedSteps, but the second provider call waits for `release`."""

    def __init__(self, scripts: list[list[StreamEvent]]) -> None:
        super().__init__(scripts)
        self.release = asyncio.Event()

    async def stream_chat(self, req: ChatRequest) -> AsyncIterator[StreamEvent]:
        if len(self.requests) == 1:
            await self.release.wait()
        async for event in super().stream_chat(req):
            yield event


async def test_a_click_while_a_turn_runs_is_ignored_with_a_visible_note(
    user: User,
) -> None:
    provider = StallSecondCall([drawing_reply(surface()), reply_events(CLICKED)])
    factory = StepFactory([])
    factory.provider = provider
    await draw(user, factory)
    user.find(marker="a2ui-button").click()
    await wait_for(lambda: len(provider.requests) == 1 and page()._turn_is_running())
    await wait_for(lambda: not elements(user, "prompt")[0].enabled)
    user.find(marker="a2ui-button").click()
    await wait_for(lambda: "[turn running: click ignored]" in texts(user, "note"))
    assert texts(user, "ui-action") == [ACTION_LINE]
    provider.release.set()
    await wait_for(lambda: "clicked reply" in reply_html(user))
    await wait_for(lambda: elements(user, "prompt")[0].enabled)
    assert len(provider.requests) == 2


async def test_new_chat_clears_the_surfaces_and_a_stale_handler_starts_no_turn(
    user: User,
) -> None:
    factory = StepFactory([drawing_reply(surface())])
    await draw(user, factory)
    old = page()._orchestrator
    assert old is not None
    user.find(marker="new-chat").click()
    await wait_for(lambda: elements(user, "a2ui-button") == [])
    assert elements(user, "a2ui-text") == []
    assert page()._orchestrator is not old
    # The old button is gone from the log; calling its handler directly is refused.
    page()._on_ui_click(old, "demo", "demo_clicked", {})
    await asyncio.sleep(0.1)
    assert texts(user, "note") == ["[surface of an earlier session: click ignored]"]
    assert texts(user, "ui-action") == []
    assert len(factory.provider.requests) == 1
    assert elements(user, "prompt")[0].enabled


async def test_after_a_model_switch_the_old_surface_stays_but_its_click_is_refused(
    user: User,
) -> None:
    factory = StepFactory([drawing_reply(surface())])
    await draw(user, factory)
    # What the model selector does when it is unlocked: a new session, log untouched.
    assert await page()._start_session(MODEL_2, True)
    assert len(elements(user, "a2ui-button")) == 1
    user.find(marker="a2ui-button").click()
    await wait_for(lambda: texts(user, "note") != [])
    assert texts(user, "note") == ["[surface of an earlier session: click ignored]"]
    assert texts(user, "ui-action") == []
    assert len(factory.provider.requests) == 1


async def test_model_surface_elements_are_full_width_with_no_fixed_sizes(
    user: User,
) -> None:
    await draw(user, StepFactory([drawing_reply(surface())]))
    drawn = [
        e for marker in ("a2ui-column", "a2ui-text") for e in elements(user, marker)
    ]
    assert len(drawn) == 3
    for element in drawn:
        assert "w-full" in element.classes
    for element in [*drawn, *elements(user, "a2ui-button")]:
        assert not [c for c in element.classes if FORBIDDEN_CLASS.search(c)]
