"""Tool registry: register, look up, list, reject duplicates."""

import pytest

from llm_harness.tools import ToolContext, ToolRegistry


class NamedTool:
    def __init__(self, name: str) -> None:
        self.name = name

    async def run(self, arguments: dict[str, object], ctx: ToolContext) -> str:
        return self.name


def test_empty_registry_has_no_names_and_get_returns_none() -> None:
    registry = ToolRegistry()
    assert registry.names() == []
    assert registry.get("x") is None


def test_register_get_and_sorted_names() -> None:
    registry = ToolRegistry()
    zeta, alpha = NamedTool("zeta"), NamedTool("alpha")
    registry.register(zeta)
    registry.register(alpha)
    assert registry.get("zeta") is zeta
    assert registry.names() == ["alpha", "zeta"]


def test_duplicate_name_raises_with_the_name_and_keeps_the_first() -> None:
    registry = ToolRegistry()
    first = NamedTool("search")
    registry.register(first)
    with pytest.raises(ValueError, match="'search' is already registered"):
        registry.register(NamedTool("search"))
    assert registry.get("search") is first
