"""`/memory` in the terminal UI and the web GUI: shown in the chat, never sent on.

Offline: a throwaway SQLite file under `tmp_path`, a scripted provider.
"""

from pathlib import Path

import pytest
from nicegui.testing import User

from llm_harness.builtin_tools import build_tool_registry
from llm_harness.config import StorageConfig, VectorConfig
from llm_harness.gui import app as gui_app
from llm_harness.gui.app import register_page
from llm_harness.memory import MemoryStore
from llm_harness.memory_command import MEMORY_OFF, VECTORS_OFF
from llm_harness.orchestrator import Orchestrator
from llm_harness.storage import Storage, open_storage
from llm_harness.tui.app import ChatApp
from tests.fakes import router_factory_for
from tests.test_gui import (
    CONFIG as GUI_CONFIG,
)
from tests.test_gui import (
    MODEL_1 as GUI_MODEL,
)
from tests.test_gui import (
    REPLY as GUI_REPLY,
)
from tests.test_gui import (
    Factory as GuiFactory,
)
from tests.test_gui import (
    elements,
    no_leftover_pages,  # noqa: F401  (autouse fixture)
    orchestrators,  # noqa: F401  (fixture)
    widget,
)
from tests.test_gui import (
    send as gui_send,
)
from tests.test_gui import (
    texts as gui_texts,
)
from tests.test_gui import (
    wait_for as gui_wait_for,
)
from tests.test_tui import (
    CONFIG as TUI_CONFIG,
)
from tests.test_tui import (
    MODEL_1 as TUI_MODEL,
)
from tests.test_tui import (
    REPLY as TUI_REPLY,
)
from tests.test_tui import (
    Factory as TuiFactory,
)
from tests.test_tui import (
    prompt_of,
)
from tests.test_tui import (
    send as tui_send,
)
from tests.test_tui import (
    texts as tui_texts,
)


@pytest.fixture
def storage(tmp_path: Path) -> Storage:
    return open_storage(
        StorageConfig(vector=VectorConfig(backend="none")),
        tmp_path,
        relational_path=tmp_path / "memory.db",
    )


async def seeded(storage: Storage) -> None:
    await MemoryStore(storage.relational).save("ann", "likes green tea", ["drink"])


def tools_for(memory: bool = True):
    return build_tool_registry([], memory=memory, user_id="ann" if memory else None)


# Terminal UI


def tui_app(storage: Storage, factory, *, user_id: str | None = "ann") -> ChatApp:
    config = TUI_CONFIG.model_copy(update={"user_id": user_id})
    return ChatApp(
        config,
        TUI_MODEL,
        router_factory_for(config, factory, None),
        False,
        storage=storage,
        tools=tools_for(user_id is not None),
    )


async def test_tui_lists_shows_and_deletes_without_a_model_call(
    storage: Storage,
) -> None:
    await seeded(storage)
    factory = TuiFactory(TUI_REPLY)
    app = tui_app(storage, factory)
    async with app.run_test() as pilot:
        await tui_send(pilot, app, "/memory")
        await tui_send(pilot, app, "/memory show 1")
        await tui_send(pilot, app, "/memory delete 1")
        await tui_send(pilot, app, "/memory")
        notes = tui_texts(app, "note")
        assert notes[0].startswith("#1 [drink] (updated ")
        assert notes[0].endswith("likes green tea\npage 1 of 1")
        assert notes[1].splitlines()[-1] == "likes green tea"
        assert notes[2] == "deleted memory #1: likes green tea"
        assert notes[3] == "(no memories)"
        assert prompt_of(app).value == ""
        # Never a user turn, never a model request.
        assert tui_texts(app, "user") == []
        assert app._orchestrator is not None
        assert app._orchestrator.state.turn == 0
        assert factory.providers[0].requests == []


async def test_tui_output_is_plain_text_filling_the_width(storage: Storage) -> None:
    await seeded(storage)
    app = tui_app(storage, TuiFactory(TUI_REPLY))
    async with app.run_test() as pilot:
        await tui_send(pilot, app, "/memory")
        (line,) = app.query("#log Static.note")
        assert line._render_markup is False  # model-derived text is never markup
        assert "#log { width: 1fr; height: 1fr; }" in ChatApp.CSS
        assert not any(rule in ChatApp.CSS for rule in ("max-width", "width: 80"))


