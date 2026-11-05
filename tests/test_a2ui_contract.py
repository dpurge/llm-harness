"""The reply contract carries A2UI messages: validated, retried, announced."""

import copy
from pathlib import Path

import pytest
from nicegui.testing import User

import llm_harness.gui.app as gui_app
from llm_harness.a2ui.demo import DEMO_MESSAGES
from llm_harness.state import AskUserAction, RespondAction, parse_reply, reply_schema
from llm_harness.types import (
    Done,
    ErrorEvent,
    StatusEvent,
    TextDelta,
    UiSurface,
)
from tests.fake_steps import ScriptedSteps, reply_events
from tests.test_cli_orchestrator import run_plain, steps, write_config_dir
from tests.test_gui import elements, open_chat, replies, texts, wait_for
from tests.test_orchestrator import ASK, RESPOND, SET_GOAL, make, observation, run
from tests.test_tui import StepFactory, make_app, reply_texts, send
from tests.test_tui import texts as tui_texts

UI = copy.deepcopy(DEMO_MESSAGES)
RESPOND_UI = {**RESPOND, "ui": UI}
ASK_UI = {**ASK, "ui": UI}


def unknown_component_ui() -> list[dict[str, object]]:
    ui = copy.deepcopy(UI)
    create = ui[0]["createSurface"]
    assert isinstance(create, dict)
    create["components"][1]["component"] = "Hologram"
    return ui


async def test_respond_with_ui_yields_the_surface_before_the_text() -> None:
    orchestrator, _ = make([reply_events(RESPOND_UI, SET_GOAL)])
    events = await run(orchestrator, "hi")
    assert events == [
        StatusEvent(step=1, action_type="respond", text="answering"),
        UiSurface(surface_ids=("demo",)),
        TextDelta(text="hello"),
        Done(),
    ]
    assert orchestrator.ui_validator.surface_ids == ("demo",)


async def test_ask_user_with_ui_yields_the_surface_before_the_question() -> None:
    orchestrator, _ = make([reply_events(ASK_UI)])
    events = await run(orchestrator, "hi")
    assert events == [
        StatusEvent(step=1, action_type="ask_user", text="asking the user"),
        UiSurface(surface_ids=("demo",)),
        TextDelta(text="What do you want?"),
        Done(),
    ]
    assert orchestrator.ui_validator.surface_ids == ("demo",)


async def test_invalid_ui_is_retried_with_the_validator_error() -> None:
    bad = {**RESPOND, "ui": unknown_component_ui()}
    orchestrator, provider = make(
        [reply_events(bad, SET_GOAL), reply_events(RESPOND_UI, SET_GOAL)]
    )
    events = await run(orchestrator, "hi")
    assert events[-1] == Done()
    assert UiSurface(surface_ids=("demo",)) in events
    retry = observation(provider.requests[1])
    assert "A2UI message rejected" in retry
    assert "Hologram" in retry
    assert "omit the `ui` key" in retry
    assert orchestrator.ui_validator.surface_ids == ("demo",)


async def test_empty_ui_list_is_rejected() -> None:
    bad = {**RESPOND, "ui": []}
    orchestrator, provider = make(
        [reply_events(bad, SET_GOAL), reply_events(RESPOND, SET_GOAL)]
    )
    events = await run(orchestrator, "hi")
    assert "must not be empty" in observation(provider.requests[1])
    assert not any(isinstance(event, UiSurface) for event in events)


async def test_ui_rejected_three_times_ends_with_an_error_and_no_surface() -> None:
    bad = {**RESPOND, "ui": unknown_component_ui()}
    orchestrator, provider = make([reply_events(bad, SET_GOAL)] * 3)
    events = await run(orchestrator, "hi")
    assert len(provider.requests) == 3
    assert isinstance(events[-1], ErrorEvent)
    assert "rejected 3 times" in events[-1].message
    assert not any(isinstance(event, UiSurface) for event in events)
    assert orchestrator.ui_validator.surface_ids == ()


