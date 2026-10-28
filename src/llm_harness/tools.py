"""Minimal tool registry: name lookup only.

Typed input and output schemas belong to the later `tools-first-set` feature.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from llm_harness.storage import Storage


@dataclass(frozen=True)
class ToolContext:
    """What the orchestrator hands every tool run: data access and who is asking.

    `storage` is None only when the orchestrator was built without one, as in
    tests; production always passes it.
    """

    storage: "Storage | None"
    user_id: str | None
    agent_id: str
    tags: frozenset[str]


class Tool(Protocol):
    name: str

    async def run(self, arguments: dict[str, object], ctx: ToolContext) -> str: ...


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(
                f"tool {tool.name!r} is already registered; tool names must be unique."
            )
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)
