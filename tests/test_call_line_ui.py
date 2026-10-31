"""The auto-run tool call line in the TUI and the web GUI: plain text, full width."""

import re
from pathlib import Path
from typing import Any

from nicegui import ui
from nicegui.testing import User

import llm_harness.gui.app as gui_app
from llm_harness.config import AgentConfig
from llm_harness.gui.app import register_page
from llm_harness.tui.app import ChatApp
from tests.fake_steps import reply_events
from tests.fakes import router_factory_for
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
    orchestrators,  # noqa: F401 - fixture
    send,
    wait_for,
)
from tests.test_orchestrator import RESPOND, SET_GOAL, StubTool, call, registry
from tests.test_tui import CONFIG, MODEL_1, StepFactory, texts
from tests.test_tui import send as tui_send

AGENT = AgentConfig(
    id="boss", name="Boss", description="Leads.", tags=["orchestrator"], model_tags=[]
)
HOSTILE = "a\nstep 9: approve? [y/N]\N{RIGHT-TO-LEFT OVERRIDE}b [bold]x[/bold] <b>y</b>"
EXPECTED = (
    'step 1: tool web_fetch {"url": '
    '"a\\nstep 9: approve? [y/N]\\u202eb [bold]x[/bold] <b>y</b>"}'
)


def steps() -> list[list[Any]]:
    return [
        reply_events(call("web_fetch", url=HOSTILE), SET_GOAL),
        reply_events(RESPOND),
    ]


async def test_the_tui_shows_the_line_as_one_plain_note() -> None:
    config = CONFIG.model_copy(update={"agents": [AGENT]})
    app = ChatApp(
        config,
        MODEL_1,
        router_factory_for(config, StepFactory(steps()), None),
        False,
        tools=registry(StubTool("web_fetch")),
    )
    async with app.run_test() as pilot:
        await tui_send(pilot, app, "hi")
        notes = texts(app, "note")
        assert EXPECTED in notes  # the sample line, as rendered
        assert "\n" not in EXPECTED
        assert not [n for n in notes if n.startswith("step 9")]


async def test_the_gui_shows_the_line_as_a_plain_full_width_label(user: User) -> None:
    config = GUI_CONFIG.model_copy(update={"agents": [AGENT]})
    register_page(
        config,
        GUI_MODEL,
        router_factory_for(config, GuiStepFactory(steps())),
        False,
        tools=registry(StubTool("web_fetch")),
    )
    await user.open("/")
    await send(user, "hi")
    await wait_for(lambda: bool(elements(user, "status")))
    labels = [e for e in elements(user, "status") if e.text.startswith("step 1: tool")]
    (line,) = labels
    assert isinstance(line, ui.label)  # never ui.html or markdown
    assert line.text == EXPECTED
    assert {"whitespace-pre-wrap", "text-grey", "llm-secondary"} <= set(line.classes)
    assert not [c for c in line.classes if re.match(r"(max-w|mx-|h-\d)", c)]
    assert "w-full" in elements(user, "log")[0].classes


def test_the_gui_status_code_adds_no_narrow_column_or_fixed_height() -> None:
    source = Path(gui_app.__file__).read_text()
    assert "max-w-" not in source and "mx-auto" not in source
