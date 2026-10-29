"""The tools that ship with the program, and the registry built from them."""

from pydantic import BaseModel, Field

from llm_harness.config import AgentConfig, ConfigError
from llm_harness.tools import (
    MAX_SEARCH_K,
    Tool,
    ToolContext,
    ToolRegistry,
    estimate_tokens,
    schema_text,
)


class ToolSearchInput(BaseModel):
    query: str = Field(min_length=1, max_length=200)
    k: int = Field(default=5, ge=1, le=MAX_SEARCH_K)


def _tool_block(tool: Tool) -> str:
    schema = schema_text(tool)
    tags = ", ".join(tool.tags) or "(none)"
    return (
        f"{tool.name} - {tool.description} tags: {tags}. "
        f"schema ~{estimate_tokens(schema)} tokens.\n{schema}"
    )


class ToolSearchTool:
    """Lets the model look up the tools it may call and their argument schemas."""

    name = "tool_search"
    description = "Find the tools you may use by words and get their argument schemas."
    input_model = ToolSearchInput

    def __init__(self, registry: ToolRegistry) -> None:
        self.tags = ["orchestrator"]
        self._registry = registry

    async def run(self, arguments: ToolSearchInput, ctx: ToolContext) -> str:
        matches = self._registry.search(arguments.query, arguments.k, ctx.allowed_tools)
        if not matches:
            return f"no tool matches {arguments.query!r}"
        return "\n\n".join(_tool_block(tool) for tool in matches)


def build_tool_registry(agents: list[AgentConfig]) -> ToolRegistry:
    """The registry of built-in tools; every tool name in `agents` must exist."""
    registry = ToolRegistry()
    registry.register(ToolSearchTool(registry))
    known = registry.names()
    for agent in agents:
        for field, names in (("tools", agent.tools), ("deny", agent.deny)):
            for name in names:
                if name not in known:
                    raise ConfigError(
                        f"invalid agents.yaml: agent {agent.id!r}: {field} names "
                        f"unknown tool {name!r}; known tools: {', '.join(known)}"
                    )
    return registry
