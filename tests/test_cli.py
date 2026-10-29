import asyncio
import io
import logging
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import llm_harness.store as store_module
from llm_harness.cli import _chat_loop, _read_line, _run_turn, main, resolve_think
from llm_harness.config import (
    AGENTS_FILE,
    CONFIG_FILE,
    EXAMPLE_AGENTS_YAML,
    EXAMPLE_CONFIG_TOML,
    EXAMPLE_MODELS_YAML,
    MODELS_FILE,
    ConfigError,
    HarnessConfig,
    ModelConfig,
    ProviderConfig,
    load_config,
)
from llm_harness.orchestrator import Orchestrator
from llm_harness.providers.base import ChatProvider
from llm_harness.router import RouterFactory
from llm_harness.storage import Storage
from llm_harness.store import CallRecord, Store
from llm_harness.subagents import SubagentFactoryMaker
from llm_harness.tools import ToolRegistry
from llm_harness.types import (
    ChatRequest,
    Done,
    ErrorEvent,
    Message,
    ModelSwitch,
    Notice,
    StreamEvent,
    TextDelta,
    ThinkingDelta,
    Usage,
    UsageEvent,
    UsageReport,
)
from tests.fake_steps import as_step_reply
from tests.fakes import ScriptedProvider as RawScriptedProvider


class ScriptedProvider(RawScriptedProvider):
    """Serves plain-text replies as valid orchestrator step replies.

    `--plain` always drives an Orchestrator, whose model answers in JSON. Wrapping
    at the provider seam keeps these tests about the plain renderer and the router
    wiring; test_cli_orchestrator.py covers the JSON replies themselves.
    """

    def __init__(self, events: list[StreamEvent], **kwargs: Any) -> None:
        super().__init__(as_step_reply(events), **kwargs)


STEP_MODEL = ModelConfig(
    id="m", name="M", provider="p", model="pm", tags=["orchestrator"], priority=1
)
CANARY_KEY = "sk-CANARY-123456789"
CANARY_PROMPT = "PROMPT-CANARY-42"

CONFIG_TOML = """\
[providers.local]
type = "ollama"
base_url = "http://localhost:11434"
"""

MODELS_YAML = """\
models:
  - id: m1
    name: M1
    provider: local
    model: provider-model-id
    tags: [default, orchestrator]
    priority: 1
  - id: m2
    name: M2
    provider: local
    model: other-model-id
    tags: []
    priority: 1
"""


class TtyStringIO(io.StringIO):
    def __init__(self, initial: str = "", *, tty: bool = False) -> None:
        super().__init__(initial)
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


class Run:
    def __init__(self, code: int, out: str, err: str) -> None:
        self.code = code
        self.out = out
        self.err = err


def run_cli(
    argv: list[str],
    config_dir: Path,
    *,
    stdin: str = "",
    stdin_tty: bool = False,
    stdout_tty: bool = False,
    stderr_tty: bool = False,
    factory: Callable[[ProviderConfig], ChatProvider] | None = None,
    tui_runner: Callable[..., int] | None = None,
    gui_runner: Callable[..., int] | None = None,
    data_dir: Path | None = None,
) -> Run:
    out, err = TtyStringIO(tty=stdout_tty), TtyStringIO(tty=stderr_tty)
    kwargs: dict[str, Any] = {}
    if data_dir is not None:
        kwargs["data_dir"] = data_dir
    if factory is not None:
        kwargs["provider_factory"] = factory
    if tui_runner is not None:
        kwargs["run_tui"] = tui_runner
    if gui_runner is not None:
        kwargs["run_gui"] = gui_runner
    code = main(
        argv,
        config_dir=config_dir,
        stdin=TtyStringIO(stdin, tty=stdin_tty),
        stdout=out,
        stderr=err,
        **kwargs,
    )
    return Run(code, out.getvalue(), err.getvalue())


AGENTS_YAML = """\
agents:
  - id: orchestrator
    name: Orchestrator
    description: Reads the prompt and assembles the reply.
    tags: [orchestrator]
    model_tags: []
"""


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "cfg"
    directory.mkdir(mode=0o700)
    directory.chmod(0o700)  # independent of the umask
    (directory / CONFIG_FILE).write_text(CONFIG_TOML)
    (directory / CONFIG_FILE).chmod(0o600)
    (directory / MODELS_FILE).write_text(MODELS_YAML)
    (directory / AGENTS_FILE).write_text(AGENTS_YAML)
    return directory


def reply_events(*extra: StreamEvent) -> list[StreamEvent]:
    return [*extra, Done(finish_reason="stop")]


def scripted(*events: StreamEvent) -> tuple[ScriptedProvider, Callable]:
    provider = ScriptedProvider(list(events))
    return provider, lambda _config: provider


FULL_REPLY = (
    ThinkingDelta(text="pondering"),
    TextDelta(text="Hel"),
    TextDelta(text="lo"),
    UsageEvent(usage=Usage(input_tokens=3, output_tokens=7)),
    Done(finish_reason="stop"),
)


# Mode selection


