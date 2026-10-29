"""`--plain`, the terminal UI and the web GUI all drive the Orchestrator."""

import sqlite3
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

import llm_harness.cli as cli_module
from llm_harness.config import (
    AGENTS_FILE,
    CONFIG_FILE,
    MODELS_FILE,
    HarnessConfig,
    ModelConfig,
)
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

AGENTS_YAML = """\
agents:
  - id: orchestrator
    name: Orchestrator
    description: Reads the prompt and assembles the reply.
    tags: [orchestrator]
    model_tags: []
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
    (directory / AGENTS_FILE).write_text(AGENTS_YAML)
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


def test_plain_gives_the_orchestrator_the_agent_from_agents_yaml(
    config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agents: list[Any] = []

    class RecordingOrchestrator(cli_module.Orchestrator):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            agents.append(kwargs.get("agent"))
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(cli_module, "Orchestrator", RecordingOrchestrator)
    _, factory = steps(reply_events(RESPOND, SET_GOAL, USAGE))
    assert run_plain(config_dir, factory, tmp_path).code == 0
    (agent,) = agents
    assert agent is not None and agent.id == "orchestrator"
    assert agent.tags == ["orchestrator"]


def test_the_runners_get_one_registry_with_tool_search(
    config_dir: Path, tmp_path: Path
) -> None:
    runner = FakeTuiRunner()
    run_cli(
        ["--gui"],
        config_dir,
        gui_runner=runner,
        data_dir=tmp_path / "data",
    )
    (registry,) = runner.tools
    assert registry is not None and registry.names() == ["tool_search"]


def test_plain_requests_list_tool_search(config_dir: Path, tmp_path: Path) -> None:
    provider, factory = steps(reply_events(RESPOND, SET_GOAL, USAGE))
    assert run_plain(config_dir, factory, tmp_path).code == 0
    user = provider.requests[0].messages[1].content
    assert "## Tools\n\ntool_search: " in user


def test_an_unknown_tool_name_in_agents_yaml_stops_startup_naming_it(
    config_dir: Path, tmp_path: Path
) -> None:
    (config_dir / AGENTS_FILE).write_text(
        AGENTS_YAML.replace("model_tags: []", "model_tags: []\n    tools: [nope]")
    )
    provider, factory = steps()
    result = run_plain(config_dir, factory, tmp_path)
    assert result.code == 1
    assert "agent 'orchestrator': tools names unknown tool 'nope'" in result.err
    assert provider.requests == []


DELEGATE = {
    "type": "delegate",
    "subagent": "researcher",
    "brief": "find the capital of France",
}
PRICE = "    price: {input_per_mtok: 1, output_per_mtok: 0}\n"
TWO_PROVIDER_CONFIG = (
    '[providers.local]\ntype = "ollama"\nbase_url = "http://parent.test"\n'
    '[providers.other]\ntype = "ollama"\nbase_url = "http://child.test"\n'
)


def million(text_events: list[StreamEvent]) -> list[StreamEvent]:
    usage = UsageEvent(usage=Usage(input_tokens=1_000_000, output_tokens=0))
    return [usage, *text_events]


def delegation_world(tmp_path: Path) -> tuple[Path, ScriptedSteps, ScriptedSteps]:
    from llm_harness.config import EXAMPLE_AGENTS_YAML

    directory = write_config_dir(tmp_path)
    (directory / CONFIG_FILE).write_text(TWO_PROVIDER_CONFIG)
    (directory / MODELS_FILE).write_text(
        "models:\n"
        "  - id: orch\n    name: Orchestrator\n    provider: local\n"
        "    model: orch-model-id\n    tags: [orchestrator]\n    priority: 1\n"
        + PRICE
        + "  - id: helper\n    name: Helper\n    provider: other\n"
        "    model: helper-model-id\n    tags: [chat]\n    priority: 1\n" + PRICE
    )
    (directory / AGENTS_FILE).write_text(EXAMPLE_AGENTS_YAML)
    child_reply = reply_events(
        RESPOND | {"text": "Paris"},
        {
            "findings": {
                "$append": [{"text": "Paris is the capital", "source": "atlas"}]
            }
        },
    )
    parent = ScriptedSteps(
        [
            million(reply_events(DELEGATE, SET_GOAL)),
            million(reply_events(RESPOND, SET_GOAL)),
        ]
    )
    child = ScriptedSteps([million(child_reply)])
    return directory, parent, child


def run_delegation(tmp_path: Path) -> tuple[Run, ScriptedSteps, ScriptedSteps, Path]:
    directory, parent, child = delegation_world(tmp_path)
    by_url = {"http://parent.test": parent, "http://child.test": child}
    result = run_cli(
        ["--plain"],
        directory,
        stdin="capital of France?\n",
        factory=lambda provider: by_url[provider.base_url],
        data_dir=tmp_path / "data",
    )
    return result, parent, child, tmp_path / "data" / "harness.db"


def test_plain_delegates_to_a_subagent_and_prints_the_parents_answer(
    tmp_path: Path,
) -> None:
    result, parent, child, _ = run_delegation(tmp_path)
    assert result.code == 0, result.err
    assert result.out == "hello there\n"
    assert len(parent.requests) == 2
    assert len(child.requests) == 1
    assert child.requests[0].model == "helper-model-id"
    first, second = (r.messages[-1].content for r in parent.requests)
    assert "## Subagents\n\nresearcher: " in first
    assert "Paris" in second
    assert "Paris is the capital (source: atlas)" in second


def test_plain_session_total_includes_the_childs_call(tmp_path: Path) -> None:
    result, *_ = run_delegation(tmp_path)
    # The plain loop prints the turn's last usage line. Parent calls alone cost $2;
    # $3 means the child's router fed the same session total.
    lines = [line for line in result.err.splitlines() if "session $" in line]
    assert len(lines) == 1, result.err
    assert lines[0].startswith("[orch tokens in=1000000")
    assert lines[0].endswith("| session $3.00]")


async def session_total(router: Any) -> float:
    from llm_harness.types import ChatRequest, UsageReport

    request = ChatRequest(model="ignored", messages=[], think=None)
    events = [event async for event in router.stream_chat(request)]
    return next(e for e in events if isinstance(e, UsageReport)).session_cost_usd


@pytest.mark.parametrize("flags", [[], ["--gui"]], ids=["tui", "gui"])
async def test_each_session_of_a_ui_starts_its_cost_at_zero(
    tmp_path: Path, flags: list[str]
) -> None:
    directory, parent, child = delegation_world(tmp_path)
    runner = FakeTuiRunner()
    by_url = {"http://parent.test": parent, "http://child.test": child}
    run_cli(
        flags,
        directory,
        stdin_tty=True,
        stdout_tty=True,
        tui_runner=runner,
        gui_runner=runner,
        factory=lambda provider: by_url[provider.base_url],
        data_dir=tmp_path / "data",
    )
    (maker,) = runner.subagent_factory_makers
    assert maker is not None
    ((config, model, router_factory, is_manual),) = runner.calls
    researcher = next(a for a in config.agents if a.id == "researcher")
    # Session one: the parent's call costs $1, the child's call joins the same total.
    first = router_factory(model, is_manual)
    first_child, _ = maker(first)(researcher)
    assert await session_total(first) == 1.0
    assert await session_total(first_child) == 2.0
    # A new session (new chat, model change, another GUI client) starts again at $0.
    second = router_factory(model, is_manual)
    assert await session_total(second) == 1.0


def test_plain_delegation_records_ledger_rows_for_both_models(tmp_path: Path) -> None:
    _, _, _, ledger = run_delegation(tmp_path)
    with closing(sqlite3.connect(ledger)) as conn:
        rows = conn.execute(
            "SELECT model_id, COUNT(*) FROM llm_calls GROUP BY model_id ORDER BY 1"
        ).fetchall()
    assert rows == [("helper", 1), ("orch", 2)]


# The terminal UI and the web GUI get the same subagent factory as plain mode


@pytest.mark.parametrize("flags", [[], ["--gui"]], ids=["tui", "gui"])
def test_the_ui_runners_get_a_maker_of_subagent_factories_that_serve_the_researcher(
    tmp_path: Path, flags: list[str]
) -> None:
    from llm_harness.config import load_config

    directory, parent, child = delegation_world(tmp_path)
    runner = FakeTuiRunner()
    by_url = {"http://parent.test": parent, "http://child.test": child}
    run_cli(
        flags,
        directory,
        stdin_tty=True,
        stdout_tty=True,
        tui_runner=runner,
        gui_runner=runner,
        factory=lambda provider: by_url[provider.base_url],
        data_dir=tmp_path / "data",
    )
    (maker,) = runner.subagent_factory_makers
    assert maker is not None
    ((_, model, router_factory, is_manual),) = runner.calls
    # What a UI does per session: bind the maker to that session's router.
    make = maker(router_factory(model, is_manual))
    researcher = next(a for a in load_config(directory).agents if a.id == "researcher")
    router, model = make(researcher)
    assert hasattr(router, "stream_chat")
    assert model.id == "helper"
    assert set(researcher.model_tags) <= set(model.tags)


def test_plain_gives_the_orchestrator_the_agents_and_a_subagent_factory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[dict[str, Any]] = []

    class RecordingOrchestrator(cli_module.Orchestrator):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            seen.append(kwargs)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(cli_module, "Orchestrator", RecordingOrchestrator)
    run_delegation(tmp_path)
    (kwargs,) = seen
    assert [agent.id for agent in kwargs["subagents"]] == ["orchestrator", "researcher"]
    assert callable(kwargs["subagent_factory"])
