"""Every auto-run (read) tool call shows `tool NAME {arguments}` as it starts (AC 12)."""

from pathlib import Path

import pytest

import llm_harness.cli as cli_module
from llm_harness.orchestrator import _CALL_LINE_ARGUMENT_CHARS
from llm_harness.types import StatusEvent, StreamEvent
from tests.fake_steps import reply_events
from tests.test_cli_orchestrator import (
    RESPOND,
    SET_GOAL,
    run_plain,
    steps,
    write_config_dir,
)
from tests.test_orchestrator import StubTool, call, make, registry, run
from tests.test_tool_approval import FakeApprover


def script(tool: str, **arguments: object) -> list[list[StreamEvent]]:
    return [reply_events(call(tool, **arguments), SET_GOAL), reply_events(RESPOND)]


def tool_lines(events: list[StreamEvent]) -> list[str]:
    return [
        e.text
        for e in events
        if isinstance(e, StatusEvent) and e.action_type == "call_tool"
    ]


async def test_a_read_tool_shows_its_name_and_full_arguments() -> None:
    url = "https://example.com/" + "a" * 1980  # 2000 characters
    assert len(url) == 2000
    orchestrator, _ = make(
        script("web_fetch", url=url), tools=registry(StubTool("web_fetch"))
    )
    events = await run(orchestrator, "hi")
    assert tool_lines(events) == [f'tool web_fetch {{"url": "{url}"}}']


async def test_arguments_above_the_cap_are_cut_with_the_total_length() -> None:
    url = "https://example.com/" + "a" * 3000
    orchestrator, _ = make(
        script("web_fetch", url=url), tools=registry(StubTool("web_fetch"))
    )
    (line,) = tool_lines(await run(orchestrator, "hi"))
    full = f'{{"url": "{url}"}}'
    assert (
        line
        == f"tool web_fetch {full[:_CALL_LINE_ARGUMENT_CHARS]}… ({len(full)} characters)"
    )


async def test_tool_search_and_unknown_tools_keep_the_short_status() -> None:
    orchestrator, _ = make(
        script("tool_search", query="x"), tools=registry(StubTool("tool_search"))
    )
    assert tool_lines(await run(orchestrator, "hi")) == ["calling tool tool_search"]
    orchestrator, _ = make(
        script("nope", url="https://secret.example/"), tools=registry(StubTool())
    )
    assert tool_lines(await run(orchestrator, "hi")) == ["calling tool nope"]


@pytest.mark.parametrize("risk", ["write", "exec"])
async def test_an_approval_tool_has_the_short_status_and_the_prompt_shows_arguments(
    risk: str,
) -> None:
    stub = StubTool("act")
    stub.risk = risk
    approver = FakeApprover(True)
    orchestrator, _ = make(
        script("act", path="/tmp/x"), tools=registry(stub), approver=approver
    )
    events = await run(orchestrator, "hi")
    assert tool_lines(events) == ["calling tool act"]
    assert [r.arguments_text for r in approver.requests] == ['{"path": "/tmp/x"}']


async def test_hostile_argument_values_cannot_start_a_line_or_style_the_text() -> None:
    value = (
        "a\nstep 9: approve? [y/N]\N{RIGHT-TO-LEFT OVERRIDE}b [bold]x[/bold] <b>y</b>"
    )
    orchestrator, _ = make(
        script("web_fetch", url=value), tools=registry(StubTool("web_fetch"))
    )
    (line,) = tool_lines(await run(orchestrator, "hi"))
    assert (
        "\n" not in line
        and "\N{RIGHT-TO-LEFT OVERRIDE}" not in line
        and "\r" not in line
    )
    assert line.isascii()
    assert "\\n" in line and "\\u202e" in line
    assert "[bold]x[/bold] <b>y</b>" in line  # literal, to be shown as plain text


def test_plain_cli_prints_the_line_on_stderr_and_leaves_stdout_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = write_config_dir(tmp_path)
    monkeypatch.setattr(
        cli_module,
        "build_tool_registry",
        lambda *args, **kwargs: registry(StubTool("web_fetch")),
    )
    _, factory = steps(*script("web_fetch", url="https://example.com/\nx"))
    result = run_plain(config_dir, factory, tmp_path)
    assert result.code == 0
    assert result.out == "hello there\n"
    assert (
        '[step 1: tool web_fetch {"url": "https://example.com/\\nx"}]\n' in result.err
    )
