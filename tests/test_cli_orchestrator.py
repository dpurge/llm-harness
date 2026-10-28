"""`--plain`, the terminal UI and the web GUI all drive the Orchestrator."""

import sqlite3
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

import llm_harness.cli as cli_module
from llm_harness.config import CONFIG_FILE, MODELS_FILE, HarnessConfig, ModelConfig
from llm_harness.router import RouterFactory
from llm_harness.storage import Storage, open_storage
from llm_harness.types import (
    Done,
    StatusEvent,
    StreamEvent,
    TextDelta,
    Usage,
    UsageEvent,
)
from tests.fake_steps import ScriptedSteps, reply_events
from tests.test_cli import CONFIG_TOML, FakeTuiRunner, Run, run_cli

MODELS_YAML = """\
models:
  - id: plain-default
    name: Plain default
    provider: local
    model: default-model-id
    tags: [default, chat]
    priority: 100
  - id: orch
    name: Orchestrator
    provider: local
    model: orch-model-id
    tags: [orchestrator]
    priority: 1
"""

RESPOND = {"type": "respond", "text": "hello there"}
ASK = {"type": "ask_user", "question": "Which file?"}
SET_GOAL = {"goal": "greet"}
USAGE = UsageEvent(usage=Usage(input_tokens=3, output_tokens=7))


def write_config_dir(root: Path, models_yaml: str = MODELS_YAML) -> Path:
    directory = root / "cfg"
    directory.mkdir(mode=0o700)
    directory.chmod(0o700)
    (directory / CONFIG_FILE).write_text(CONFIG_TOML)
    (directory / CONFIG_FILE).chmod(0o600)
    (directory / MODELS_FILE).write_text(models_yaml)
    return directory


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    return write_config_dir(tmp_path)


def steps(*scripts: list[StreamEvent]) -> tuple[ScriptedSteps, Callable[..., Any]]:
    provider = ScriptedSteps(list(scripts))
    return provider, lambda _config: provider


def run_plain(config_dir: Path, factory: Callable[..., Any], tmp_path: Path) -> Run:
    return run_cli(
        ["--plain"],
        config_dir,
        stdin="hi\n",
        factory=factory,
        data_dir=tmp_path / "data",
    )


def test_plain_prints_the_reply_a_status_line_and_the_usage_line(
    config_dir: Path, tmp_path: Path
) -> None:
    _, factory = steps(reply_events(RESPOND, SET_GOAL, USAGE))
    result = run_plain(config_dir, factory, tmp_path)
    assert result.code == 0
    assert result.out == "hello there\n"
    assert "[step 1: answering]\n" in result.err
    assert "[orch tokens in=3 out=7" in result.err
    assert "{" not in result.out  # no raw JSON on stdout


def test_plain_sends_the_configured_user_id_to_the_model(
    config_dir: Path, tmp_path: Path
) -> None:
    config_path = config_dir / CONFIG_FILE
    config_path.write_text('user_id = "ann"\n' + CONFIG_TOML)
    provider, factory = steps(reply_events(RESPOND, SET_GOAL))
    run_plain(config_dir, factory, tmp_path)
    assert '"user_id":"ann"' in provider.requests[0].messages[-1].content


def test_plain_without_a_user_id_sends_null(config_dir: Path, tmp_path: Path) -> None:
    provider, factory = steps(reply_events(RESPOND, SET_GOAL))
    run_plain(config_dir, factory, tmp_path)
    assert '"user_id":null' in provider.requests[0].messages[-1].content


def test_plain_uses_the_orchestrator_model_not_the_default(
    config_dir: Path, tmp_path: Path
) -> None:
    provider, factory = steps(reply_events(RESPOND, SET_GOAL))
    run_plain(config_dir, factory, tmp_path)
    assert provider.requests[0].model == "orch-model-id"


def test_plain_model_flag_overrides_the_orchestrator_tag(
    config_dir: Path, tmp_path: Path
) -> None:
    provider, factory = steps(reply_events(RESPOND, SET_GOAL))
    result = run_cli(
        ["--plain", "--model", "plain-default"],
        config_dir,
        stdin="hi\n",
        factory=factory,
        data_dir=tmp_path / "data",
    )
    assert result.code == 0
    assert provider.requests[0].model == "default-model-id"


