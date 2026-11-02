"""The memory reflection notice in the three front ends: plain CLI, TUI and web GUI.

Each runs a real orchestrator and router over a scripted provider and a throwaway
SQLite file: a tool turn, then a scripted reflection reply that saves a note (or
fails). The notice is a `Notice` event, shown as the other one-line notes are.
"""

import io
import json
from pathlib import Path

import pytest
from nicegui import ui
from nicegui.testing import User

from llm_harness.cli import _run_turn
from llm_harness.config import StorageConfig, VectorConfig
from llm_harness.gui.app import register_page
from llm_harness.orchestrator import Orchestrator
from llm_harness.storage import Storage, open_storage
from llm_harness.tui.app import ChatApp
from llm_harness.types import Done, TextDelta
from tests.fake_steps import ScriptedSteps, reply_events
from tests.fakes import router_factory_for
from tests.test_call_line_ui import AGENT as BASE_AGENT
from tests.test_gui import (
    CONFIG as GUI_CONFIG,
)
from tests.test_gui import (
    MODEL_1 as GUI_MODEL,
)
from tests.test_gui import (
    StepFactory as GuiStepFactory,
)
from tests.test_gui import (
    elements,
    no_leftover_pages,  # noqa: F401 - autouse fixture
    send,
    wait_for,
)
from tests.test_gui_inspector import span_parents, span_titles
from tests.test_memory_tools import FakeClock, memory_registry
from tests.test_orchestrator import RESPOND, SET_GOAL, StubTool, call, model_cfg
from tests.test_tui import CONFIG, MODEL_1, StepFactory, texts
from tests.test_tui import send as tui_send
from tests.test_tui_inspector import settle
from tests.test_tui_inspector import titles as tui_titles

AGENT = BASE_AGENT.model_copy(update={"tools": ["search"]})
SAVED = "memory saved #1: Prefers metric units."
FAILED_START = "memory reflection failed: "
# turn, recall, step 1, its llm_call and tool_call, step 2, its llm_call,
# reflection and its llm_call.
SPAN_COUNT = 9


def scripts(reflection_events: list) -> list[list]:
    return [
        reply_events(call("search", q="x"), SET_GOAL),
        reply_events(RESPOND),
        reflection_events,
    ]


def saving() -> list:
    body = json.dumps(
        {
            "action": "save",
            "id": None,
            "text": "Prefers metric units.",
            "keywords": ["metric"],
        }
    )
    return [TextDelta(text=body), Done()]


def broken() -> list:
    return [TextDelta(text="not json"), Done()]


@pytest.fixture
def storage(tmp_path: Path) -> Storage:
    return open_storage(
        StorageConfig(vector=VectorConfig(backend="none")),
        tmp_path,
        relational_path=tmp_path / "memory.db",
    )


def tools():
    registry = memory_registry(FakeClock())
    registry.register(StubTool())
    return registry


# --- plain CLI --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("reflection_events", "line"),
    [(saving(), SAVED), (broken(), FAILED_START)],
    ids=["saved", "failed"],
)
async def test_the_cli_prints_the_notice_after_the_answer(
    storage, reflection_events, line
) -> None:
    orchestrator = Orchestrator(
        ScriptedSteps(scripts(reflection_events)),
        model_cfg(),
        tools=tools(),
        storage=storage,
        user_id="alice",
    )
    out, err = io.StringIO(), io.StringIO()
    assert await _run_turn(orchestrator, "hi", out, err)
    assert out.getvalue() == "hello\n"
    lines = err.getvalue().splitlines()
    assert lines[:2] == ['[step 1: tool search {"q": "x"}]', "[step 2: answering]"]
    (notice,) = lines[2:]
    assert notice.startswith(line)
    assert "\n" not in notice


async def test_the_cli_prints_nothing_for_a_none_reflection(storage) -> None:
    none = [
        TextDelta(
            text='{"action": "none", "id": null, "text": null, "keywords": null}'
        ),
        Done(),
    ]
    orchestrator = Orchestrator(
        ScriptedSteps(scripts(none)),
        model_cfg(),
        tools=tools(),
        storage=storage,
        user_id="alice",
    )
    out, err = io.StringIO(), io.StringIO()
    assert await _run_turn(orchestrator, "hi", out, err)
    assert err.getvalue().splitlines() == [
        '[step 1: tool search {"q": "x"}]',
        "[step 2: answering]",
    ]


# --- TUI --------------------------------------------------------------------------


def tui_app(storage: Storage, reflection_events: list) -> ChatApp:
    config = CONFIG.model_copy(update={"agents": [AGENT], "user_id": "alice"})
    return ChatApp(
        config,
        MODEL_1,
        router_factory_for(config, StepFactory(scripts(reflection_events)), None),
        False,
        storage=storage,
        tools=tools(),
    )


async def test_the_tui_shows_the_notice_and_the_reflection_span(storage) -> None:
    app = tui_app(storage, saving())
    async with app.run_test() as pilot:
        await tui_send(pilot, app, "hi")
        notes = texts(app, "note")
        assert SAVED in notes
        assert notes.index(SAVED) > notes.index("step 2: answering")
        await settle(pilot, app, SPAN_COUNT)
        kinds = [title.split(":")[0] for title in tui_titles(app).values()]
        assert kinds.count("reflection") == 1
        title = next(t for t in tui_titles(app).values() if t.startswith("reflection"))
        assert title.startswith("reflection: memory reflection | ok | ")
        assert app.query_one("#prompt").disabled is False


async def test_the_tui_shows_a_failure_as_a_note_and_the_turn_is_done(storage) -> None:
    app = tui_app(storage, broken())
    async with app.run_test() as pilot:
        await tui_send(pilot, app, "hi")
        (notice,) = [n for n in texts(app, "note") if n.startswith(FAILED_START)]
        assert "not valid JSON" in notice
        assert texts(app, "error") == []
        assert app.query_one("#prompt").disabled is False


# --- web GUI ----------------------------------------------------------------------


def register_gui(storage: Storage, reflection_events: list) -> None:
    config = GUI_CONFIG.model_copy(update={"agents": [AGENT], "user_id": "alice"})
    register_page(
        config,
        GUI_MODEL,
        router_factory_for(config, GuiStepFactory(scripts(reflection_events)), None),
        False,
        storage=storage,
        tools=tools(),
    )


async def test_the_gui_shows_the_notice_as_a_muted_plain_label(
    user: User, storage
) -> None:
    register_gui(storage, saving())
    await user.open("/")
    await send(user, "hi")
    await wait_for(lambda: any(e.text == SAVED for e in elements(user, "note")))
    (label,) = [e for e in elements(user, "note") if e.text == SAVED]
    assert isinstance(label, ui.label)  # never ui.html or markdown
    assert "text-grey" in set(label.classes)
    titles = span_titles(user)
    reflection_id = next(
        i for i, title in titles.items() if title.startswith("reflection: ")
    )
    llm_calls = [
        i for i, parent in span_parents(user).items() if parent == reflection_id
    ]
    assert [titles[i].split(":")[0] for i in llm_calls] == ["llm_call"]


async def test_the_gui_shows_a_failure_as_a_note(user: User, storage) -> None:
    register_gui(storage, broken())
    await user.open("/")
    await send(user, "hi")
    await wait_for(
        lambda: any(e.text.startswith(FAILED_START) for e in elements(user, "note"))
    )
    assert not elements(user, "error")
