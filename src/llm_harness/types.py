"""Neutral, provider-independent chat types."""

from typing import Literal

from pydantic import BaseModel, ConfigDict

Role = Literal["system", "user", "assistant"]
ErrorKind = Literal[
    "auth", "connection", "not_found", "rate_limit", "bad_request", "unknown"
]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Message(_Frozen):
    role: Role
    content: str


class ChatRequest(_Frozen):
    model: str
    messages: list[Message]
    think: bool | None = None


class Usage(_Frozen):
    """Token counts; `output_tokens` INCLUDES `reasoning_tokens`."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None


class ThinkingDelta(_Frozen):
    text: str


class TextDelta(_Frozen):
    text: str


class UsageEvent(_Frozen):
    usage: Usage


class Done(_Frozen):
    finish_reason: str | None = None


class ErrorEvent(_Frozen):
    kind: ErrorKind
    message: str


SwitchReason = ErrorKind | Literal["backoff", "config"]


class ModelSwitch(_Frozen):
    """The router moves to another model; adapters never emit this."""

    from_model_id: str
    reason: SwitchReason
    to_model_id: str


class UsageReport(_Frozen):
    """One per successful turn, just before Done; only the router emits it."""

    model_id: str
    usage: Usage | None
    cost_usd: float | None
    seconds: float
    session_cost_usd: float
    session_cost_incomplete: bool


class Notice(_Frozen):
    """A note for the user that is not part of the reply; adapters never emit it."""

    text: str


StreamEvent = (
    ThinkingDelta
    | TextDelta
    | UsageEvent
    | Done
    | ErrorEvent
    | ModelSwitch
    | UsageReport
    | Notice
)
