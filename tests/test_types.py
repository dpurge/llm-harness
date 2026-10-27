import pytest
from pydantic import ValidationError

from llm_harness.types import (
    ChatRequest,
    Done,
    ErrorEvent,
    Message,
    TextDelta,
    ThinkingDelta,
    Usage,
    UsageEvent,
)

MODELS = [
    Message(role="user", content="hi"),
    ChatRequest(model="m", messages=[]),
    Usage(),
    ThinkingDelta(text="t"),
    TextDelta(text="t"),
    UsageEvent(usage=Usage()),
    Done(),
    ErrorEvent(kind="auth", message="boom"),
]


@pytest.mark.parametrize("model", MODELS, ids=lambda m: type(m).__name__)
def test_models_are_frozen(model):
    field = next(iter(type(model).model_fields))
    with pytest.raises(ValidationError):
        setattr(model, field, getattr(model, field))


@pytest.mark.parametrize("model", MODELS, ids=lambda m: type(m).__name__)
def test_models_forbid_extra_fields(model):
    with pytest.raises(ValidationError):
        type(model)(**model.model_dump(), unexpected=1)


def test_usage_fields_default_to_none():
    usage = Usage()
    assert (usage.input_tokens, usage.output_tokens, usage.reasoning_tokens) == (
        None,
        None,
        None,
    )


def test_usage_fields_hold_values():
    usage = Usage(input_tokens=1, output_tokens=5, reasoning_tokens=2)
    assert (usage.input_tokens, usage.output_tokens, usage.reasoning_tokens) == (
        1,
        5,
        2,
    )


def test_chat_request_think_defaults_to_none():
    assert ChatRequest(model="m", messages=[]).think is None


def test_invalid_role_and_error_kind_rejected():
    with pytest.raises(ValidationError):
        Message(role="tool", content="x")
    with pytest.raises(ValidationError):
        ErrorEvent(kind="nope", message="x")
