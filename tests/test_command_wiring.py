"""`run_command` wiring: registry, config, the approval gate, subagents, event loops.

Offline. Real `sys.executable -c` children; every await is bounded by
`asyncio.wait_for(..., timeout=5)`.
"""

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from nicegui import ui
from nicegui.testing import User
from pydantic import ValidationError
from textual.app import App, ComposeResult
from textual.widgets import Button

import llm_harness.command_executor as command_executor_module
from llm_harness.builtin_tools import build_tool_registry
from llm_harness.command_executor import CommandRequest, PermissionsExecutor
from llm_harness.command_tool import RunCommandTool
from llm_harness.config import (
    CONFIG_FILE,
    AgentConfig,
    ConfigError,
    load_config,
)
from llm_harness.tools import ToolContext, ToolRegistry
from tests import test_subagents
from tests.fake_steps import reply_events
from tests.test_cli import run_cli
from tests.test_cli_orchestrator import write_config_dir
from tests.test_orchestrator import (
    RESPOND,
    SET_GOAL,
    call,
    make,
    observation,
    run,
)
from tests.test_tool_approval import FakeApprover, approval_of

BOUND = 5
PRINT_HI = [sys.executable, "-c", "print('hi')"]
CTX = ToolContext(
    storage=None,
    user_id=None,
    agent_id="orchestrator",
    tags=frozenset({"orchestrator"}),
    allowed_tools=frozenset({"run_command"}),
)


def orchestrator_agent(**fields: Any) -> AgentConfig:
    return AgentConfig(
        id="orchestrator",
        name="Orchestrator",
        description="Leads.",
        tags=["orchestrator"],
        model_tags=[],
        **fields,
    )


def scripted_command(approver: object | None, **arguments: object) -> tuple[Any, Any]:
    tools = build_tool_registry([], run_command=True)
    return make(
        [
            reply_events(call("run_command", **arguments), SET_GOAL),
            reply_events(RESPOND),
        ],
        tools=tools,
        approver=approver,
    )


def creates(marker: Path) -> list[str]:
    """An allowed command whose only effect is to create `marker`."""
    return [sys.executable, "-c", f"open({str(marker)!r}, 'w').write('x')"]


# --- (a) default off, on, and the hint -----------------------------------------


def test_the_registry_has_no_run_command_by_default() -> None:
    assert build_tool_registry([]).get("run_command") is None


def test_the_command_modules_are_not_imported_when_it_is_off() -> None:
    code = (
        "import sys\n"
        "from llm_harness.builtin_tools import build_tool_registry\n"
        "build_tool_registry([])\n"
        "bad = [m for m in sys.modules if m in {'llm_harness.command_tool',"
        " 'llm_harness.command_executor', 'llm_harness.command_policy'}]\n"
        "print(bad)\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", code],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert done.stdout.strip() == "[]", done


def test_run_command_is_registered_with_risk_exec_and_the_orchestrator_tag() -> None:
    tool = build_tool_registry([], run_command=True).get("run_command")
    assert isinstance(tool, RunCommandTool)
    assert (tool.risk, tool.tags) == ("exec", ["orchestrator"])


def test_an_injected_executor_replaces_the_real_one() -> None:
    class Refusing:
        def check(self, argv: object, cwd: object) -> str:
            return "refused by the fake"

        async def execute(self, request: CommandRequest) -> object:
            raise AssertionError("never executed")

    tool = build_tool_registry([], run_command=True, command_executor=Refusing()).get(
        "run_command"
    )
    with pytest.raises(ValidationError, match="refused by the fake"):
        tool.input_model(argv=["x"])


def test_naming_run_command_while_disabled_gives_the_enable_hint() -> None:
    agent = orchestrator_agent(tools=["run_command"])
    with pytest.raises(ConfigError) as caught:
        build_tool_registry([agent])
    message = str(caught.value)
    assert "unknown tool 'run_command'" in message
    assert message.endswith("; enable it with [tools] run_command = true")
    build_tool_registry([agent], run_command=True)  # and it is accepted when on


def test_another_unknown_tool_gets_no_hint() -> None:
    with pytest.raises(ConfigError) as caught:
        build_tool_registry([orchestrator_agent(tools=["nope"])])
    assert "enable it" not in str(caught.value)


def test_the_orchestrator_gets_the_tool_by_tag_and_other_agents_do_not() -> None:
    tools = build_tool_registry([], run_command=True)
    researcher = AgentConfig(
        id="researcher",
        name="R",
        description="d",
        tags=["research"],
        model_tags=[],
    )
    assert "run_command" in [t.name for t in tools.tools_for(orchestrator_agent())]
    assert "run_command" not in [t.name for t in tools.tools_for(researcher)]


# --- (b) config -> registry through the real cli call site ---------------------

CONFIG_WITH_COMMANDS = """\
[providers.local]
type = "ollama"
base_url = "http://localhost:11434"

[tools]
run_command = true
command_allow = ["python*", "git"]
command_deny = ["git"]
command_timeout_seconds = 10
"""