def test_plain_without_an_orchestrator_model_names_the_tag(tmp_path: Path) -> None:
    only_default = MODELS_YAML.replace("[orchestrator]", "[chat]")
    directory = write_config_dir(tmp_path, only_default)
    _, factory = steps()
    result = run_plain(directory, factory, tmp_path)
    assert result.code == 1
    assert "no model is tagged 'orchestrator'" in result.err
    assert "models.yaml" in result.err and "--model" in result.err


def test_plain_ask_user_prints_the_question(config_dir: Path, tmp_path: Path) -> None:
    _, factory = steps(reply_events(ASK))
    result = run_plain(config_dir, factory, tmp_path)
    assert result.code == 0
    assert result.out == "Which file?\n"
    assert "[step 1: asking the user]" in result.err


def test_plain_status_line_is_dim_only_on_a_tty(
    config_dir: Path, tmp_path: Path
) -> None:
    _, factory = steps(reply_events(RESPOND, SET_GOAL))
    tty = run_cli(
        ["--plain"],
        config_dir,
        stdin="hi\n",
        stderr_tty=True,
        factory=factory,
        data_dir=tmp_path / "data",
    )
    assert "\x1b[2m[step 1: answering]\x1b[0m" in tty.err


def test_plain_status_text_is_sanitised() -> None:
    import asyncio
    import io

    class Fixed:
        async def send(self, text: str):
            yield StatusEvent(step=2, action_type="respond", text="a\x1b[31mb\x07")
            yield Done()

    err = io.StringIO()
    assert asyncio.run(cli_module._run_turn(Fixed(), "hi", io.StringIO(), err))
    assert err.getvalue() == "[step 2: a[31mb]\n"


def test_plain_rejected_replies_end_with_an_error_line_and_exit_1(
    config_dir: Path, tmp_path: Path
) -> None:
    bad = [TextDelta(text="not json"), Done()]
    _, factory = steps(bad, bad, bad)
    result = run_plain(config_dir, factory, tmp_path)
    assert result.code == 1
    assert "error (unknown): " in result.err
    assert "[step" not in result.err


