"""Build the one request the orchestrator sends per turn.

Pure: the only I/O is reading the packaged instructions file. The model sees
the instructions, the state and the latest observation, never chat history.
"""

import json
import re
from collections.abc import Sequence
from functools import cache
from importlib.resources import files

from llm_harness.config import ORCHESTRATOR_TAG, AgentConfig, ModelConfig, resolve_think
from llm_harness.redact import redact
from llm_harness.state import OrchestratorState, reply_schema
from llm_harness.tools import Tool
from llm_harness.types import ChatRequest, Message

MAX_OBSERVATION_CHARS = 4000
MAX_REJECTION_CHARS = 500
MAX_QUESTION_CHARS = 500


@cache
def _instructions() -> str:
    return (files("llm_harness") / "prompts" / "orchestrator.md").read_text("utf-8")


def _dump(schema: dict) -> str:
    return json.dumps(schema, sort_keys=True, indent=2)


def _with_schemas(instructions: str) -> str:
    # Generated from the models so the prompt can never drift from the contract.
    return (
        f"{instructions.rstrip()}\n\n"
        f"## Reply schema\n\n{_dump(_reply_schema())}\n\n"
        f"## State schema\n\n{_dump(OrchestratorState.model_json_schema())}\n"
    )


@cache
def _system_prompt() -> str:
    return _with_schemas(_instructions())


def subagent_instructions(agent_id: str, agent_name: str, description: str) -> str:
    """The subagent prompt with its identity filled in, ready for `build_request`.

    Single-pass replacement, not `str.format`: the prompt holds JSON braces, and a
    value that itself contains a placeholder must not be substituted again.
    """
    template = (files("llm_harness") / "prompts" / "subagent.md").read_text("utf-8")
    values = {
        "{agent_id}": agent_id,
        "{agent_name}": agent_name,
        "{description}": description,
    }
    return re.sub(
        "|".join(re.escape(key) for key in values),
        lambda match: values[match.group(0)],
        template,
    )


def _reply_schema() -> dict:
    return reply_schema()


def _truncate(observation: str) -> str:
    removed = len(observation) - MAX_OBSERVATION_CHARS
    if removed <= 0:
        return observation
    kept = observation[:MAX_OBSERVATION_CHARS]
    return f"{kept}\n[observation truncated: {removed} characters removed]"


def retry_observation(observation: str, error: str) -> str:
    """Append the rejection to the observation; the rejection always survives.

    The error text is capped and masked, and the ORIGINAL observation is cut so
    the whole stays within MAX_OBSERVATION_CHARS (build_request cuts the tail).
    """
    rejection = f"\n\nYour previous reply was rejected: {redact(error, limit=MAX_REJECTION_CHARS)}"
    room = MAX_OBSERVATION_CHARS - len(rejection)
    if len(observation) > room:
        marker = "\n[observation truncated]"
        observation = observation[: max(room - len(marker), 0)] + marker
    return observation + rejection


def answer_observation(question: str, answer: str) -> str:
    """The first observation after an ask_user: what was asked and what came back."""
    asked = redact(question, limit=MAX_QUESTION_CHARS)
    return f'You asked the user: "{asked}"\nThe user answered: {answer}'


def tool_lines(tools: Sequence[Tool]) -> str:
    """One `name: description` line per tool; the schema comes from `tool_search`."""
    return "\n".join(f"{tool.name}: {tool.description}" for tool in tools) or "(none)"


def delegation_targets(agents: Sequence[AgentConfig]) -> list[AgentConfig]:
    """The agents the orchestrator may delegate to: not itself, and with examples.

    The one place the rule lives, so the listing and the unknown-id check agree.
    """
    return [a for a in agents if ORCHESTRATOR_TAG not in a.tags and a.examples]


def subagent_lines(agents: Sequence[AgentConfig]) -> str:
    """One `id: description (e.g. a; b)` line per delegation target."""
    return (
        "\n".join(
            f"{a.id}: {a.description} (e.g. {'; '.join(a.examples)})"
            for a in delegation_targets(agents)
        )
        or "(none)"
    )


def build_request(
    state: OrchestratorState,
    observation: str,
    model_cfg: ModelConfig,
    think: bool | None = None,
    tools: Sequence[Tool] = (),
    instructions: str | None = None,
    subagents: Sequence[AgentConfig] = (),
) -> ChatRequest:
    """Two messages: system (instructions + schemas), user (state, tools, observation).

    A `## Subagents` section follows the tools only when `subagents` holds at
    least one delegation target; otherwise the message is unchanged.

    `instructions` replaces the orchestrator prompt (a subagent passes its own);
    the schema sections are appended either way.
    """
    targets = delegation_targets(subagents)
    subagent_section = (
        f"## Subagents\n\n{subagent_lines(targets)}\n\n" if targets else ""
    )
    user = (
        f"## State\n\n{state.model_dump_json()}\n\n"
        f"## Tools\n\n{tool_lines(tools)}\n\n"
        f"{subagent_section}"
        f"## Observation\n\n{_truncate(observation)}\n"
    )
    system = _system_prompt() if instructions is None else _with_schemas(instructions)
    return ChatRequest(
        model=model_cfg.model,
        messages=[
            Message(role="system", content=system),
            Message(role="user", content=user),
        ],
        think=resolve_think(think, model_cfg),
        response_schema=_reply_schema(),
    )
