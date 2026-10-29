"""`tool_search` and `build_tool_registry`."""

import json

import pytest
from pydantic import ValidationError

from llm_harness.builtin_tools import ToolSearchInput, build_tool_registry
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