async def test_valid_ui_with_a_bad_patch_leaves_the_validator_unchanged() -> None:
    orchestrator, provider = make(
        [
            reply_events(RESPOND_UI, {"environment": {}}),
            reply_events(RESPOND_UI, SET_GOAL),
        ]
    )
    # Seen from inside the first attempt's rejection: the retry request is built
    # after _accept failed, so the validator must still be empty at that point.
    seen: list[tuple[str, ...]] = []
    original = provider.stream_chat

    async def spy(request):
        seen.append(orchestrator.ui_validator.surface_ids)
        async for event in original(request):
            yield event

    provider.stream_chat = spy  # type: ignore[method-assign]
    events = await run(orchestrator, "hi")
    assert seen == [(), ()]
    assert "`environment` is protected" in observation(provider.requests[1])
    assert events[-1] == Done()
    assert orchestrator.ui_validator.surface_ids == ("demo",)


async def test_valid_ui_with_an_empty_goal_leaves_the_validator_unchanged() -> None:
    orchestrator, provider = make(
        [reply_events(RESPOND_UI), reply_events(RESPOND_UI, SET_GOAL)]
    )
    events = await run(orchestrator, "hi")
    assert "goal is empty" in observation(provider.requests[1])
    assert events[-1] == Done()
    # Had the first attempt been committed, the same surface id would clash.
    assert orchestrator.ui_validator.surface_ids == ("demo",)


async def test_a_reply_without_ui_has_no_surface_event() -> None:
    orchestrator, _ = make([reply_events(RESPOND, SET_GOAL)])
    events = await run(orchestrator, "hi")
    assert events == [
        StatusEvent(step=1, action_type="respond", text="answering"),
        TextDelta(text="hello"),
        Done(),
    ]
    assert orchestrator.ui_validator.surface_ids == ()


async def test_a_delete_only_batch_yields_the_deleted_surface_id() -> None:
    orchestrator, _ = make(
        [
            reply_events(RESPOND_UI, SET_GOAL),
            reply_events(
                {
                    **RESPOND,
                    "ui": [{"version": "1.0", "deleteSurface": {"surfaceId": "demo"}}],
                }
            ),
        ]
    )
    await run(orchestrator, "hi")
    events = await run(orchestrator, "again")
    assert UiSurface(surface_ids=(), deleted_surface_ids=("demo",)) in events
    assert events[-1] == Done()
    assert orchestrator.ui_validator.surface_ids == ()


def test_reply_schema_keeps_ui_optional_and_a_reply_without_ui_parses() -> None:
    # jsonschema is only a transitive dependency, so the schema is read directly.
    schema = reply_schema()
    for model in (RespondAction, AskUserAction):
        assert "ui" in model.model_fields
    for name in ("RespondAction", "AskUserAction"):
        assert "ui" in schema["$defs"][name]["properties"]
        assert "ui" not in schema["$defs"][name]["required"]
    plain = '{"state_patch": {}, "action": {"type": "respond", "text": "x"}}'
    assert parse_reply(plain).action == RespondAction(type="respond", text="x")


# Other front ends


def test_plain_prints_the_omitted_note_on_stderr(tmp_path: Path) -> None:
    _, factory = steps(reply_events(RESPOND_UI, SET_GOAL))
    result = run_plain(write_config_dir(tmp_path), factory, tmp_path)
    assert result.code == 0
    assert result.out == "hello\n"
    assert "[ui surface omitted]\n" in result.err


async def test_tui_shows_the_omitted_note_and_the_reply() -> None:
    app = make_app(StepFactory([reply_events(RESPOND_UI, SET_GOAL)]))
    async with app.run_test() as pilot:
        await send(pilot, app, "hi")
        assert tui_texts(app, "note") == ["step 1: answering", "[ui surface omitted]"]
        assert reply_texts(app) == ["hello"]
        assert tui_texts(app, "error") == []


@pytest.fixture
def no_leftover_pages() -> None:
    gui_app._open_pages.clear()


async def test_gui_draws_the_surface_event(user: User, no_leftover_pages: None) -> None:
    factory = StepFactory([reply_events(RESPOND_UI, SET_GOAL)])
    await open_chat(user, factory)
    user.find(marker="prompt").type("hi").trigger("keydown.enter")
    await wait_for(lambda: replies(user) != [])
    await wait_for(lambda: elements(user, "prompt")[0].enabled)
    assert texts(user, "status") == ["step 1: answering"]
    assert texts(user, "error") == []
    assert len(elements(user, "a2ui-text")) == 2
    assert isinstance(factory.provider, ScriptedSteps)