def test_no_flags_without_a_terminal_points_to_plain_and_touches_no_config(
    tmp_path: Path,
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    calls: list[tuple[Any, ...]] = []
    result = run_cli([], empty, tui_runner=lambda *args: calls.append(args) or 0)
    assert result.code == 2
    assert "needs a terminal" in result.err
    assert "--plain" in result.err
    assert list(empty.iterdir()) == []
    assert calls == []


@pytest.mark.parametrize(("stdin_tty", "stdout_tty"), [(True, False), (False, True)])
def test_tui_needs_both_stdin_and_stdout_to_be_terminals(
    tmp_path: Path, stdin_tty: bool, stdout_tty: bool
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    result = run_cli([], empty, stdin_tty=stdin_tty, stdout_tty=stdout_tty)
    assert result.code == 2
    assert "--plain" in result.err
    assert list(empty.iterdir()) == []


class FakeTuiRunner:
    def __init__(self, code: int = 0) -> None:
        self.code = code
        self.calls: list[tuple[HarnessConfig, ModelConfig, RouterFactory, bool]] = []
        self.storages: list[Storage | None] = []
        self.tools: list[ToolRegistry | None] = []
        self.subagent_factory_makers: list[SubagentFactoryMaker | None] = []

    def __call__(
        self,
        config: HarnessConfig,
        model: ModelConfig,
        router_factory: RouterFactory,
        is_manual: bool,
        *,
        storage: Storage | None = None,
        tools: ToolRegistry | None = None,
        subagent_factory_maker: SubagentFactoryMaker | None = None,
    ) -> int:
        self.calls.append((config, model, router_factory, is_manual))
        self.storages.append(storage)
        self.tools.append(tools)
        self.subagent_factory_makers.append(subagent_factory_maker)
        return self.code


def first_request_think(
    router_factory: RouterFactory, model: ModelConfig, provider: ScriptedProvider
) -> bool | None:
    """Run one turn through a router the runner got; the think value the provider saw."""

    async def turn() -> None:
        router = router_factory(model, False)
        request = ChatRequest(
            model=model.model, messages=[Message(role="user", content="hi")]
        )
        try:
            async for _ in router.stream_chat(request):
                pass
        finally:
            await router.aclose()

    asyncio.run(turn())
    return provider.requests[0].think


def run_tui_cli(
    config_dir: Path, argv: list[str], runner: FakeTuiRunner, factory: Any = None
) -> Run:
    return run_cli(
        argv,
        config_dir,
        stdin_tty=True,
        stdout_tty=True,
        tui_runner=runner,
        factory=factory,
    )


@pytest.mark.parametrize(
    ("flags", "expected"), [([], None), (["--think"], True), (["--no-think"], False)]
)
def test_no_flags_on_a_terminal_starts_the_tui_with_the_loaded_inputs(
    config_dir: Path, flags: list[str], expected: bool | None
) -> None:
    runner = FakeTuiRunner(code=0)
    provider, factory = scripted(*reply_events())
    result = run_tui_cli(config_dir, flags, runner, factory)
    assert result.code == 0
    ((config, model, router_factory, is_manual),) = runner.calls
    assert config == load_config(config_dir)
    assert model.id == "m1"
    assert is_manual is False
    assert first_request_think(router_factory, model, provider) is expected


def test_tui_model_flag_is_passed_on_as_a_manual_pick(config_dir: Path) -> None:
    runner = FakeTuiRunner()
    run_tui_cli(config_dir, ["--model", "m1"], runner)
    assert runner.calls[0][3] is True


def test_tui_exit_code_is_returned(config_dir: Path) -> None:
    assert run_tui_cli(config_dir, [], FakeTuiRunner(code=7)).code == 7


def test_tui_receives_the_model_flag_and_builds_no_provider_itself(
    config_dir: Path,
) -> None:
    runner = FakeTuiRunner()
    built: list[ProviderConfig] = []

    def factory(config: ProviderConfig) -> ChatProvider:
        built.append(config)
        raise AssertionError("main must leave provider building to the TUI")

    run_tui_cli(config_dir, ["--model", "m2"], runner, factory)
    assert runner.calls[0][1].id == "m2"
    assert built == []


def test_tui_path_reports_an_unknown_model_without_starting(config_dir: Path) -> None:
    runner = FakeTuiRunner()
    result = run_tui_cli(config_dir, ["--model", "nope"], runner)
    assert result.code == 1
    assert "m1" in result.err and "m2" in result.err
    assert runner.calls == []


def with_embedding_model(config_dir: Path) -> None:
    models = config_dir / MODELS_FILE
    models.write_text(
        models.read_text()
        + """\
  - id: bge-m3-local
    name: BGE M3
    provider: local
    model: bge-m3
    tags: [embedding]
    priority: 1
"""
    )


def test_model_flag_naming_an_embedding_model_fails_before_any_chat(
    config_dir: Path,
) -> None:
    with_embedding_model(config_dir)
    runner = FakeTuiRunner()
    result = run_tui_cli(config_dir, ["--model", "bge-m3-local"], runner)
    assert result.code == 1
    assert result.err.startswith("error:")
    assert "bge-m3-local" in result.err and "embedding" in result.err
    assert runner.calls == []


def test_plain_model_flag_naming_an_embedding_model_fails_without_a_provider(
    config_dir: Path,
) -> None:
    with_embedding_model(config_dir)

    def factory(config: ProviderConfig) -> ChatProvider:
        raise AssertionError("no provider may be built")

    result = run_cli(
        ["--plain", "--model", "bge-m3-local"],
        config_dir,
        stdin="hi\n",
        factory=factory,
    )
    assert result.code == 1
    assert result.err.startswith("error:")
    assert "bge-m3-local" in result.err and "embedding" in result.err


def test_unknown_model_flag_keeps_its_message_with_an_embedding_entry(
    config_dir: Path,
) -> None:
    with_embedding_model(config_dir)
    result = run_tui_cli(config_dir, ["--model", "nope"], FakeTuiRunner())
    assert result.code == 1
    assert result.err.startswith("error: unknown model 'nope'; valid ids: m1, m2")


def test_model_flag_naming_a_chat_model_still_works_with_an_embedding_entry(
    config_dir: Path,
) -> None:
    with_embedding_model(config_dir)
    runner = FakeTuiRunner()
    result = run_tui_cli(config_dir, ["--model", "m1"], runner)
    assert result.code == 0
    assert [model.id for _, model, _, _ in runner.calls] == ["m1"]


def test_tui_path_warns_about_loose_config_permissions(config_dir: Path) -> None:
    (config_dir / CONFIG_FILE).chmod(0o644)
    runner = FakeTuiRunner()
    result = run_tui_cli(config_dir, [], runner)
    assert "chmod 600" in result.err
    assert len(runner.calls) == 1


def test_tui_path_invalid_config_is_an_error_line(config_dir: Path) -> None:
    (config_dir / CONFIG_FILE).write_text("not = [valid")
    runner = FakeTuiRunner()
    result = run_tui_cli(config_dir, [], runner)
    assert result.code == 1
    assert result.err.startswith("error: ")
    assert runner.calls == []


@pytest.mark.parametrize("answer", ["n\n", ""])
def test_tui_first_run_declined_prints_examples_and_writes_nothing(
    tmp_path: Path, answer: str
) -> None:
    target = tmp_path / "new"
    runner = FakeTuiRunner()
    result = run_cli(
        [],
        target,
        stdin=answer,
        stdin_tty=True,
        stdout_tty=True,
        tui_runner=runner,
    )
    assert result.code == 1
    assert EXAMPLE_CONFIG_TOML in result.err
    assert EXAMPLE_MODELS_YAML in result.err
    assert EXAMPLE_AGENTS_YAML in result.err
    assert not target.exists()
    assert runner.calls == []


def test_tui_first_run_yes_writes_examples_like_plain(tmp_path: Path) -> None:
    target = tmp_path / "new"
    runner = FakeTuiRunner()
    result = run_cli(
        [],
        target,
        stdin="y\n",
        stdin_tty=True,
        stdout_tty=True,
        tui_runner=runner,
    )
    assert result.code == 0
    assert (target / MODELS_FILE).is_file()
    assert (target / AGENTS_FILE).is_file()
    assert runner.calls == []


def _imported_modules_after(code: str, *args: str) -> set[str]:
    """Run `code` in an isolated interpreter; it must print the sys.modules names."""
    result = subprocess.run(
        [sys.executable, "-I", "-c", code, *args],
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    return set(result.stdout.split())


def test_importing_the_cli_and_help_do_not_import_textual() -> None:
    code = (
        "import sys\n"
        "import llm_harness.cli as cli\n"
        "try:\n"
        "    cli.main(['--help'])\n"
        "except SystemExit:\n"
        "    pass\n"
        "print(*sys.modules)\n"
    )
    modules = _imported_modules_after(code)
    assert "llm_harness.cli" in modules
    assert "textual" not in modules


def test_importing_the_cli_help_and_plain_do_not_import_nicegui(
    tmp_path: Path,
) -> None:
    code = (
        "import sys\n"
        "from pathlib import Path\n"
        "import llm_harness.cli as cli\n"
        "try:\n"
        "    cli.main(['--help'])\n"
        "except SystemExit:\n"
        "    pass\n"
        "# An empty config dir: --plain prints the first-run help and exits 1.\n"
        "assert cli.main(['--plain'], config_dir=Path(sys.argv[1])) == 1\n"
        "print(*sys.modules)\n"
    )
    modules = _imported_modules_after(code, str(tmp_path / "empty"))
    assert "llm_harness.cli" in modules
    assert "nicegui" not in modules


def test_the_tui_module_does_not_import_nicegui() -> None:
    code = "import sys\nimport llm_harness.tui.app\nprint(*sys.modules)\n"
    modules = _imported_modules_after(code)
    assert "textual" in modules
    assert "nicegui" not in modules


def test_the_default_gui_runner_delegates_to_run_gui(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import llm_harness.gui.app as gui_app

    calls: list[tuple[Any, ...]] = []
    keywords: list[dict[str, Any]] = []
    monkeypatch.setattr(
        gui_app,
        "run_gui",
        lambda *args, **kwargs: calls.append(args) or keywords.append(kwargs) or 5,
    )
    provider, factory = scripted(*reply_events())
    result = run_cli(["--gui", "--no-think"], config_dir, factory=factory)
    assert result.code == 5
    ((config, model, router_factory, is_manual),) = calls
    assert config == load_config(config_dir)
    assert model.id == "m1"
    assert is_manual is False
    assert first_request_think(router_factory, model, provider) is False
    assert isinstance(keywords[0]["storage"], Storage)
    assert keywords[0]["tools"].names() == ["tool_search"]
    assert callable(keywords[0]["subagent_factory_maker"])


def test_the_default_tui_runner_delegates_to_run_tui_with_the_registry(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import llm_harness.tui.app as tui_app

    keywords: list[dict[str, Any]] = []
    monkeypatch.setattr(
        tui_app, "run_tui", lambda *args, **kwargs: keywords.append(kwargs) or 6
    )
    _, factory = scripted(*reply_events())
    result = run_cli(
        ["--no-think"],
        config_dir,
        factory=factory,
        stdin_tty=True,
        stdout_tty=True,
    )
    assert result.code == 6
    assert isinstance(keywords[0]["storage"], Storage)
    assert keywords[0]["tools"].names() == ["tool_search"]
    assert callable(keywords[0]["subagent_factory_maker"])


def run_gui_cli(
    config_dir: Path,
    argv: list[str],
    runner: FakeTuiRunner,
    factory: Any = None,
    **kwargs: Any,
) -> Run:
    return run_cli(
        ["--gui", *argv], config_dir, gui_runner=runner, factory=factory, **kwargs
    )


@pytest.mark.parametrize(
    ("flags", "expected"), [([], None), (["--think"], True), (["--no-think"], False)]
)
def test_gui_flag_starts_the_gui_with_the_loaded_inputs(
    config_dir: Path, flags: list[str], expected: bool | None
) -> None:
    runner = FakeTuiRunner(code=0)
    provider, factory = scripted(*reply_events())
    result = run_gui_cli(config_dir, flags, runner, factory)
    assert result.code == 0
    ((config, model, router_factory, is_manual),) = runner.calls
    assert config == load_config(config_dir)
    assert model.id == "m1"
    assert is_manual is False
    assert first_request_think(router_factory, model, provider) is expected


def test_gui_model_flag_is_passed_on_as_a_manual_pick(config_dir: Path) -> None:
    runner = FakeTuiRunner()
    run_gui_cli(config_dir, ["--model", "m1"], runner)
    assert runner.calls[0][3] is True


def test_gui_exit_code_is_returned(config_dir: Path) -> None:
    assert run_gui_cli(config_dir, [], FakeTuiRunner(code=7)).code == 7


def test_gui_works_without_a_terminal(config_dir: Path) -> None:
    runner = FakeTuiRunner()
    result = run_gui_cli(config_dir, [], runner, stdin_tty=False, stdout_tty=False)
    assert result.code == 0
    assert len(runner.calls) == 1
    assert "needs a terminal" not in result.err


def test_gui_does_not_start_the_tui(config_dir: Path) -> None:
    tui_runner, gui_runner = FakeTuiRunner(), FakeTuiRunner()
    run_cli(["--gui"], config_dir, tui_runner=tui_runner, gui_runner=gui_runner)
    assert tui_runner.calls == []
    assert len(gui_runner.calls) == 1


def test_gui_receives_the_model_flag_and_builds_no_provider_itself(
    config_dir: Path,
) -> None:
    runner = FakeTuiRunner()
    built: list[ProviderConfig] = []

    def factory(config: ProviderConfig) -> ChatProvider:
        built.append(config)
        raise AssertionError("main must leave provider building to the GUI")

    run_gui_cli(config_dir, ["--model", "m2"], runner, factory)
    assert runner.calls[0][1].id == "m2"
    assert built == []


def test_gui_path_reports_an_unknown_model_without_starting(config_dir: Path) -> None:
    runner = FakeTuiRunner()
    result = run_gui_cli(config_dir, ["--model", "nope"], runner)
    assert result.code == 1
    assert "m1" in result.err and "m2" in result.err
    assert runner.calls == []


def test_gui_path_warns_about_loose_config_permissions(config_dir: Path) -> None:
    (config_dir / CONFIG_FILE).chmod(0o644)
    runner = FakeTuiRunner()
    result = run_gui_cli(config_dir, [], runner)
    assert "chmod 600" in result.err
    assert len(runner.calls) == 1


def test_gui_path_invalid_config_is_an_error_line(config_dir: Path) -> None:
    (config_dir / CONFIG_FILE).write_text("not = [valid")
    runner = FakeTuiRunner()
    result = run_gui_cli(config_dir, [], runner)
    assert result.code == 1
    assert result.err.startswith("error: ")
    assert runner.calls == []


def test_gui_first_run_non_interactive_prints_examples_and_writes_nothing(
    tmp_path: Path,
) -> None:
    target = tmp_path / "new"
    runner = FakeTuiRunner()
    result = run_gui_cli(target, [], runner)
    assert result.code == 1
    assert EXAMPLE_CONFIG_TOML in result.err
    assert EXAMPLE_MODELS_YAML in result.err
    assert EXAMPLE_AGENTS_YAML in result.err
    assert not target.exists()
    assert runner.calls == []


def test_gui_first_run_yes_writes_examples_like_plain(tmp_path: Path) -> None:
    target = tmp_path / "new"
    runner = FakeTuiRunner()
    result = run_gui_cli(target, [], runner, stdin="y\n", stdin_tty=True)
    assert result.code == 0
    assert (target / MODELS_FILE).is_file()
    assert (target / AGENTS_FILE).is_file()
    assert runner.calls == []


def test_plain_and_gui_are_mutually_exclusive(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as info:
        run_cli(["--plain", "--gui"], tmp_path)
    assert info.value.code == 2


def test_help_lists_every_option(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    with pytest.raises(SystemExit) as info:
        run_cli(["--help"], tmp_path)
    assert info.value.code == 0
    help_text = capsys.readouterr().out
    for option in (
        "--plain",
        "--gui",
        "--storage-status",
        "--model",
        "--think",
        "--no-think",
    ):
        assert option in help_text


# --storage-status


def test_storage_status_prints_the_stores_and_creates_nothing(
    config_dir: Path, tmp_path: Path
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    result = run_cli(["--storage-status"], config_dir, data_dir=data)
    assert result.code == 0
    assert result.err == ""
    assert result.out.splitlines() == [
        f"relational: sqlite  {data / 'harness.db'}",
        "  file: not created yet (it is created on first use)",
        "  schema: nothing to read yet",
        f"vector: qdrant-edge  {data / 'vectors'}",
        "  not created yet",
        f"files: local  {data / 'files'}",
        "  not created yet",
    ]
    assert list(data.iterdir()) == []


def test_storage_status_needs_no_terminal_and_no_default_model(
    config_dir: Path, tmp_path: Path
) -> None:
    (config_dir / MODELS_FILE).write_text(
        MODELS_YAML.replace("[default, orchestrator]", "[]")
    )
    assert run_cli(["--plain"], config_dir, data_dir=tmp_path).code == 1
    result = run_cli(["--storage-status"], config_dir, data_dir=tmp_path)
    assert result.code == 0
    assert "relational: sqlite" in result.out


def test_storage_status_prints_the_custom_ledger_path_from_the_config(
    config_dir: Path, tmp_path: Path
) -> None:
    ledger = tmp_path / "custom" / "ledger.db"
    (config_dir / CONFIG_FILE).write_text(
        f'{CONFIG_TOML}\n[storage.relational]\npath = "{ledger}"\n'
    )
    result = run_cli(["--storage-status"], config_dir, data_dir=tmp_path / "data")
    assert result.code == 0
    assert f"relational: sqlite  {ledger}" in result.out
    assert not ledger.parent.exists()


def test_storage_status_uses_the_default_ledger_path_without_a_data_dir(
    config_dir: Path,
) -> None:
    result = run_cli(["--storage-status"], config_dir)
    assert result.code == 0
    assert f"relational: sqlite  {store_module.default_db_path()}" in result.out


def test_storage_status_with_a_broken_config_fails_with_an_error(
    config_dir: Path, tmp_path: Path
) -> None:
    (config_dir / CONFIG_FILE).write_text("[providers.local\n")
    result = run_cli(["--storage-status"], config_dir, data_dir=tmp_path)
    assert result.code == 1
    assert result.err.startswith("error: ")
    assert result.out == ""


def test_storage_status_with_a_missing_config_never_offers_to_write_examples(
    tmp_path: Path,
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    result = run_cli(
        ["--storage-status"], empty, stdin="y\n", stdin_tty=True, data_dir=tmp_path
    )
    assert result.code == 1
    assert result.out == ""
    assert "Write example files" not in result.err
    assert list(empty.iterdir()) == []


@pytest.mark.parametrize("other", ["--plain", "--gui"])
def test_storage_status_excludes_the_other_modes(other: str, tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as info:
        run_cli(["--storage-status", other], tmp_path)
    assert info.value.code == 2


def test_think_and_no_think_are_mutually_exclusive(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    with pytest.raises(SystemExit) as info:
        run_cli(["--plain", "--think", "--no-think"], tmp_path)
    assert info.value.code == 2
    assert "not allowed with" in capsys.readouterr().err


# First run


def test_first_run_non_interactive_prints_example_and_writes_nothing(
    tmp_path: Path,
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    result = run_cli(["--plain"], empty, stdin="", stdin_tty=False)
    assert result.code == 1
    assert str(empty / CONFIG_FILE) in result.err
    assert EXAMPLE_CONFIG_TOML in result.err
    assert EXAMPLE_MODELS_YAML in result.err
    assert EXAMPLE_AGENTS_YAML in result.err
    assert "[y/N]" not in result.err
    assert list(empty.iterdir()) == []


@pytest.mark.parametrize("answer", ["n\n", "\n", "no\n", ""])
def test_first_run_interactive_declined_writes_nothing(
    tmp_path: Path, answer: str
) -> None:
    target = tmp_path / "new"
    result = run_cli(["--plain"], target, stdin=answer, stdin_tty=True)
    assert result.code == 1
    assert "[y/N]" in result.err
    assert not target.exists()


def test_first_run_interactive_yes_writes_loadable_examples(tmp_path: Path) -> None:
    target = tmp_path / "new"
    result = run_cli(["--plain"], target, stdin="y\n", stdin_tty=True)
    assert result.code == 0
    assert "Review" in result.err
    assert stat.S_IMODE((target / CONFIG_FILE).stat().st_mode) == 0o600
    assert (target / MODELS_FILE).is_file()
    assert (target / AGENTS_FILE).is_file()
    config = load_config(target)
    assert [m.model for m in config.models] == ["gemma4:12b"]


def test_invalid_config_is_an_error_line(config_dir: Path) -> None:
    (config_dir / CONFIG_FILE).write_text("not = [valid")
    result = run_cli(["--plain"], config_dir)
    assert result.code == 1
    assert result.err.startswith("error: ")


# Model selection and permissions


def test_unknown_model_lists_valid_ids(config_dir: Path) -> None:
    result = run_cli(["--plain", "--model", "nope"], config_dir)
    assert result.code == 1
    assert "error: " in result.err
    assert "m1" in result.err and "m2" in result.err


def test_model_flag_overrides_default(config_dir: Path) -> None:
    provider, factory = scripted(*reply_events())
    result = run_cli(
        ["--plain", "--model", "m2"], config_dir, stdin="hi\n", factory=factory
    )
    assert result.code == 0
    assert provider.requests[0].model == "other-model-id"


def test_permission_warning_for_loose_config(config_dir: Path) -> None:
    (config_dir / CONFIG_FILE).chmod(0o644)
    _, factory = scripted(*reply_events())
    result = run_cli(["--plain"], config_dir, factory=factory)
    assert "chmod 600" in result.err
    assert str(config_dir / CONFIG_FILE) in result.err


def test_permission_warning_for_writable_config_dir(config_dir: Path) -> None:
    config_dir.chmod(0o775)
    _, factory = scripted(*reply_events())
    result = run_cli(["--plain"], config_dir, factory=factory)
    assert f"warning: {config_dir} is writable by other users" in result.err
    assert f"chmod 700 {config_dir}" in result.err


def test_each_permission_problem_gets_its_own_stderr_line(config_dir: Path) -> None:
    (config_dir / CONFIG_FILE).chmod(0o666)
    config_dir.chmod(0o777)
    _, factory = scripted(*reply_events())
    result = run_cli(["--plain"], config_dir, factory=factory)
    assert len([line for line in result.err.splitlines() if "warning:" in line]) == 3


def test_no_permission_warning_for_private_config(config_dir: Path) -> None:
    _, factory = scripted(*reply_events())
    result = run_cli(["--plain"], config_dir, factory=factory)
    assert "chmod" not in result.err


def test_provider_factory_config_error_is_an_error_line(config_dir: Path) -> None:
    def failing(_config: ProviderConfig) -> ChatProvider:
        raise ConfigError("no adapter for provider type 'ollama' yet")

    result = run_cli(["--plain"], config_dir, stdin="hi\n", factory=failing)
    assert result.code == 1
    assert result.err == "error: no adapter for provider type 'ollama' yet\n"


# Chat loop


def test_stdout_has_only_text_and_stderr_has_thinking_and_usage(
    config_dir: Path,
) -> None:
    _, factory = scripted(*FULL_REPLY)
    result = run_cli(["--plain"], config_dir, stdin="hi\n", factory=factory)
    assert result.code == 0
    assert result.out == "Hello\n"
    assert "pondering" in result.err
    assert "\x1b" not in result.err  # stderr is not a tty: no dim codes
    assert "[m1 tokens in=3 out=7 $0.00 " in result.err
    assert "reasoning" not in result.err


def test_thinking_is_dimmed_only_on_a_tty(config_dir: Path) -> None:
    _, factory = scripted(*FULL_REPLY)
    result = run_cli(
        ["--plain"], config_dir, stdin="hi\n", stderr_tty=True, factory=factory
    )
    assert "\x1b[2mpondering\x1b[0m" in result.err


def test_prompt_is_shown_only_when_stdin_is_a_tty(config_dir: Path) -> None:
    _, factory = scripted(*FULL_REPLY)
    piped = run_cli(["--plain"], config_dir, stdin="hi\n", factory=factory)
    assert "you> " not in piped.out
    _, factory = scripted(*FULL_REPLY)
    typed = run_cli(
        ["--plain"], config_dir, stdin="hi\n", stdin_tty=True, factory=factory
    )
    assert typed.out.startswith("you> ")


def test_second_request_carries_the_state_not_the_first_line(
    config_dir: Path,
) -> None:
    provider, factory = scripted(*FULL_REPLY)
    run_cli(
        ["--plain"],
        config_dir,
        stdin="FIRST-LINE-91\nSECOND-LINE-91\n",
        factory=factory,
    )
    last = provider.requests[-1]
    sent = "\n".join(message.content for message in last.messages)
    assert "SECOND-LINE-91" in sent
    assert "FIRST-LINE-91" not in sent
    assert last.model == "provider-model-id"


def thinking_models_yaml(thinking_line: str) -> str:
    return MODELS_YAML.replace(
        "    priority: 1\n  - id: m2", f"    priority: 1\n{thinking_line}  - id: m2", 1
    )


@pytest.mark.parametrize(
    ("flags", "thinking_line", "expected"),
    [
        ([], "", None),
        ([], "    thinking: false\n", False),
        ([], "    thinking: true\n", True),
        (["--think"], "", True),
        (["--no-think"], "", False),
        (["--think"], "    thinking: false\n", True),
        (["--no-think"], "    thinking: true\n", False),
    ],
)
def test_request_think_follows_flag_then_model_setting(
    config_dir: Path, flags: list[str], thinking_line: str, expected: bool | None
) -> None:
    (config_dir / MODELS_FILE).write_text(thinking_models_yaml(thinking_line))
    provider, factory = scripted(*reply_events())
    run_cli(["--plain", *flags], config_dir, stdin="hi\n", factory=factory)
    assert provider.requests[-1].think is expected


@pytest.mark.parametrize("model_thinking", [True, False, None])
@pytest.mark.parametrize("flag", [True, False, None])
def test_resolve_think_matrix(flag: bool | None, model_thinking: bool | None) -> None:
    model = ModelConfig.model_validate(
        {
            "id": "m",
            "name": "M",
            "provider": "local",
            "model": "x",
            "tags": [],
            "priority": 1,
            "thinking": model_thinking,
        }
    )
    expected = flag if flag is not None else model_thinking
    assert resolve_think(flag, model) is expected


def test_control_characters_are_removed_but_newline_and_tab_kept(
    config_dir: Path,
) -> None:
    _, factory = scripted(
        ThinkingDelta(text="a\x1b[31mb\x07c\x9bd\x7f"),
        TextDelta(text="x\x1b]0;title\x07y\n\tz\r\x00"),
        Done(),
    )
    result = run_cli(["--plain"], config_dir, stdin="hi\n", factory=factory)
    assert result.out == "x]0;titley\n\tz\n"
    assert result.err.startswith("a[31mbcd\n[step 1: answering]\n[m1 tokens ? $? ")
    assert "\x1b" not in result.out + result.err


def test_error_event_stops_the_loop_and_later_lines_are_not_sent(
    config_dir: Path,
) -> None:
    provider, factory = scripted(
        TextDelta(text="partial"), ErrorEvent(kind="auth", message="bad key")
    )
    result = run_cli(["--plain"], config_dir, stdin="one\ntwo\n", factory=factory)
    assert result.code == 1
    assert "error (auth): bad key" in result.err
    assert len(provider.requests) == 1
    # The orchestrator discards the text of a failed attempt: nothing partial shows.
    assert "partial" not in result.out


def test_exit_command_ends_cleanly_before_later_lines(config_dir: Path) -> None:
    provider, factory = scripted(*reply_events())
    result = run_cli(
        ["--plain"], config_dir, stdin="one\n/exit\ntwo\n", factory=factory
    )
    assert result.code == 0
    assert len(provider.requests) == 1


@pytest.mark.parametrize("command", ["/quit", " /QUIT ", "/Exit"])
def test_quit_commands_end_cleanly_without_a_model_call(
    config_dir: Path, command: str
) -> None:
    provider, factory = scripted(*reply_events())
    result = run_cli(
        ["--plain"], config_dir, stdin=f"{command}\ntwo\n", factory=factory
    )
    assert result.code == 0
    assert provider.requests == []


def test_text_that_merely_contains_quit_is_a_normal_message(config_dir: Path) -> None:
    provider, factory = scripted(*reply_events())
    result = run_cli(["--plain"], config_dir, stdin="/quit now\n", factory=factory)
    assert result.code == 0
    assert len(provider.requests) == 1


def test_blank_lines_are_skipped_and_eof_ends_cleanly(config_dir: Path) -> None:
    provider, factory = scripted(*reply_events())
    result = run_cli(["--plain"], config_dir, stdin="\n   \none\n\n", factory=factory)
    assert result.code == 0
    assert len(provider.requests) == 1


def test_keyboard_interrupt_from_stream_returns_130(config_dir: Path) -> None:
    class Interrupted(ScriptedProvider):
        async def stream_chat(self, req):  # type: ignore[no-untyped-def]
            raise KeyboardInterrupt
            yield  # pragma: no cover

    result = run_cli(
        ["--plain"],
        config_dir,
        stdin="hi\n",
        factory=lambda _config: Interrupted([]),
    )
    assert result.code == 130
    assert "Traceback" not in result.err


def test_provider_is_closed_after_the_chat(config_dir: Path) -> None:
    closed: list[bool] = []

    class Closing(ScriptedProvider):
        async def aclose(self) -> None:
            closed.append(True)

    run_cli(
        ["--plain"],
        config_dir,
        stdin="hi\n",
        factory=lambda _config: Closing(reply_events()),
    )
    assert closed == [True]


# Canary


def test_no_key_or_prompt_text_in_output_or_logs(
    config_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    (config_dir / CONFIG_FILE).write_text(
        '[providers.local]\ntype = "openai"\nbase_url = "https://api.example/v1"\n'
        f'api_key = "{CANARY_KEY}"\n'
    )
    (config_dir / CONFIG_FILE).chmod(0o600)
    _, factory = scripted(ErrorEvent(kind="auth", message="rejected"))
    with caplog.at_level(logging.DEBUG):
        result = run_cli(
            ["--plain"], config_dir, stdin=f"{CANARY_PROMPT}\n", factory=factory
        )
    assert result.code == 1
    everything = result.out + result.err + caplog.text
    assert CANARY_KEY not in everything
    assert CANARY_PROMPT not in everything


# Thinking line closing


def thinking_run(config_dir: Path, *events: StreamEvent, tty: bool = False) -> Run:
    _, factory = scripted(*events)
    return run_cli(
        ["--plain"], config_dir, stdin="hi\n", stderr_tty=tty, factory=factory
    )


USAGE = UsageEvent(usage=Usage(input_tokens=1, output_tokens=2))


def test_usage_line_is_on_its_own_line_after_open_thinking(config_dir: Path) -> None:
    result = thinking_run(
        config_dir, ThinkingDelta(text="pong."), TextDelta(text="pong"), USAGE, Done()
    )
    assert result.err.startswith(
        "pong.\n[step 1: answering]\n[m1 tokens in=1 out=2 $0.00 "
    )
    assert result.err.count("\n") == 3


def test_open_thinking_is_closed_after_the_reset_code_on_a_tty(
    config_dir: Path,
) -> None:
    result = thinking_run(
        config_dir, ThinkingDelta(text="pong."), USAGE, Done(), tty=True
    )
    assert result.err.startswith(
        "\x1b[2mpong.\x1b[0m\n\x1b[2m[step 1: answering]\x1b[0m\n[m1 tokens in=1 "
    )


def test_thinking_ending_in_newline_gets_no_extra_blank_line(config_dir: Path) -> None:
    result = thinking_run(
        config_dir, ThinkingDelta(text="pong.\n"), TextDelta(text="x"), USAGE, Done()
    )
    assert result.err.startswith("pong.\n[step 1: answering]\n[m1 tokens ")


def test_no_thinking_means_no_stray_newline_on_stderr(config_dir: Path) -> None:
    result = thinking_run(config_dir, TextDelta(text="x"), USAGE, Done())
    assert result.err.startswith("[step 1: answering]\n[m1 tokens ")
    assert result.err.count("\n") == 2


def test_error_after_thinking_is_on_its_own_line(config_dir: Path) -> None:
    result = thinking_run(
        config_dir, ThinkingDelta(text="hmm"), ErrorEvent(kind="auth", message="bad")
    )
    assert result.err == "hmm\nerror (auth): bad\n"


def test_thinking_line_is_closed_before_the_first_text_delta(config_dir: Path) -> None:
    log: list[str] = []

    class Out(TtyStringIO):
        def write(self, text: str) -> int:
            log.append(f"out:{text}")
            return super().write(text)

    class Err(TtyStringIO):
        def write(self, text: str) -> int:
            log.append(f"err:{text}")
            return super().write(text)

    _, factory = scripted(ThinkingDelta(text="hmm"), TextDelta(text="hi"), Done())
    main(
        ["--plain"],
        config_dir=config_dir,
        provider_factory=factory,
        stdin=TtyStringIO("q\n"),
        stdout=Out(),
        stderr=Err(),
    )
    assert log.index("err:\n") < log.index("out:hi")


# Fallback notes, usage line and the ledger in --plain

FALLBACK_CONFIG_TOML = """\
[providers.dead]
type = "ollama"
base_url = "http://dead.invalid:1"

[providers.local]
type = "ollama"
base_url = "http://local.invalid:2"
"""

# dead-model is the default (priority 200); live-model costs $0.017 for 3 in / 7 out.
FALLBACK_MODELS_YAML = """\
models:
  - id: dead-model
    name: Dead
    provider: dead
    model: dead-id
    tags: [default, chat, orchestrator]
    priority: 200
  - id: live-model
    name: Live
    provider: local
    model: live-id
    tags: [chat, default, orchestrator]
    priority: 100
    price:
      input_per_mtok: 1000
      output_per_mtok: 2000
"""

DEAD_EVENTS = [ErrorEvent(kind="connection", message="refused")]
LIVE_EVENTS = [
    TextDelta(text="pong"),
    UsageEvent(usage=Usage(input_tokens=3, output_tokens=7)),
    Done(finish_reason="stop"),
]
SWITCH_NOTE = "dead-model unavailable (connection); using live-model"
BACKOFF_NOTE = "dead-model is in backoff; using live-model"


class FallbackSetup:
    def __init__(self, tmp_path: Path) -> None:
        self.config_dir = tmp_path / "fallback-cfg"
        self.config_dir.mkdir(mode=0o700)
        self.config_dir.chmod(0o700)
        (self.config_dir / CONFIG_FILE).write_text(FALLBACK_CONFIG_TOML)
        (self.config_dir / CONFIG_FILE).chmod(0o600)
        (self.config_dir / MODELS_FILE).write_text(FALLBACK_MODELS_YAML)
        (self.config_dir / AGENTS_FILE).write_text(AGENTS_YAML)
        self.data_dir = tmp_path / "data"
        self.dead = ScriptedProvider(DEAD_EVENTS)
        self.live = ScriptedProvider(LIVE_EVENTS)

    def run(self, argv: list[str], stdin: str = "hi\n") -> Run:
        providers = {
            "http://dead.invalid:1": self.dead,
            "http://local.invalid:2": self.live,
        }
        return run_cli(
            ["--plain", *argv],
            self.config_dir,
            stdin=stdin,
            factory=lambda config: providers[config.base_url or ""],
            data_dir=self.data_dir,
        )

    def rows(self, query: str) -> list[tuple[Any, ...]]:
        with closing(sqlite3.connect(self.data_dir / "harness.db")) as conn:
            return conn.execute(query).fetchall()

    def seed_backoff(self, model_id: str) -> None:
        """Three manual failures: the model is in backoff for about 3 minutes."""
        store = Store(self.data_dir / "harness.db")
        now = datetime.now(UTC)
        call = CallRecord(
            time=now,
            model_id=model_id,
            provider_id="dead",
            input_tokens=None,
            output_tokens=None,
            reasoning_tokens=None,
            cost_usd=None,
            seconds=0.0,
            outcome="error",
            error_kind="connection",
            fallback_from=None,
        )
        for _ in range(3):
            store.record_attempt(call, "fail", attempt_started=now, is_manual=True)


@pytest.fixture
def fallback(tmp_path: Path) -> FallbackSetup:
    return FallbackSetup(tmp_path)


def test_dead_first_model_falls_back_with_a_note_and_a_usage_line_on_stderr(
    fallback: FallbackSetup,
) -> None:
    result = fallback.run([])
    assert result.code == 0
    assert result.out == "pong\n"
    note, status, usage = result.err.splitlines()
    assert note == SWITCH_NOTE
    assert status == "[step 1: answering]"
    assert usage.startswith("[live-model tokens in=3 out=7 $0.02 ")
    assert usage.endswith("s | session $0.02]")


def test_ledger_has_the_error_row_and_the_fallback_ok_row(
    fallback: FallbackSetup,
) -> None:
    fallback.run([])
    assert fallback.rows(
        "SELECT model_id, outcome, error_kind, fallback_from FROM llm_calls ORDER BY id"
    ) == [
        ("dead-model", "error", "connection", None),
        ("live-model", "ok", None, "dead-model"),
    ]
    assert fallback.rows("SELECT model_id, failures FROM model_backoff") == [
        ("dead-model", 1)
    ]


def test_the_session_total_grows_and_the_next_turn_skips_the_model_in_backoff(
    fallback: FallbackSetup,
) -> None:
    result = fallback.run([], stdin="one\ntwo\n")
    lines = result.err.splitlines()
    assert [line for line in lines if line.startswith("[") and "[step" not in line] == [
        line for line in lines if "session" in line
    ]
    totals = [line.rsplit("| ", 1)[1] for line in lines if "session" in line]
    assert totals == ["session $0.02]", "session $0.03]"]
    assert lines[0] == SWITCH_NOTE
    assert BACKOFF_NOTE in lines
    assert len(fallback.dead.requests) == 1
    assert len(fallback.live.requests) == 2


def test_the_default_model_in_backoff_is_skipped_with_the_backoff_note(
    fallback: FallbackSetup,
) -> None:
    fallback.seed_backoff("dead-model")
    result = fallback.run([])
    assert result.code == 0
    assert result.err.splitlines()[0] == BACKOFF_NOTE
    assert fallback.dead.requests == []
    assert len(fallback.live.requests) == 1


def test_a_model_flag_pick_in_backoff_is_still_tried(fallback: FallbackSetup) -> None:
    fallback.seed_backoff("dead-model")
    result = fallback.run(["--model", "dead-model"])
    assert result.code == 0
    assert result.out == "pong\n"
    assert result.err.splitlines()[0] == SWITCH_NOTE
    assert len(fallback.dead.requests) == 1
    assert fallback.rows("SELECT failures FROM model_backoff") == [(4,)]


def test_when_every_model_failed_the_error_line_names_them_and_exit_code_is_1(
    fallback: FallbackSetup,
) -> None:
    fallback.live = ScriptedProvider(DEAD_EVENTS)
    result = fallback.run([])
    assert result.code == 1
    assert result.out == "\n"
    assert (
        "error (connection): no model available: dead-model connection; " in result.err
    )
    assert "live-model connection" in result.err


@pytest.mark.parametrize("broken", ["directory", "garbage"])
def test_an_unusable_database_still_answers_and_notes_it_once(
    fallback: FallbackSetup, broken: str
) -> None:
    db_file = fallback.data_dir / "harness.db"
    garbage = b"this is not a sqlite database" * 20
    if broken == "directory":
        db_file.mkdir(parents=True)
    else:
        fallback.data_dir.mkdir()
        db_file.write_bytes(garbage)
    result = fallback.run([], stdin="one\ntwo\n")
    assert result.code == 0
    assert result.out == "pong\npong\n"
    notices = [
        line for line in result.err.splitlines() if "usage database unavailable" in line
    ]
    assert len(notices) == 1
    assert f"{db_file}; usage and backoff are not saved" in notices[0]
    if broken == "garbage":
        assert db_file.read_bytes() == garbage


def test_without_data_dir_the_ledger_goes_to_the_default_path(
    config_dir: Path,
) -> None:
    _, factory = scripted(*FULL_REPLY)
    run_cli(["--plain"], config_dir, stdin="hi\n", factory=factory)
    assert store_module.default_db_path().is_file()


def test_the_default_db_path_is_never_the_real_home_in_tests() -> None:
    real = Path.home() / ".local" / "share" / "llm-harness" / "harness.db"
    assert store_module.default_db_path() != real


async def test_a_note_closes_an_open_thinking_line_first() -> None:
    switch = ModelSwitch(from_model_id="a", reason="connection", to_model_id="b")
    report = UsageReport(
        model_id="b",
        usage=None,
        cost_usd=None,
        seconds=1.0,
        session_cost_usd=0.0,
        session_cost_incomplete=True,
    )
    events = [
        ThinkingDelta(text="hmm"),
        switch,
        ThinkingDelta(text="again"),
        Notice(text="note\x1b[31m"),
        report,
        Done(),
    ]
    session = Orchestrator(ScriptedProvider(events), STEP_MODEL)
    err = io.StringIO()
    assert await _run_turn(session, "hi", io.StringIO(), err)
    assert err.getvalue() == (
        "hmm\na unavailable (connection); using b\n"
        "again\nnote[31m\n"
        "[step 1: answering]\n"
        "[b tokens ? $? 1.0s | session $0.00+?]\n"
    )


# Reading stdin without blocking the loop or the exit


class BlockingStdin:
    """A stdin whose `readline` blocks until `release` is set."""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.started = threading.Event()
        self.reader_thread: threading.Thread | None = None

    def isatty(self) -> bool:
        return False

    def readline(self) -> str:
        self.reader_thread = threading.current_thread()
        self.started.set()
        self.release.wait(10)
        return "late line\n"


async def wait_until_started(stdin: BlockingStdin) -> None:
    for _ in range(200):
        if stdin.started.is_set():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the reader never started")


async def test_read_line_returns_lines_and_empty_string_at_eof() -> None:
    stdin = io.StringIO("one\ntwo")
    assert [await _read_line(stdin) for _ in range(3)] == ["one\n", "two", ""]


async def test_read_line_raises_the_readline_error_in_the_caller() -> None:
    stdin = io.StringIO()
    stdin.close()
    with pytest.raises(ValueError, match="closed file"):
        await _read_line(stdin)


def test_ctrl_c_does_not_wait_for_a_blocked_read_when_the_loop_closes(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    stdin = BlockingStdin()
    loop_errors: list[dict[str, Any]] = []
    thread_errors: list[Any] = []
    monkeypatch.setattr(threading, "excepthook", thread_errors.append)

    provider = ScriptedProvider([])

    async def interrupted_chat() -> None:
        asyncio.get_running_loop().set_exception_handler(
            lambda _loop, context: loop_errors.append(context)
        )
        chat = asyncio.create_task(
            _chat_loop(
                provider,
                stdin,
                io.StringIO(),
                io.StringIO(),
                session=Orchestrator(provider, STEP_MODEL),
            )
        )
        await wait_until_started(stdin)
        chat.cancel()
        with pytest.raises(asyncio.CancelledError):
            await chat

    # Safety net so a regression fails the timing assert instead of hanging.
    safety = threading.Timer(5, stdin.release.set)
    safety.start()
    started = time.monotonic()
    try:
        asyncio.run(interrupted_chat())
        elapsed = time.monotonic() - started
        assert elapsed < 2
        assert not stdin.release.is_set()
        reader = stdin.reader_thread
        assert reader is not None and reader.is_alive() and reader.daemon
        stdin.release.set()
        reader.join(2)
        assert not reader.is_alive()
    finally:
        safety.cancel()
        stdin.release.set()
    assert loop_errors == []
    assert thread_errors == []
    assert capsys.readouterr().err == ""


async def test_a_late_line_for_a_cancelled_read_is_ignored_silently(
    capsys: pytest.CaptureFixture[str],
) -> None:
    loop_errors: list[dict[str, Any]] = []
    asyncio.get_running_loop().set_exception_handler(
        lambda _loop, context: loop_errors.append(context)
    )
    stdin = BlockingStdin()
    reading = asyncio.create_task(_read_line(stdin))
    await wait_until_started(stdin)
    reading.cancel()
    with pytest.raises(asyncio.CancelledError):
        await reading
    reader = stdin.reader_thread
    assert reader is not None and reader.is_alive()
    stdin.release.set()
    reader.join(2)
    await asyncio.sleep(0.05)  # let the loop run the delivery callback
    assert loop_errors == []
    assert capsys.readouterr().err == ""
