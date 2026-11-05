"""The tools that ship with the program, and the registry built from them."""

from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

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

if TYPE_CHECKING:
    from llm_harness.command_executor import CommandExecutor
    from llm_harness.embeddings import Embedder
    from llm_harness.web_fetcher import WebFetcher
    from llm_harness.web_search import SearchProvider


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
    risk = "read"

    def __init__(self, registry: ToolRegistry) -> None:
        self.tags = ["orchestrator"]
        self._registry = registry

    async def run(self, arguments: ToolSearchInput, ctx: ToolContext) -> str:
        matches = self._registry.search(arguments.query, arguments.k, ctx.allowed_tools)
        if not matches:
            return f"no tool matches {arguments.query!r}"
        return "\n\n".join(_tool_block(tool) for tool in matches)


class DevEchoInput(BaseModel):
    text: str


class DevEchoTool:
    """Returns its text; exists to try the approval prompt before real write tools."""

    name = "dev_echo"
    description = "Echo the given text; a harmless write-risk tool for trying the approval prompt."
    input_model = DevEchoInput
    risk = "write"

    def __init__(self) -> None:
        self.tags = ["orchestrator"]

    async def run(self, arguments: DevEchoInput, ctx: ToolContext) -> str:
        return f"dev_echo: {arguments.text}"


_WEB_TOOL_NAMES = ("web_search", "web_fetch")
_MEMORY_TOOL_NAMES = frozenset(
    {"memory_search", "memory_save", "memory_update", "memory_forget"}
)
_COMMAND_TOOL_NAME = "run_command"
MEMORY_VECTORS_NEEDS_MEMORY = (
    "memory vector search is off: [tools] memory_vectors = true needs "
    "[tools] memory = true and a user_id"
)
MEMORY_NEEDS_USER_ID = (
    "memory is off: [tools] memory = true needs user_id, but user_id is not set; "
    'add user_id = "your-name" at the top of config.toml'
)


def _unknown_tool_hint(name: str, memory_on: bool, user_id: str | None) -> str:
    if name in _WEB_TOOL_NAMES or name == _COMMAND_TOOL_NAME:
        return f"; enable it with [tools] {name} = true"
    if name in _MEMORY_TOOL_NAMES:
        if memory_on and user_id is None:
            return "; memory is on but needs user_id, which is not set (add it at the top of config.toml)"
        return "; enable it with [tools] memory = true and set user_id"
    return ""


def build_tool_registry(
    agents: list[AgentConfig],
    *,
    dev_echo: bool = False,
    web_search: bool = False,
    web_fetch: bool = False,
    web_relax_tls_strict: bool = False,
    fetcher: "WebFetcher | None" = None,
    search_provider: "SearchProvider | None" = None,
    run_command: bool = False,
    command_allow: Sequence[str] = (),
    command_deny: Sequence[str] = (),
    command_timeout_seconds: int = 30,
    command_backend: str = "permissions",
    command_image: str | None = None,
    command_executor: "CommandExecutor | None" = None,
    memory: bool = False,
    user_id: str | None = None,
    memory_vectors: bool = False,
    embedder_factory: "Callable[[], Embedder] | None" = None,
) -> ToolRegistry:
    """The registry of built-in tools; every tool name in `agents` must exist.

    `fetcher` and `search_provider` replace the real web access (tests). The web
    modules are imported only when a web tool is switched on. `command_executor`
    replaces the real executor (tests); `command_backend` "docker" runs each command in a
    container of `command_image`, otherwise on this machine; the command modules are imported only
    when `run_command` is on. The memory tools are registered only when `memory` is on
    and `user_id` is set (memory is per user); with `memory` on and no `user_id` the
    registry says so in `notes` and a `memory_*` name in `agents` gets a hint.
    `memory_vectors` and `embedder_factory` add vector search to the memory tools; the
    factory is only called on first use. `memory_vectors` without `memory` (or without
    `user_id`) is a note, not an error.
    """
    registry = ToolRegistry()
    registry.register(ToolSearchTool(registry))
    if dev_echo:
        registry.register(DevEchoTool())
    if web_search or web_fetch:
        from llm_harness.web_fetcher import WebFetcher
        from llm_harness.web_search import DuckDuckGoHtmlProvider
        from llm_harness.web_tools import WebFetchTool, WebSearchTool

        shared_fetcher = (
            fetcher
            if fetcher is not None
            else WebFetcher(relax_tls_strict=web_relax_tls_strict)
        )
        if web_fetch:
            registry.register(WebFetchTool(shared_fetcher))
        if web_search:
            provider = (
                search_provider
                if search_provider is not None
                else DuckDuckGoHtmlProvider(shared_fetcher)
            )
            registry.register(WebSearchTool(provider))
    if run_command:
        from llm_harness.command_executor import PermissionsExecutor
        from llm_harness.command_tool import RunCommandTool

        executor: CommandExecutor
        if command_executor is not None:
            executor = command_executor
        elif command_backend == "docker":
            if not command_image:
                raise ConfigError(
                    'invalid config.toml: [tools] command_backend = "docker" needs '
                    "command_image, a local image name"
                )
            from llm_harness.docker_executor import DockerExecutor

            executor = DockerExecutor(command_image, command_allow, command_deny)
        else:
            executor = PermissionsExecutor(command_allow, command_deny)
        registry.register(RunCommandTool(executor, command_timeout_seconds))
    if memory and user_id is not None:
        from llm_harness.memory_tools import MemoryTools

        memory_tools = MemoryTools(
            vectors=memory_vectors, embedder_factory=embedder_factory
        )
        for memory_tool in memory_tools.tools():
            registry.register(memory_tool)
    elif memory:
        registry.notes.append(MEMORY_NEEDS_USER_ID)
    if memory_vectors and not (memory and user_id is not None):
        registry.notes.append(MEMORY_VECTORS_NEEDS_MEMORY)
    known = registry.names()
    for agent in agents:
        for field, names in (("tools", agent.tools), ("deny", agent.deny)):
            for name in names:
                if name not in known:
                    raise ConfigError(
                        f"invalid agents.yaml: agent {agent.id!r}: {field} names "
                        f"unknown tool {name!r}; known tools: {', '.join(known)}"
                        f"{_unknown_tool_hint(name, memory, user_id)}"
                    )
    return registry
