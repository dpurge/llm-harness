"""Orchestrator.send_ui_action: clicks on drawn surfaces run a turn, others are dropped."""

import copy
import logging

import pytest

from llm_harness.a2ui.demo import DEMO_MESSAGES
from llm_harness.context import answer_observation
from llm_harness.orchestrator import Orchestrator
from llm_harness.types import Done, StreamEvent, TextDelta
from tests.fake_steps import reply_events
from tests.test_orchestrator import ASK, RESPOND, SET_GOAL, everything, make

UI = copy.deepcopy(DEMO_MESSAGES)
CLICKED = {"type": "respond", "text": "clicked"}
LOGGER = "llm_harness.orchestrator"


async def click(
    orchestrator: Orchestrator,
    surface_id: str = "demo",
    name: str = "demo_clicked",
    context: dict[str, object] | None = None,
) -> list[StreamEvent]:
    return [
        e async for e in orchestrator.send_ui_action(surface_id, name, context or {})
    ]


async def test_unknown_surface_yields_nothing_calls_no_model_and_logs_no_content(
    caplog: pytest.LogCaptureFixture,
) -> None:
    orchestrator, provider = make([])
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        events = await click(
            orchestrator, "SECRET-SURFACE", "SECRET-NAME", {"k": "SECRET-VALUE"}
        )
    assert events == []
    assert provider.requests == []
    (record,) = caplog.records
    assert record.levelno == logging.WARNING
    assert record.getMessage() == (
        "dropped ui action for a surface this conversation did not draw"
    )
    assert "SECRET" not in caplog.text


async def test_known_surface_runs_a_turn_with_the_action_observation() -> None:
    orchestrator, provider = make(
        [reply_events({**RESPOND, "ui": UI}, SET_GOAL), reply_events(CLICKED)]
    )
    _ = [e async for e in orchestrator.send("hi")]
    events = await click(orchestrator)
    assert TextDelta(text="clicked") in events
    assert isinstance(events[-1], Done)
    assert len(provider.requests) == 2
    assert "ui_action: surface=demo action=demo_clicked context={}" in everything(
        provider.requests[1]
    )


async def test_click_answers_a_pending_question() -> None:
    orchestrator, provider = make(
        [reply_events({**ASK, "ui": UI}, SET_GOAL), reply_events(CLICKED)]
    )
    _ = [e async for e in orchestrator.send("hi")]
    assert orchestrator.pending_question == "What do you want?"
    await click(orchestrator)
    text = "ui_action: surface=demo action=demo_clicked context={}"
    assert answer_observation("What do you want?", text) in everything(
        provider.requests[1]
    )
    assert orchestrator.pending_question is None


async def test_context_is_sorted_json() -> None:
    orchestrator, provider = make(
        [reply_events({**RESPOND, "ui": UI}, SET_GOAL), reply_events(CLICKED)]
    )
    _ = [e async for e in orchestrator.send("hi")]
    await click(orchestrator, context={"b": 2, "a": "v"})
    assert 'context={"a": "v", "b": 2}' in everything(provider.requests[1])


async def test_a_long_context_is_cut_to_500_characters_with_an_ellipsis() -> None:
    orchestrator, provider = make(
        [reply_events({**RESPOND, "ui": UI}, SET_GOAL), reply_events(CLICKED)]
    )
    _ = [e async for e in orchestrator.send("hi")]
    await click(orchestrator, context={"k": "x" * 2000})
    seen = everything(provider.requests[1])
    context = seen.split("context=", 1)[1].split("\n", 1)[0]
    assert len(context) == 500
    assert context.endswith("...")
    assert "x" * 100 in context


async def test_control_characters_in_the_action_are_stripped() -> None:
    orchestrator, provider = make(
        [reply_events({**RESPOND, "ui": UI}, SET_GOAL), reply_events(CLICKED)]
    )
    _ = [e async for e in orchestrator.send("hi")]
    await click(orchestrator, name="a\x1b[31mb")
    assert "\x1b" not in everything(provider.requests[1])


async def test_a_click_during_a_running_turn_still_raises() -> None:
    orchestrator, _ = make([reply_events({**RESPOND, "ui": UI}, SET_GOAL)])
    _ = [e async for e in orchestrator.send("hi")]
    orchestrator._busy = True
    with pytest.raises(RuntimeError, match="already running"):
        await click(orchestrator)