def tools_given_to_the_gui(tmp_path: Path, config_text: str) -> ToolRegistry:
    directory = write_config_dir(tmp_path)
    (directory / CONFIG_FILE).write_text(config_text)
    (directory / CONFIG_FILE).chmod(0o600)
    seen: list[dict[str, Any]] = []

    def gui(*args: Any, **kwargs: Any) -> int:
        seen.append(kwargs)
        return 0

    result = run_cli(["--gui"], directory, gui_runner=gui, data_dir=tmp_path / "data")
    assert (result.code, result.err) == (0, "")
    return seen[0]["tools"]


def test_a_config_toml_reaches_the_registry_through_the_cli(tmp_path: Path) -> None:
    tools = tools_given_to_the_gui(tmp_path, CONFIG_WITH_COMMANDS)
    tool = tools.get("run_command")
    assert tool is not None and tool.risk == "exec"
    model = tool.input_model
    with pytest.raises(ValidationError, match="at most 10"):
        model(argv=PRINT_HI, timeout_seconds=11)
    assert model(argv=PRINT_HI, timeout_seconds=10).timeout_seconds == 10
    assert model(argv=PRINT_HI).timeout_seconds == 10  # the configured maximum
    with pytest.raises(ValidationError, match="denied by `\\[tools\\] command_deny`"):
        model(argv=["git", "status"])  # deny beats allow
    with pytest.raises(ValidationError, match="not in `\\[tools\\] command_allow`"):
        model(argv=["ls"])


def test_without_the_key_the_cli_registers_no_run_command(tmp_path: Path) -> None:
    config_text = CONFIG_WITH_COMMANDS.split("[tools]")[0]
    assert tools_given_to_the_gui(tmp_path, config_text).get("run_command") is None


def test_the_load_config_values_are_what_the_cli_passes(tmp_path: Path) -> None:
    directory = write_config_dir(tmp_path)
    (directory / CONFIG_FILE).write_text(CONFIG_WITH_COMMANDS)
    tools_config = load_config(directory).tools
    assert (
        tools_config.run_command,
        tools_config.command_allow,
        tools_config.command_deny,
        tools_config.command_timeout_seconds,
    ) == (True, ["python*", "git"], ["git"], 10)


# --- (c) through the real Orchestrator._call_tool ------------------------------


async def test_a_denied_command_returns_invalid_arguments_and_never_asks() -> None:
    approver = FakeApprover(True)
    orchestrator, provider = scripted_command(approver, argv=["bash", "-c", "true"])
    await asyncio.wait_for(run(orchestrator, "hi"), BOUND)
    text = observation(provider.requests[1])
    assert text.startswith("invalid arguments for tool run_command: ")
    assert "denied by the built-in deny list" in text
    assert approver.requests == []
    (span,) = [s for s in orchestrator.last_trace if s.kind == "tool_call"]
    assert span.status == "error" and "approval" not in span.attributes


async def test_an_approved_command_asks_once_with_the_exact_argv_and_runs() -> None:
    approver = FakeApprover(True)
    orchestrator, provider = scripted_command(approver, argv=PRINT_HI)
    await asyncio.wait_for(run(orchestrator, "hi"), BOUND)
    (request,) = approver.requests
    assert request.risk == "exec" and request.tool == "run_command"
    assert request.arguments_text == json.dumps(
        {"argv": PRINT_HI, "cwd": None, "timeout_seconds": 30}, sort_keys=True
    )
    text = observation(provider.requests[1])
    assert text.startswith("tool run_command result: exit=0 ")
    assert "<command-output>\nhi\n</command-output>" in text
    assert approval_of(orchestrator) == ("approved", "ok")


