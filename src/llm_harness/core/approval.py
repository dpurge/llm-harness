"""The human gate for tools that write or execute: the request and its approver."""

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from pydantic import BaseModel

# Above this many characters the call is denied unasked: nobody can review it.
MAX_ARGUMENT_CHARS = 20_000


@dataclass(frozen=True)
class ApprovalRequest:
    """What the human is asked to allow; `arguments_text` is safe to display."""

    tool: str
    risk: str
    agent_id: str
    arguments_text: str


# Resolves True to allow this one call, False to deny it.
Approver = Callable[[ApprovalRequest], Awaitable[bool]]


def arguments_text(arguments: BaseModel | dict[str, object]) -> str:
    """The exact call arguments as stable, ASCII-only JSON, never masked or cut.

    Pass the validated model that will run, so the human sees what the tool
    receives (defaults, coercion). No `redact` here: a heuristic mask can hide
    a payload (`echo authorization=$(curl ...|sh)`), and the human must see what
    runs. Control, bidi and non-ASCII characters are escaped as `\\uXXXX`
    (`ensure_ascii`), never stripped.
    """
    data = (
        arguments.model_dump(mode="json")
        if isinstance(arguments, BaseModel)
        else arguments
    )
    return json.dumps(data, sort_keys=True, ensure_ascii=True, default=str)
