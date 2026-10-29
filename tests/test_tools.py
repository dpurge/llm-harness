"""Tool registry: registration rules, tag matching, search, token estimates."""

import pytest
from pydantic import BaseModel

from llm_harness.config import AgentConfig
from llm_harness.tools import (
    MAX_SEARCH_K,
    ToolContext,
    ToolRegistry,
    estimate_tokens,
    schema_text,
)


class NoInput(BaseModel):
    pass


class QueryInput(BaseModel):
    query: str


class FakeTool:
    def __init__(
        self,
        name: str,
        description: str = "A tool.",
        tags: list[str] | None = None,
        input_model: type[BaseModel] = NoInput,
    ) -> None:
        self.name = name
        self.description = description
        self.tags = tags if tags is not None else []
        self.input_model = input_model

    async def run(self, arguments: BaseModel, ctx: ToolContext) -> str:
        return self.name


def agent(**overrides: object) -> AgentConfig:
    fields: dict[str, object] = {
        "id": "a",
        "name": "A",
        "description": "An agent.",
        "tags": [],
        "model_tags": [],
    }
    return AgentConfig.model_validate(fields | overrides)


def registry_of(*tools: FakeTool) -> ToolRegistry:
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    return registry


def test_empty_registry_has_no_names_and_get_returns_none() -> None:
    registry = ToolRegistry()
    assert registry.names() == []
    assert registry.get("x") is None


def test_register_get_and_sorted_names() -> None:
    zeta, alpha = FakeTool("zeta"), FakeTool("alpha")
    registry = registry_of(zeta, alpha)
    assert registry.get("zeta") is zeta
    assert registry.names() == ["alpha", "zeta"]


def test_duplicate_name_raises_with_the_name_and_keeps_the_first() -> None:
    first = FakeTool("search")
    registry = registry_of(first)
    with pytest.raises(ValueError, match="'search' is already registered"):
        registry.register(FakeTool("search"))
    assert registry.get("search") is first


@pytest.mark.parametrize("name", ["", "Search", "web-search", "1st", "has space"])
def test_a_bad_name_is_refused_naming_the_tool(name: str) -> None:
    with pytest.raises(ValueError, match="the name must be lowercase"):
        ToolRegistry().register(FakeTool(name))


@pytest.mark.parametrize("description", ["", "   ", "two\nlines"])
def test_a_bad_description_is_refused_naming_the_tool(description: str) -> None:
    with pytest.raises(ValueError, match="tool 'x': the description"):
        ToolRegistry().register(FakeTool("x", description))


@pytest.mark.parametrize("tags", [["Upper"], ["a b"], [""], ["a", "a"]])
def test_bad_tags_are_refused_naming_the_tool(tags: list[str]) -> None:
    with pytest.raises(ValueError, match="tool 'x'"):
        ToolRegistry().register(FakeTool("x", tags=tags))


def test_an_input_model_that_is_not_a_pydantic_class_is_refused() -> None:
    tool = FakeTool("x")
    tool.input_model = dict  # type: ignore[assignment]
    with pytest.raises(TypeError, match="tool 'x': input_model"):
        ToolRegistry().register(tool)


def names_for(registry: ToolRegistry, **agent_fields: object) -> list[str]:
    return [tool.name for tool in registry.tools_for(agent(**agent_fields))]


def test_tools_for_matches_tags_and_sorts_by_name() -> None:
    registry = registry_of(
        FakeTool("zeta", tags=["web"]),
        FakeTool("alpha", tags=["web", "other"]),
        FakeTool("mid", tags=["other"]),
    )
    assert names_for(registry, tags=["web"]) == ["alpha", "zeta"]


def test_tools_for_adds_named_tools_without_a_matching_tag() -> None:
    registry = registry_of(FakeTool("a", tags=["x"]), FakeTool("b", tags=["y"]))
    assert names_for(registry, tags=["x"], tools=["b"]) == ["a", "b"]


def test_deny_wins_over_a_matching_tag() -> None:
    registry = registry_of(FakeTool("a", tags=["x"]), FakeTool("b", tags=["x"]))
    assert names_for(registry, tags=["x"], deny=["a"]) == ["b"]


def test_an_agent_with_nothing_gets_nothing_and_unknown_names_are_ignored() -> None:
    registry = registry_of(FakeTool("a", tags=["x"]))
    assert names_for(registry) == []
    assert names_for(registry, tools=["missing"]) == []


def search_names(registry: ToolRegistry, query: str, k: int = 5, names=None):
    return [tool.name for tool in registry.search(query, k, names)]


def test_search_weights_name_over_tag_over_description() -> None:
    registry = registry_of(
        FakeTool("describes", "Find a web page."),
        FakeTool("tagged", "Other.", tags=["web"]),
        FakeTool("web_fetch", "Other."),
    )
    assert search_names(registry, "web") == ["web_fetch", "tagged", "describes"]


def test_search_sums_distinct_query_words_and_counts_a_repeated_word_once() -> None:
    registry = registry_of(
        FakeTool("web_search", "Other."), FakeTool("web_fetch", "Search pages.")
    )
    # web_search: web (name 3) + search (name 3); web_fetch: web (3) + search (1).
    assert search_names(registry, "web search web") == ["web_search", "web_fetch"]


def test_search_leaves_out_zero_scores_and_breaks_ties_by_name() -> None:
    registry = registry_of(
        FakeTool("b_tool", tags=["x"]), FakeTool("a_tool", tags=["x"]), FakeTool("c")
    )
    assert search_names(registry, "x") == ["a_tool", "b_tool"]
    assert search_names(registry, "nothing") == []


def test_search_is_restricted_to_the_given_names_and_is_case_insensitive() -> None:
    registry = registry_of(FakeTool("web_a"), FakeTool("web_b"))
    assert search_names(registry, "WEB", names={"web_b"}) == ["web_b"]
    assert search_names(registry, "web", names=set()) == []


def test_search_caps_k() -> None:
    registry = registry_of(*(FakeTool(f"t{i:02d}", tags=["x"]) for i in range(15)))
    assert len(registry.search("x", 99)) == MAX_SEARCH_K
    assert registry.search("x", 0) == []
    assert registry.search("x", -3) == []


def test_estimate_tokens_is_characters_over_four_rounded_up() -> None:
    assert [estimate_tokens(t) for t in ("", "a", "abcd", "abcde")] == [0, 1, 1, 2]


def test_schema_text_is_the_compact_sorted_json_schema() -> None:
    text = schema_text(FakeTool("x", input_model=QueryInput))
    assert text == (
        '{"properties":{"query":{"title":"Query","type":"string"}},'
        '"required":["query"],"title":"QueryInput","type":"object"}'
    )
