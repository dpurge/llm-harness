"""Build the one request the orchestrator sends per turn.

Pure: the only I/O is reading the packaged instructions file. The model sees
the instructions, the state and the latest observation, never chat history.
"""

import json
from functools import cache
from importlib.resources import files

from llm_harness.config import ModelConfig, resolve_think
from llm_harness.redact import redact
from llm_harness.state import OrchestratorState, reply_schema
from llm_harness.types import ChatRequest, Message

MAX_OBSERVATION_CHARS = 4000
MAX_REJECTION_CHARS = 500
MAX_QUESTION_CHARS = 500


@cache
def _instructions() -> str:
    return (files("llm_harness") / "prompts" / "orchestrator.md").read_text("utf-8")


def _dump(schema: dict) -> str:
    return json.dumps(schema, sort_keys=True, indent=2)


@cache
def _system_prompt() -> str:
    # Generated from the models so the prompt can never drift from the contract.
    return (
        f"{_instructions().rstrip()}\n\n"
        f"## Reply schema\n\n{_dump(_reply_schema())}\n\n"
        f"## State schema\n\n{_dump(OrchestratorState.model_json_schema())}\n"
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


def build_request(
    state: OrchestratorState,
    observation: str,
    model_cfg: ModelConfig,
    think: bool | None = None,
) -> ChatRequest:
    """Two messages: system (instructions + schemas), user (state + observation)."""
    user = (
        f"## State\n\n{state.model_dump_json()}\n\n"
        f"## Observation\n\n{_truncate(observation)}\n"
    )
    return ChatRequest(
        model=model_cfg.model,
        messages=[
            Message(role="system", content=_system_prompt()),
            Message(role="user", content=user),
        ],
        think=resolve_think(think, model_cfg),
        response_schema=_reply_schema(),
    )