async def test_tui_reindex_answers(storage: Storage) -> None:
    app = tui_app(storage, TuiFactory(TUI_REPLY))
    async with app.run_test() as pilot:
        await tui_send(pilot, app, "/memory reindex")
        assert tui_texts(app, "note") == [VECTORS_OFF]  # no memory_vectors in this rig


async def test_tui_memory_off_answers_one_line(storage: Storage) -> None:
    app = tui_app(storage, TuiFactory(TUI_REPLY), user_id=None)
    async with app.run_test() as pilot:
        await tui_send(pilot, app, "/memory")
        assert tui_texts(app, "note") == [MEMORY_OFF]


async def test_tui_text_that_merely_contains_memory_is_a_normal_message(
    storage: Storage,
) -> None:
    factory = TuiFactory(TUI_REPLY)
    app = tui_app(storage, factory)
    async with app.run_test() as pilot:
        await tui_send(pilot, app, "what is /memory")
        assert tui_texts(app, "user") == ["you: what is /memory"]
        assert factory.providers[0].requests


# Web GUI


async def open_gui(
    user: User, storage: Storage, factory, *, user_id: str | None = "ann"
) -> None:
    config = GUI_CONFIG.model_copy(update={"user_id": user_id})
    register_page(
        config,
        GUI_MODEL,
        router_factory_for(config, factory, None),
        False,
        storage,
        tools_for(user_id is not None),
    )
    await user.open("/")


async def gui_command(user: User, text: str, expected: int) -> None:
    user.find(marker="prompt").type(text).trigger("keydown.enter")
    await gui_wait_for(lambda: len(gui_texts(user, "memory")) == expected)


async def test_gui_lists_shows_and_deletes_without_a_model_call(
    user: User,
    storage: Storage,
    orchestrators: list[Orchestrator],  # noqa: F811
) -> None:
    await seeded(storage)
    factory = GuiFactory(GUI_REPLY)
    await open_gui(user, storage, factory)
    await gui_command(user, "/memory", 1)
    await gui_command(user, "/memory show 1", 2)
    await gui_command(user, "/memory delete 1", 3)
    await gui_command(user, "/memory", 4)
    shown = gui_texts(user, "memory")
    assert shown[0].startswith("#1 [drink] (updated ")
    assert shown[1].splitlines()[-1] == "likes green tea"
    assert shown[2] == "deleted memory #1: likes green tea"
    assert shown[3] == "(no memories)"
    assert widget(user, "prompt").value == ""
    assert gui_texts(user, "user") == []
    assert factory.providers[0].requests == []
    assert orchestrators[0].has_completed_turn is False


async def test_gui_output_is_full_width_large_text_not_markup(
    user: User, storage: Storage
) -> None:
    await seeded(storage)
    await open_gui(user, storage, GuiFactory(GUI_REPLY))
    await gui_command(user, "/memory", 1)
    (label,) = elements(user, "memory")
    assert {"w-full", "whitespace-pre-wrap"} <= set(label.classes)
    assert not [c for c in label.classes if c.startswith(("max-w", "h-", "max-h"))]
    assert "mx-auto" not in label.classes
    assert "llm-secondary" not in label.classes  # main content: the 18px base
    assert ".llm-page { font-size: 18px; }" in gui_app._PAGE_CSS
    assert type(label).__name__ == "Label"  # a text node, never ui.html or markdown


async def test_gui_reindex_answers(user: User, storage: Storage) -> None:
    await open_gui(user, storage, GuiFactory(GUI_REPLY))
    await gui_command(user, "/memory reindex", 1)
    assert gui_texts(user, "memory") == [VECTORS_OFF]


async def test_gui_memory_off_answers_one_line(user: User, storage: Storage) -> None:
    await open_gui(user, storage, GuiFactory(GUI_REPLY), user_id=None)
    await gui_command(user, "/memory", 1)
    assert gui_texts(user, "memory") == [MEMORY_OFF]


async def test_gui_text_that_merely_contains_memory_is_a_normal_message(
    user: User, storage: Storage
) -> None:
    factory = GuiFactory(GUI_REPLY)
    await open_gui(user, storage, factory)
    await gui_send(user, "what is /memory")
    assert gui_texts(user, "user") == ["you: what is /memory"]
    assert factory.providers[0].requests
