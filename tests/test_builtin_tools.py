"""`tool_search` and `build_tool_registry`."""

import json

import pytest
from pydantic import ValidationError

from llm_harness.builtin_tools import (
    DevEchoInput,
    DevEchoTool,
    ToolSearchInput,
    build_tool_registry,
)
from llm_harness.config import AgentConfig, ConfigError
from llm_harness.tools import ToolContext, estimate_tokens
from tests.test_tools import FakeTool, QueryInput, agent


def ctx_for(*allowed: str) -> ToolContext:
    return ToolContext(
        storage=None,
        user_id=None,
        agent_id="orchestrator",
        tags=frozenset({"orchestrator"}),
        allowed_tools=frozenset(allowed),
    )


async def search(registry, query: str, *allowed: str, k: int = 5) -> str:
    tool = registry.get("tool_search")
    return await tool.run(ToolSearchInput(query=query, k=k), ctx_for(*allowed))


def with_tools(*tools: FakeTool):
    registry = build_tool_registry([])
    for tool in tools:
        registry.register(tool)
    return registry


def test_the_built_in_registry_has_tool_search_for_the_orchestrator() -> None:
    registry = build_tool_registry([])
    assert registry.names() == ["tool_search"]
    orchestrator = agent(tags=["orchestrator"])
    assert [t.name for t in registry.tools_for(orchestrator)] == ["tool_search"]
    assert registry.tools_for(agent(tags=["writer"])) == []


@pytest.mark.parametrize(
    "fields",
    [
        {"query": ""},
        {"query": "x" * 201},
        {"query": "x", "k": 0},
        {"query": "x", "k": 11},
    ],
)
def test_tool_search_input_rejects_bad_values(fields: dict) -> None:
    with pytest.raises(ValidationError):
        ToolSearchInput(**fields)


def test_tool_search_input_defaults_k_to_five() -> None:
    assert ToolSearchInput(query="x").k == 5


async def test_tool_search_returns_name_description_tags_tokens_and_schema() -> None:
    web = FakeTool("web_search", "Search the web.", ["web"], QueryInput)
    registry = with_tools(web)
    result = await search(registry, "web", "web_search")
    schema = json.dumps(
        QueryInput.model_json_schema(), sort_keys=True, separators=(",", ":")
    )
    assert result == (
        f"web_search - Search the web. tags: web. "
        f"schema ~{estimate_tokens(schema)} tokens.\n{schema}"
    )


async def test_tool_search_only_finds_the_tools_the_caller_may_use() -> None:
    registry = with_tools(FakeTool("web_a"), FakeTool("web_b"))
    result = await search(registry, "web", "web_b")
    assert "web_b" in result and "web_a" not in result


async def test_tool_search_says_so_when_nothing_matches() -> None:
    registry = with_tools(FakeTool("web_a"))
    assert await search(registry, "zzz", "web_a") == "no tool matches 'zzz'"
    assert await search(registry, "web") == "no tool matches 'web'"


async def test_tool_search_separates_matches_and_respects_k() -> None:
    registry = with_tools(FakeTool("web_a"), FakeTool("web_b"))
    both = await search(registry, "web", "web_a", "web_b")
    assert both.count("\n\n") == 1
    one = await search(registry, "web", "web_a", "web_b", k=1)
    assert "web_b" not in one


async def test_tool_with_no_tags_says_none() -> None:
    registry = with_tools(FakeTool("web_a"))
    assert "tags: (none)." in await search(registry, "web", "web_a")


def agent_with(**overrides: object) -> AgentConfig:
    return agent(id="writer", **overrides)


@pytest.mark.parametrize("field", ["tools", "deny"])
def test_an_unknown_tool_name_in_agents_is_refused_naming_agent_and_tool(
    field: str,
) -> None:
    with pytest.raises(ConfigError) as info:
        build_tool_registry([agent_with(**{field: ["nope"]})])
    message = str(info.value)
    assert "invalid agents.yaml" in message
    assert f"agent 'writer': {field} names unknown tool 'nope'" in message
    assert "known tools: tool_search" in message


def test_known_tool_names_in_agents_are_accepted() -> None:
    registry = build_tool_registry(
        [agent_with(tools=["tool_search"]), agent(id="b", deny=["tool_search"])]
    )
    assert registry.names() == ["tool_search"]


def test_dev_echo_is_registered_only_when_enabled() -> None:
    assert "dev_echo" not in build_tool_registry([]).names()
    registry = build_tool_registry([], dev_echo=True)
    assert registry.names() == ["dev_echo", "tool_search"]
    orchestrator = agent(tags=["orchestrator"])
    assert "dev_echo" in [t.name for t in registry.tools_for(orchestrator)]