def test_plain_router_factory_requires_the_orchestrator_tag(
    config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[frozenset[str]] = []
    real = cli_module.make_router_factory

    def recording(*args: Any, **kwargs: Any) -> RouterFactory:
        seen.append(kwargs["required_tags"])
        return real(*args, **kwargs)

    monkeypatch.setattr(cli_module, "make_router_factory", recording)
    _, factory = steps(reply_events(RESPOND, SET_GOAL))
    run_plain(config_dir, factory, tmp_path)
    assert seen == [frozenset({"orchestrator"})]


def test_tui_gets_the_orchestrator_model_and_the_orchestrator_tag(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[frozenset[str]] = []
    real = cli_module.make_router_factory

    def recording(*args: Any, **kwargs: Any) -> RouterFactory:
        seen.append(kwargs["required_tags"])
        return real(*args, **kwargs)

    monkeypatch.setattr(cli_module, "make_router_factory", recording)
    runner = FakeTuiRunner()
    run_cli([], config_dir, stdin_tty=True, stdout_tty=True, tui_runner=runner)
    assert runner.calls[0][1].id == "orch"
    assert seen == [frozenset({"orchestrator"})]


def test_tui_model_flag_overrides_the_orchestrator_tag(config_dir: Path) -> None:
    runner = FakeTuiRunner()
    run_cli(
        ["--model", "plain-default"],
        config_dir,
        stdin_tty=True,
        stdout_tty=True,
        tui_runner=runner,
    )
    assert runner.calls[0][1].id == "plain-default"
    assert runner.calls[0][3] is True


def test_tui_without_an_orchestrator_model_names_the_tag(tmp_path: Path) -> None:
    only_default = MODELS_YAML.replace("[orchestrator]", "[chat]")
    directory = write_config_dir(tmp_path, only_default)
    runner = FakeTuiRunner()
    result = run_cli([], directory, stdin_tty=True, stdout_tty=True, tui_runner=runner)
    assert result.code == 1
    assert "no model is tagged 'orchestrator'" in result.err
    assert runner.calls == []


def test_gui_gets_the_orchestrator_model_and_the_orchestrator_tag(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[frozenset[str]] = []
    real = cli_module.make_router_factory

    def recording(*args: Any, **kwargs: Any) -> RouterFactory:
        seen.append(kwargs["required_tags"])
        return real(*args, **kwargs)

    monkeypatch.setattr(cli_module, "make_router_factory", recording)
    calls: list[tuple[HarnessConfig, ModelConfig, RouterFactory, bool]] = []

    def gui(*args: Any, **kwargs: Any) -> int:
        calls.append(args)  # type: ignore[arg-type]
        return 0

    run_cli(["--gui"], config_dir, gui_runner=gui)
    assert calls[0][1].id == "orch"
    assert calls[0][3] is False
    assert seen == [frozenset({"orchestrator"})]


def test_gui_model_flag_overrides_the_orchestrator_tag(config_dir: Path) -> None:
    calls: list[tuple[Any, ...]] = []

    def gui(*args: Any, **kwargs: Any) -> int:
        calls.append(args)
        return 0

    run_cli(["--gui", "--model", "plain-default"], config_dir, gui_runner=gui)
    assert calls[0][1].id == "plain-default"
    assert calls[0][3] is True


def test_gui_without_an_orchestrator_model_names_the_tag(tmp_path: Path) -> None:
    only_default = MODELS_YAML.replace("[orchestrator]", "[chat]")
    directory = write_config_dir(tmp_path, only_default)
    calls: list[tuple[Any, ...]] = []

    def gui(*args: Any, **kwargs: Any) -> int:
        calls.append(args)
        return 0

    result = run_cli(["--gui"], directory, gui_runner=gui)
    assert result.code == 1
    assert "no model is tagged 'orchestrator'" in result.err
    assert "models.yaml" in result.err and "--model" in result.err
    assert calls == []


# --- [storage]: one Storage per process, closed on exit ---------------------


def test_plain_writes_the_ledger_to_the_configured_relational_path(
    config_dir: Path, tmp_path: Path
) -> None:
    ledger = tmp_path / "custom" / "ledger.db"
    with (config_dir / CONFIG_FILE).open("a") as handle:
        handle.write(f'\n[storage.relational]\npath = "{ledger}"\n')
    _, factory = steps(reply_events(RESPOND, SET_GOAL, USAGE))
    result = run_plain(config_dir, factory, tmp_path)
    assert result.code == 0
    with closing(sqlite3.connect(ledger)) as conn:
        (rows,) = conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()
    assert rows == 1
    assert not (tmp_path / "data" / "harness.db").exists()
    assert not (tmp_path / "data" / "vectors").exists()
    assert not (tmp_path / "data" / "files").exists()


class SpyStorage(Storage):
    def __init__(self, inner: Storage) -> None:
        super().__init__(inner.relational, inner.files, inner.vectors)
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1
        await super().close()


@pytest.fixture
def spy_storage(monkeypatch: pytest.MonkeyPatch) -> list[SpyStorage]:
    created: list[SpyStorage] = []

    def spying(*args: Any, **kwargs: Any) -> SpyStorage:
        spy = SpyStorage(open_storage(*args, **kwargs))
        created.append(spy)
        return spy

    monkeypatch.setattr(cli_module, "open_storage", spying)
    return created


def test_plain_closes_the_storage_once_on_a_normal_exit(
    config_dir: Path, tmp_path: Path, spy_storage: list[SpyStorage]
) -> None:
    _, factory = steps(reply_events(RESPOND, SET_GOAL, USAGE))
    assert run_plain(config_dir, factory, tmp_path).code == 0
    assert [spy.close_calls for spy in spy_storage] == [1]


def test_plain_closes_the_storage_on_ctrl_c(
    config_dir: Path, tmp_path: Path, spy_storage: list[SpyStorage]
) -> None:
    class Interrupted(ScriptedSteps):
        async def stream_chat(self, req):  # type: ignore[no-untyped-def]
            raise KeyboardInterrupt
            yield  # pragma: no cover

    result = run_plain(config_dir, lambda _config: Interrupted([]), tmp_path)
    assert result.code == 130
    assert [spy.close_calls for spy in spy_storage] == [1]


@pytest.mark.parametrize("flag", ["--gui", None])
def test_the_gui_and_tui_runners_get_the_storage_to_close(
    config_dir: Path, spy_storage: list[SpyStorage], flag: str | None
) -> None:
    runner = FakeTuiRunner()
    if flag:
        run_cli([flag], config_dir, gui_runner=runner)
    else:
        run_cli([], config_dir, stdin_tty=True, stdout_tty=True, tui_runner=runner)
    assert runner.storages == spy_storage
    assert len(spy_storage) == 1
    # Closing is the front end's job, so the runner that never closes leaves it open.
    assert spy_storage[0].close_calls == 0
