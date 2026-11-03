"""Typed tools and their registry: registration rules, tag matching, search.

A tool is described by a name, a one-line description, tags and a pydantic
input model; its result is a string that becomes the next observation.
"""

import json
import math
import re
from collections.abc import Collection
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol

from pydantic import BaseModel

from llm_harness.config import AgentConfig

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
    # The names of the tools the calling agent may use.
    allowed_tools: frozenset[str] = frozenset()


Risk = Literal["read", "write", "exec"]
_RISKS: tuple[str, ...] = ("read", "write", "exec")


class Tool(Protocol):
    name: str
    description: str
    tags: list[str]
    input_model: type[BaseModel]
    risk: Risk
    # Optional (so not declared here): `prompts: bool`, default True. A non-read tool
    # with `prompts = False` runs without the approval prompt and shows a one-line
    # notice with its arguments instead; only for writes the user's own `[tools]`
    # switch already allows (agent memory). `tool_prompts` reads it.
    # Optional: `notify_result: bool`, default False. A tool with `notify_result = True`
    # also shows its successful result as a one-line notice. `tool_notifies` reads it.

    async def run(self, arguments: Any, ctx: ToolContext) -> str: ...


def tool_prompts(tool: Tool) -> bool:
    """False only for a tool that declares `prompts = False`; every other tool asks."""
    return getattr(tool, "prompts", True) is not False


def tool_notifies(tool: Tool) -> bool:
    """True only for a tool that declares `notify_result = True`."""
    return getattr(tool, "notify_result", False) is True


_NAME_RULE = re.compile(r"[a-z][a-z0-9_]*")
_WORDS = re.compile(r"[a-z0-9]+")
MAX_SEARCH_K = 10
# Weights of a query word found in the name, in a tag, in the description.
_NAME_WEIGHT, _TAG_WEIGHT, _DESCRIPTION_WEIGHT = 3, 2, 1


def estimate_tokens(text: str) -> int:
    """A rough token count (4 characters per token); an estimate, not a tokenizer."""
    return math.ceil(len(text) / 4)


def schema_text(tool: Tool) -> str:
    """The tool's input JSON schema as compact JSON, as it would be sent."""
    return json.dumps(
        tool.input_model.model_json_schema(), sort_keys=True, separators=(",", ":")
    )


def _words(text: str) -> set[str]:
    return set(_WORDS.findall(text.lower()))


def _check_tool(tool: Tool) -> None:
    name = tool.name
    if not _NAME_RULE.fullmatch(name):
        raise ValueError(
            f"tool {name!r}: the name must be lowercase letters, digits and "
            "underscores, starting with a letter."
        )
    description = tool.description
    if not description.strip() or "\n" in description.strip():
        raise ValueError(f"tool {name!r}: the description must be one non-empty line.")
    for tag in tool.tags:
        if not tag or tag != tag.lower() or any(c.isspace() for c in tag):
            raise ValueError(
                f"tool {name!r}: tag {tag!r} must be non-empty, lowercase and "
                "contain no spaces."
            )
    if len(set(tool.tags)) != len(tool.tags):
        raise ValueError(f"tool {name!r}: duplicate tags.")
    if not (
        isinstance(tool.input_model, type) and issubclass(tool.input_model, BaseModel)
    ):
        raise TypeError(f"tool {name!r}: input_model must be a pydantic model class.")
    risk = getattr(tool, "risk", None)
    if risk not in _RISKS:
        raise ValueError(
            f"tool {name!r}: risk must be one of {', '.join(_RISKS)}, got {risk!r}."
        )
    prompts = getattr(tool, "prompts", True)
    if not isinstance(prompts, bool):
        raise TypeError(f"tool {name!r}: prompts must be a bool, got {prompts!r}.")
    notify_result = getattr(tool, "notify_result", False)
    if not isinstance(notify_result, bool):
        raise TypeError(
            f"tool {name!r}: notify_result must be a bool, got {notify_result!r}."
        )


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        # Why a tool group is off although the config asks for it; the State tab shows it.
        self.notes: list[str] = []

    def register(self, tool: Tool) -> None:
        _check_tool(tool)
        if tool.name in self._tools:
            raise ValueError(
                f"tool {tool.name!r} is already registered; tool names must be unique."
            )
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def all(self) -> list[Tool]:
        return [tool for _, tool in sorted(self._tools.items())]

    def tools_for(self, agent: AgentConfig) -> list[Tool]:
        """Tools whose tags meet the agent's, plus its `tools`, minus its `deny`."""
        wanted = set(agent.tags)
        chosen = [
            tool
            for name, tool in sorted(self._tools.items())
            if name not in agent.deny
            and (wanted & set(tool.tags) or name in agent.tools)
        ]
        return chosen

    def search(
        self, query: str, k: int, names: Collection[str] | None = None
    ) -> list[Tool]:
        """Rank tools (only `names`, when given) by the words of `query`.

        A query word counts once, at the highest weight of where it is found.
        Tools with no match are left out; ties break by name.
        """
        query_words = _words(query)
        scored = []
        for name, tool in self._tools.items():
            if names is not None and name not in names:
                continue
            name_words = _words(name)
            tag_words = set().union(*(_words(tag) for tag in tool.tags))
            description_words = _words(tool.description)
            score = sum(
                _NAME_WEIGHT
                if word in name_words
                else _TAG_WEIGHT
                if word in tag_words
                else _DESCRIPTION_WEIGHT
                if word in description_words
                else 0
                for word in query_words
            )
            if score:
                scored.append((-score, name, tool))
        scored.sort(key=lambda item: item[:2])
        return [tool for _, _, tool in scored[: max(0, min(k, MAX_SEARCH_K))]]
