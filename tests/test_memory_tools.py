"""The four memory tools, the no-prompt gate, the notice line and the `[tools] memory` flag.

Offline: a throwaway SQLite file under `tmp_path`, a fake clock, a fake provider.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from llm_harness.builtin_tools import MEMORY_NEEDS_USER_ID, build_tool_registry
from llm_harness.config import ConfigError, StorageConfig, ToolsConfig, VectorConfig
from llm_harness.inspect_state import tools_text
from llm_harness.memory import MemoryStore, MemoryValidationError
from llm_harness.memory_tools import MemoryTools
from llm_harness.storage import Storage, open_storage
from llm_harness.tools import ToolContext, ToolRegistry, tool_notifies, tool_prompts
from llm_harness.types import Done, Notice, StatusEvent, TextDelta
from tests.fake_steps import reply_events
from tests.test_builtin_tools import agent_with
from tests.test_config import PROVIDER_TOML, load_config, load_error, write_config
from tests.test_orchestrator import (
    RESPOND,
    SET_GOAL,
    StubTool,
    call,
    make,
    observation,
    run,
)
from tests.test_tool_approval import FakeApprover, approval_of
from tests.test_tools import agent

START = datetime(2026, 11, 2, 9, 30, 15, tzinfo=UTC)
MEMORY_TOOLS = ["memory_forget", "memory_save", "memory_search", "memory_update"]


class FakeClock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: int) -> None:
        self.now += timedelta(**delta)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def storage(tmp_path: Path) -> Storage:
    return open_storage(
        StorageConfig(vector=VectorConfig(backend="none")),
        tmp_path,
        relational_path=tmp_path / "memory.db",
    )


@pytest.fixture
def tools(clock: FakeClock) -> dict[str, object]:
    return {tool.name: tool for tool in MemoryTools(clock).tools()}


def ctx_for(
    storage: Storage | None, user_id: str | None = "alice", tags: set[str] | None = None
) -> ToolContext:
    return ToolContext(
        storage=storage,
        user_id=user_id,
        agent_id="orchestrator",
        tags=frozenset(tags or {"orchestrator"}),
    )


async def invoke(tools: dict, name: str, ctx: ToolContext, **arguments: object) -> str:
    tool = tools[name]
    return await tool.run(tool.input_model.model_validate(arguments), ctx)


# --- the tools -----------------------------------------------------------------


async def test_save_search_update_forget_round_trip(
    storage: Storage, tools: dict, clock: FakeClock
) -> None:
    ctx = ctx_for(storage)
    saved = await invoke(
        tools, "memory_save", ctx, text="Prefers metric units.", keywords=["metric"]
    )
    assert saved == (
        "saved memory #1 (created 2026-11-02T09:30:15Z, updated 2026-11-02T09:30:15Z)"
    )
    found = await invoke(tools, "memory_search", ctx, query="metric units")
    assert found == "#1 [metric] (2026-11-02T09:30:15Z) Prefers metric units."
    clock.advance(hours=1)
    updated = await invoke(
        tools, "memory_update", ctx, id=1, keywords=["metric", "units"]
    )
    assert updated == (
        "updated memory #1: Prefers metric units. (keywords: metric, units; "
        "created 2026-11-02T09:30:15Z, updated 2026-11-02T10:30:15Z)"
    )
    found = await invoke(tools, "memory_search", ctx, query="units")
    assert found == "#1 [metric, units] (2026-11-02T10:30:15Z) Prefers metric units."
    assert await invoke(tools, "memory_forget", ctx, id=1) == "forgot memory #1"
    assert await invoke(tools, "memory_forget", ctx, id=1) == "no memory #1"
    assert await invoke(tools, "memory_search", ctx, query="metric") == (
        "no memories match"
    )


async def test_search_returns_one_block_per_hit_and_honours_k(
    storage: Storage, tools: dict
) -> None:
    ctx = ctx_for(storage)
    for index in range(3):
        await invoke(
            tools, "memory_save", ctx, text=f"Likes tea {index}.", keywords=["tea"]
        )
    assert (
        len((await invoke(tools, "memory_search", ctx, query="tea")).splitlines()) == 3
    )
    limited = await invoke(tools, "memory_search", ctx, query="tea", k=2)
    assert len(limited.splitlines()) == 2


async def test_update_of_a_missing_id_says_so(storage: Storage, tools: dict) -> None:
    result = await invoke(tools, "memory_update", ctx_for(storage), id=9, text="x")
    assert result == "no memory #9"


async def test_two_users_are_isolated_through_the_tools(
    storage: Storage, tools: dict
) -> None:
    alice, bob = ctx_for(storage, "alice"), ctx_for(storage, "bob")
    await invoke(tools, "memory_save", alice, text="Alice likes tea.", keywords=["tea"])
    assert await invoke(tools, "memory_search", bob, query="tea") == "no memories match"
    assert await invoke(tools, "memory_update", bob, id=1, text="Hacked.") == (
        "no memory #1"
    )
    assert await invoke(tools, "memory_forget", bob, id=1) == "no memory #1"
    assert "Alice likes tea." in await invoke(
        tools, "memory_search", alice, query="tea"
    )


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("memory_search", {"query": "x"}),
        ("memory_save", {"text": "x", "keywords": ["x"]}),
        ("memory_update", {"id": 1, "text": "x"}),
        ("memory_forget", {"id": 1}),
    ],
)
async def test_without_user_id_or_storage_a_tool_returns_a_one_line_error(
    name: str, arguments: dict, storage: Storage, tools: dict
) -> None:
    no_user = await invoke(tools, name, ctx_for(storage, user_id=None), **arguments)
    no_storage = await invoke(tools, name, ctx_for(None), **arguments)
    assert no_user == "memory is off: no user_id is set"
    assert no_storage == "memory is unavailable: no storage"


async def test_a_validation_error_propagates_for_the_orchestrator(
    storage: Storage, tools: dict
) -> None:
    with pytest.raises(MemoryValidationError, match="keyword"):
        await invoke(
            tools, "memory_save", ctx_for(storage), text="ok", keywords=["Bad Word!"]
        )


def test_one_memory_store_is_shared_per_relational_store(
    storage: Storage, tmp_path: Path
) -> None:
    holder = MemoryTools()
    first = holder.store_for(storage)
    assert isinstance(first, MemoryStore)
    assert holder.store_for(storage) is first
    other = open_storage(
        StorageConfig(vector=VectorConfig(backend="none")),
        tmp_path / "other",
        relational_path=tmp_path / "other.db",
    )
    assert holder.store_for(other) is not first


def test_all_four_tools_share_one_holder_and_declare_the_gate() -> None:
    tools = {tool.name: tool for tool in MemoryTools().tools()}
    assert sorted(tools) == MEMORY_TOOLS
    assert len({id(tool._memories) for tool in tools.values()}) == 1
    assert [tool_prompts(tools[name]) for name in MEMORY_TOOLS] == [
        False,  # memory_forget
        False,  # memory_save
        True,  # memory_search (read)
        False,  # memory_update
    ]
    assert tools["memory_search"].risk == "read"
    assert tools["memory_search"].tags == ["orchestrator", "memory"]
    for name in ("memory_save", "memory_update", "memory_forget"):
        assert tools[name].risk == "write" and tools[name].tags == ["orchestrator"]
    for tool in tools.values():
        assert "\n" not in tool.description and "English" in tool.description
    assert "memory_search first" in tools["memory_save"].description
    assert "memory_update" in tools["memory_save"].description


@pytest.mark.parametrize("k", [0, 11, -1])
def test_k_outside_one_to_ten_is_rejected(k: int, tools: dict) -> None:
    with pytest.raises(ValidationError):
        tools["memory_search"].input_model.model_validate({"query": "x", "k": k})


@pytest.mark.parametrize("query", ["", "x" * 201])
def test_query_length_is_bounded(query: str, tools: dict) -> None:
    with pytest.raises(ValidationError):
        tools["memory_search"].input_model.model_validate({"query": query})


def test_k_defaults_to_five_and_id_must_be_an_integer(tools: dict) -> None:
    assert tools["memory_search"].input_model.model_validate({"query": "x"}).k == 5
    for name in ("memory_update", "memory_forget"):
        with pytest.raises(ValidationError):
            tools[name].input_model.model_validate({"id": "abc"})
        with pytest.raises(ValidationError):
            tools[name].input_model.model_validate({"id": 1.5})


# --- the approval gate and the notice line -------------------------------------


def saving_script() -> list:
    return [
        reply_events(
            call("memory_save", text="Prefers metric units.", keywords=["metric"]),
            SET_GOAL,
        ),
        reply_events(RESPOND),
    ]


def memory_registry(clock: FakeClock) -> ToolRegistry:
    registry = ToolRegistry()
    for tool in MemoryTools(clock).tools():
        registry.register(tool)
    return registry


async def test_a_memory_save_runs_with_zero_approver_calls_and_shows_the_notice(
    storage: Storage, clock: FakeClock
) -> None:
    approver = FakeApprover(True)
    orchestrator, provider = make(
        saving_script(),
        tools=memory_registry(clock),
        approver=approver,
        storage=storage,
        user_id="alice",
    )
    events = await run(orchestrator, "hi")
    assert approver.requests == []
    assert approval_of(orchestrator) == ("not_required", "ok")
    assert observation(provider.requests[1]).startswith(
        "tool memory_save result: saved memory #1 ("
    )
    notices = [
        e.text
        for e in events
        if isinstance(e, StatusEvent) and e.action_type == "call_tool"
    ]
    assert notices == [
        'tool memory_save {"keywords": ["metric"], "text": "Prefers metric units."}'
    ]
    hits = await MemoryStore(storage.relational).search("alice", "metric")
    assert [m.text for m in hits] == ["Prefers metric units."]


async def test_a_memory_save_runs_when_no_approver_exists(
    storage: Storage, clock: FakeClock
) -> None:
    orchestrator, _ = make(
        saving_script(),
        tools=memory_registry(clock),
        approver=None,
        storage=storage,
        user_id="alice",
    )
    await run(orchestrator, "hi")
    assert approval_of(orchestrator) == ("not_required", "ok")
    assert len(await MemoryStore(storage.relational).list("alice")) == 1


async def test_a_validation_error_becomes_a_failed_observation(
    storage: Storage, clock: FakeClock
) -> None:
    script = [
        reply_events(call("memory_save", text="ok", keywords=["BAD"]), SET_GOAL),
        reply_events(RESPOND),
    ]
    orchestrator, provider = make(
        script, tools=memory_registry(clock), storage=storage, user_id="alice"
    )
    await run(orchestrator, "hi")
    text = observation(provider.requests[1])
    assert text.startswith("tool memory_save failed: ")
    assert "keyword" in text


async def test_a_tool_without_prompts_still_asks_and_is_denied_without_an_approver() -> (
    None
):
    # Regression: dev_echo is a write tool with the default `prompts`.
    from llm_harness.builtin_tools import DevEchoTool

    registry = ToolRegistry()
    registry.register(DevEchoTool())
    script = [reply_events(call("dev_echo", text="x"), SET_GOAL), reply_events(RESPOND)]
    approver = FakeApprover(True)
    orchestrator, _ = make(script, tools=registry, approver=approver)
    await run(orchestrator, "hi")
    assert len(approver.requests) == 1
    orchestrator, provider = make(script, tools=registry, approver=None)
    events = await run(orchestrator, "hi")
    assert observation(provider.requests[1]) == (
        "tool dev_echo was not run: it needs the user's approval and none is available here"
    )
    assert approval_of(orchestrator) == ("unavailable", "rejected")
    status = [e.text for e in events if isinstance(e, StatusEvent)]
    assert "calling tool dev_echo" in status  # no notice line for a gated tool


@pytest.mark.parametrize("value", ["no", 0, None, 1])
def test_check_tool_rejects_a_non_bool_prompts(value: object) -> None:
    stub = StubTool("act")
    stub.risk = "write"
    stub.prompts = value
    with pytest.raises(TypeError, match="prompts must be a bool"):
        ToolRegistry().register(stub)


def test_prompts_defaults_to_true_and_only_false_skips_the_prompt() -> None:
    stub = StubTool("act")
    assert tool_prompts(stub) is True
    stub.prompts = False
    ToolRegistry().register(stub)
    assert tool_prompts(stub) is False


def no_reflection() -> list:
    body = '{"action": "none", "id": null, "text": null, "keywords": null}'
    return [TextDelta(text=body), Done()]


def updating_script(**arguments: object) -> list:
    return [
        reply_events(call("memory_update", **arguments), SET_GOAL),
        reply_events(RESPOND),
        no_reflection(),
    ]


async def update_notices(
    storage: Storage, clock: FakeClock, approver: FakeApprover, **arguments: object
) -> list[str]:
    orchestrator, _ = make(
        updating_script(**arguments),
        tools=memory_registry(clock),
        approver=approver,
        storage=storage,
        user_id="alice",
    )
    return [e.text for e in await run(orchestrator, "hi") if isinstance(e, Notice)]


async def test_a_memory_update_shows_the_stored_text_and_asks_nothing(
    storage: Storage, clock: FakeClock
) -> None:
    await MemoryStore(storage.relational, clock).save("alice", "Old text.", ["a"])
    approver = FakeApprover(True)
    notices = await update_notices(
        storage, clock, approver, id=1, text="Prefers metric units."
    )
    assert approver.requests == []
    (notice,) = notices
    assert notice.startswith("updated memory #1: Prefers metric units. (keywords: a;")


async def test_a_keywords_only_update_shows_the_unchanged_stored_text(
    storage: Storage, clock: FakeClock
) -> None:
    await MemoryStore(storage.relational, clock).save("alice", "Old text.", ["a"])
    (notice,) = await update_notices(
        storage, clock, FakeApprover(True), id=1, keywords=["b", "c"]
    )
    assert notice.startswith("updated memory #1: Old text. (keywords: b, c;")


async def test_a_missing_memory_update_notice_claims_no_stored_text(
    storage: Storage, clock: FakeClock
) -> None:
    # `no memory #99` is a normal tool result, so the generic notice shows it as is.
    approver = FakeApprover(True)
    notices = await update_notices(storage, clock, approver, id=99, text="x")
    assert notices == ["no memory #99"]
    assert approver.requests == []


async def test_invalid_memory_update_arguments_yield_no_notice(
    storage: Storage, clock: FakeClock
) -> None:
    approver = FakeApprover(True)
    assert await update_notices(storage, clock, approver, id="nope") == []
    assert approver.requests == []


async def test_a_memory_save_yields_no_result_notice(
    storage: Storage, clock: FakeClock
) -> None:
    orchestrator, _ = make(
        [*saving_script(), no_reflection()],
        tools=memory_registry(clock),
        storage=storage,
        user_id="alice",
    )
    events = await run(orchestrator, "hi")
    assert [e for e in events if isinstance(e, Notice)] == []


@pytest.mark.parametrize("value", ["no", 0, None, 1])
def test_check_tool_rejects_a_non_bool_notify_result(value: object) -> None:
    stub = StubTool("act")
    stub.notify_result = value
    with pytest.raises(TypeError, match="notify_result must be a bool"):
        ToolRegistry().register(stub)


def test_notify_result_defaults_to_false() -> None:
    stub = StubTool("act")
    assert tool_notifies(stub) is False
    stub.notify_result = True
    ToolRegistry().register(stub)
    assert tool_notifies(stub) is True


# --- the flag and the registry -------------------------------------------------


def test_the_memory_flag_defaults_to_false_and_is_strict(tmp_path: Path) -> None:
    assert ToolsConfig().memory is False
    write_config(tmp_path, PROVIDER_TOML + "[tools]\nmemory = true\n")
    assert load_config(tmp_path).tools.memory is True
    write_config(tmp_path, PROVIDER_TOML + '[tools]\nmemory = "yes"\n')
    assert "tools.memory" in str(load_error(tmp_path))


@pytest.mark.parametrize(
    ("memory", "user_id", "registered"),
    [
        (True, "alice", True),
        (True, None, False),
        (False, "alice", False),
        (False, None, False),
    ],
)
def test_memory_tools_need_the_flag_and_a_user_id(
    memory: bool, user_id: str | None, registered: bool
) -> None:
    registry = build_tool_registry([], memory=memory, user_id=user_id)
    expected = ["tool_search", *MEMORY_TOOLS] if registered else ["tool_search"]
    assert sorted(registry.names()) == sorted(expected)
    assert registry.notes == ([MEMORY_NEEDS_USER_ID] if memory and not user_id else [])


def test_memory_tools_are_not_imported_while_memory_is_off() -> None:
    import subprocess
    import sys

    code = (
        "import sys\n"
        "from llm_harness.builtin_tools import build_tool_registry\n"
        "build_tool_registry([])\n"
        "assert 'llm_harness.memory_tools' not in sys.modules\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_a_disabled_memory_name_in_agents_gets_the_enable_hint() -> None:
    with pytest.raises(ConfigError) as info:
        build_tool_registry([agent_with(tools=["memory_save"])])
    message = str(info.value)
    assert "unknown tool 'memory_save'" in message
    assert "enable it with [tools] memory = true and set user_id" in message


def test_the_hint_names_user_id_when_that_is_the_cause() -> None:
    with pytest.raises(ConfigError) as info:
        build_tool_registry([agent_with(deny=["memory_search"])], memory=True)
    assert "needs user_id, which is not set" in str(info.value)


def test_a_typo_in_a_memory_name_gets_no_hint() -> None:
    with pytest.raises(ConfigError) as info:
        build_tool_registry([agent_with(tools=["memory_savee"])], memory=True)
    assert "enable it with" not in str(info.value)
    assert "user_id" not in str(info.value)


def test_an_agent_can_list_a_memory_tool_once_memory_is_on() -> None:
    lists_it = agent_with(tools=["memory_save"])
    assert build_tool_registry([lists_it], memory=True, user_id="alice").names()


def test_a_subagent_gets_memory_search_only_when_its_tags_include_memory() -> None:
    registry = build_tool_registry([], memory=True, user_id="alice")

    def names(tags: list[str]) -> list[str]:
        return [t.name for t in registry.tools_for(agent(tags=tags))]

    assert names(["memory"]) == ["memory_search"]
    assert names(["writer"]) == []
    assert names(["orchestrator"]) == [*MEMORY_TOOLS, "tool_search"]


# --- the State tab text --------------------------------------------------------


def test_the_state_tab_says_why_memory_is_off() -> None:
    registry = build_tool_registry([], memory=True)
    text = tools_text(registry.all(), registry.notes)
    assert text.endswith(MEMORY_NEEDS_USER_ID)
    assert tools_text(registry.all()) == tools_text(registry.all(), [])
    assert tools_text([], ["why"]) == "(none)\n\nwhy"