def test_an_agent_can_list_dev_echo_only_when_it_is_enabled() -> None:
    lists_it = AgentConfig(
        id="a",
        name="A",
        description="d",
        tags=["orchestrator"],
        model_tags=[],
        tools=["dev_echo"],
    )
    with pytest.raises(ConfigError, match="unknown tool 'dev_echo'"):
        build_tool_registry([lists_it])
    assert build_tool_registry([lists_it], dev_echo=True).names()


async def test_dev_echo_is_a_write_tool_that_echoes_its_text() -> None:
    tool = DevEchoTool()
    assert tool.risk == "write"
    ctx = ToolContext(
        storage=None,
        user_id=None,
        agent_id="a",
        tags=frozenset(),
        allowed_tools=frozenset(),
    )
    result = await tool.run(DevEchoInput(text="hello"), ctx)
    assert result == "dev_echo: hello"


# --- web tools --------------------------------------------------------------


class RecordingFetcher:
    """Stands in for WebFetcher: the registry only stores it."""


class FakeSearchProvider:
    name = "fake"

    async def search(self, query: str, max_results: int) -> list:
        return []


def test_web_tools_are_registered_only_when_enabled() -> None:
    assert build_tool_registry([]).names() == ["tool_search"]
    assert build_tool_registry([], web_fetch=True).names() == [
        "tool_search",
        "web_fetch",
    ]
    assert build_tool_registry([], web_search=True).names() == [
        "tool_search",
        "web_search",
    ]
    both = build_tool_registry([], web_search=True, web_fetch=True)
    assert both.names() == ["tool_search", "web_fetch", "web_search"]


def test_the_web_tools_are_read_risk_tools_for_the_orchestrator() -> None:
    registry = build_tool_registry([], web_search=True, web_fetch=True)
    shown = [t.name for t in registry.tools_for(agent(tags=["orchestrator"]))]
    assert "web_fetch" in shown and "web_search" in shown
    assert registry.get("web_fetch").risk == "read"
    assert registry.get("web_search").risk == "read"
    assert registry.tools_for(agent(tags=["writer"])) == []


@pytest.mark.parametrize("name", ["web_fetch", "web_search"])
def test_an_agent_naming_a_disabled_web_tool_gets_the_enable_hint(name: str) -> None:
    with pytest.raises(ConfigError) as info:
        build_tool_registry([agent_with(tools=[name])])
    message = str(info.value)
    assert f"unknown tool {name!r}" in message
    assert f"enable it with [tools] {name} = true" in message


def test_a_typo_in_a_web_tool_name_gets_no_hint() -> None:
    with pytest.raises(ConfigError) as info:
        build_tool_registry([agent_with(tools=["web_fetchh"])], web_fetch=True)
    assert "unknown tool 'web_fetchh'" in str(info.value)
    assert "enable it with" not in str(info.value)


def test_an_agent_can_list_a_web_tool_once_it_is_enabled() -> None:
    lists_it = agent_with(tools=["web_fetch"], deny=["web_search"])
    assert build_tool_registry([lists_it], web_fetch=True, web_search=True).names()
    with pytest.raises(
        ConfigError, match="enable it with \\[tools\\] web_fetch = true"
    ):
        build_tool_registry([lists_it], web_search=True)


def test_an_injected_fetcher_and_provider_are_used() -> None:
    fetcher, provider = RecordingFetcher(), FakeSearchProvider()
    registry = build_tool_registry(
        [], web_search=True, web_fetch=True, fetcher=fetcher, search_provider=provider
    )
    assert registry.get("web_fetch")._fetcher is fetcher
    assert registry.get("web_search")._provider is provider


def test_the_real_search_provider_shares_the_fetcher(monkeypatch) -> None:
    fetcher = RecordingFetcher()
    registry = build_tool_registry([], web_search=True, fetcher=fetcher)
    provider = registry.get("web_search")._provider
    assert provider.name == "duckduckgo" and provider._fetcher is fetcher


def test_web_relax_tls_strict_reaches_the_real_fetcher(monkeypatch) -> None:
    created: list[bool] = []

    from llm_harness import web_fetcher

    real = web_fetcher.WebFetcher

    class Spy(real):  # type: ignore[misc, valid-type]
        def __init__(self, *args: object, **kwargs: object) -> None:
            created.append(bool(kwargs.get("relax_tls_strict")))
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(web_fetcher, "WebFetcher", Spy)
    build_tool_registry([], web_fetch=True)
    build_tool_registry([], web_fetch=True, web_relax_tls_strict=True)
    build_tool_registry([], web_relax_tls_strict=True)  # no web tool: no fetcher
    assert created == [False, True]
    registry = build_tool_registry([], web_fetch=True, web_relax_tls_strict=True)
    assert registry.get("web_fetch")._fetcher._relax_tls_strict is True