async def test_a_command_the_human_denies_is_not_run(tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    approver = FakeApprover(False)
    orchestrator, provider = scripted_command(approver, argv=creates(marker))
    await asyncio.wait_for(run(orchestrator, "hi"), BOUND)
    assert len(approver.requests) == 1
    assert observation(provider.requests[1]) == (
        "tool run_command was not run: the user denied it"
    )
    assert not marker.exists()
    assert approval_of(orchestrator) == ("denied", "rejected")


async def test_with_no_approver_the_command_is_not_run(tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    orchestrator, provider = scripted_command(None, argv=creates(marker))
    await asyncio.wait_for(run(orchestrator, "hi"), BOUND)
    assert observation(provider.requests[1]) == (
        "tool run_command was not run: it needs the user's approval "
        "and none is available here"
    )
    assert not marker.exists()
    assert approval_of(orchestrator) == ("unavailable", "rejected")


async def test_the_marker_command_does_create_the_file_when_approved(
    tmp_path: Path,
) -> None:
    """Proves the file-creation probe above can fail: here it must succeed."""
    marker = tmp_path / "ran"
    orchestrator, _ = scripted_command(FakeApprover(True), argv=creates(marker))
    await asyncio.wait_for(run(orchestrator, "hi"), BOUND)
    assert marker.read_text() == "x"


# --- (d) a subagent can never run it -------------------------------------------


async def test_a_subagent_that_names_run_command_gets_no_approval_and_nothing_runs(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "ran"
    tools = build_tool_registry([], run_command=True)
    script = [
        reply_events(call("run_command", argv=creates(marker)), SET_GOAL),
        reply_events(test_subagents.RESPOND),
    ]
    child, provider = test_subagents.make(script, tools=tools)
    # CHILD lists only "search"; the tool is reachable only when named, so name it.
    child._orchestrator._agent = test_subagents.CHILD.model_copy(
        update={"tools": ["run_command"]}
    )
    await asyncio.wait_for(test_subagents.drain(child), BOUND)
    assert test_subagents.observation(provider.requests[1]) == (
        "tool run_command was not run: it needs the user's approval "
        "and none is available here"
    )
    assert not marker.exists()


# --- (f) event loops -----------------------------------------------------------

OK = [sys.executable, "-c", "print('ok')"]
FLOOD_THEN_SLEEP = [
    sys.executable,
    "-c",
    (
        "import sys, time\n"
        "sys.stdout.write('x' * 5_000_000); sys.stdout.flush()\n"
        "time.sleep(30)"
    ),
]


def real_tool() -> RunCommandTool:
    return RunCommandTool(PermissionsExecutor(), 30)


async def exercise(tool: RunCommandTool) -> dict[str, Any]:
    """Run the tool for real on the CURRENT loop; report what was seen."""
    seen: dict[str, Any] = {"loop": type(asyncio.get_running_loop()).__module__}
    ok = await asyncio.wait_for(
        tool.run(tool.input_model(argv=OK, timeout_seconds=5), CTX), BOUND
    )
    seen["ok"] = ok
    transports: list[tuple[str, bool]] = []
    real_close = command_executor_module._close_pipes

    def recording(proc: Any) -> None:
        transport = getattr(proc, "_transport", None)
        real_close(proc)
        transports.append(
            (
                type(transport).__module__,
                transport is not None and transport.is_closing(),
            )
        )

    command_executor_module._close_pipes = recording
    try:
        seen["flood"] = await asyncio.wait_for(
            tool.run(tool.input_model(argv=FLOOD_THEN_SLEEP, timeout_seconds=1), CTX),
            BOUND,
        )
    finally:
        command_executor_module._close_pipes = real_close
    seen["transports"] = transports
    return seen


def check_seen(seen: dict[str, Any]) -> None:
    assert seen["ok"].startswith("exit=0 ")
    assert "<command-output>\nok\n</command-output>" in seen["ok"]
    assert "exit=killed:timeout" in seen["flood"]
    assert "output_bytes=5000000" in seen["flood"]
    # _close_pipes found the private `_transport` on this loop. asyncio's transport
    # reports closing afterwards; uvloop's reports False (probe: close() is accepted,
    # and uvloop's `wait()` does not hang on a paused pipe even without the close).
    assert len(seen["transports"]) >= 1
    if seen["loop"].startswith("asyncio"):
        assert all(closing for _, closing in seen["transports"]), seen["transports"]


class ProbeApp(App[None]):
    """A Textual app whose button handler runs the tool on the app's own loop."""

    def __init__(self) -> None:
        super().__init__()
        self.seen: dict[str, Any] | None = None

    def compose(self) -> ComposeResult:
        yield Button("go", id="go")

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        self.seen = await exercise(real_tool())


async def test_the_executor_works_in_a_textual_handler() -> None:
    app = ProbeApp()
    async with app.run_test() as pilot:
        await pilot.click("#go")
        async with asyncio.timeout(BOUND * 3):
            while app.seen is None:
                await pilot.pause(0.05)
    check_seen(app.seen)
    assert app.seen["loop"].startswith("asyncio"), app.seen["loop"]


async def test_the_executor_works_in_a_nicegui_click_handler(user: User) -> None:
    seen: list[dict[str, Any]] = []

    @ui.page("/command-probe")
    def page() -> None:
        async def handler() -> None:
            seen.append(await exercise(real_tool()))

        ui.button("go", on_click=handler)

    await user.open("/command-probe")
    user.find("go").click()
    async with asyncio.timeout(BOUND * 3):
        while not seen:
            await asyncio.sleep(0.05)
    check_seen(seen[0])


def test_the_executor_works_on_the_loop_uvicorn_picks_for_the_gui() -> None:
    """`ui.run` serves with uvicorn `loop="auto"`: uvloop when it is installed."""
    from uvicorn.config import Config

    factory = Config(app="x:y", loop="auto").get_loop_factory()
    assert factory is not None
    with asyncio.Runner(loop_factory=factory) as runner:
        seen = runner.run(exercise(real_tool()))
    check_seen(seen)
    assert seen["loop"].startswith("uvloop"), seen["loop"]


def test_the_default_asyncio_loop_still_works() -> None:
    with asyncio.Runner() as runner:
        seen = runner.run(exercise(real_tool()))
    check_seen(seen)
    assert seen["loop"].startswith("asyncio")
