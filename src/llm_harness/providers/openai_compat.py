"""OpenAI-compatible adapter (OpenAI, OpenRouter, Ollama `/v1`) on the `openai` SDK."""

from collections.abc import AsyncIterator
from contextlib import aclosing
from typing import Any

import openai
from openai.types.chat import ChatCompletionChunk

from llm_harness.config import ConfigError, ProviderConfig
from llm_harness.providers.base import ChatProvider
from llm_harness.providers.environment import hidden_environment
from llm_harness.redact import Secret, redact
from llm_harness.types import (
    ChatRequest,
    Done,
    ErrorEvent,
    ErrorKind,
    StreamEvent,
    TextDelta,
    ThinkingDelta,
    Usage,
    UsageEvent,
)

_NO_FINISH_REASON = "the stream ended without a finish reason"


def _thinking_kwargs(reasoning_style: str, think: bool | None) -> dict[str, Any]:
    if think is None:
        return {}
    if reasoning_style == "openrouter":
        return {"extra_body": {"reasoning": {"enabled": think}}}
    return {"reasoning_effort": "medium" if think else "none"}


def _details_text(details: list[Any]) -> str:
    """Concatenate OpenRouter `reasoning_details`; encrypted and bad entries are skipped."""
    parts: list[str] = []
    for entry in details:
        if not isinstance(entry, dict):
            continue
        field = {"reasoning.text": "text", "reasoning.summary": "summary"}.get(
            entry.get("type")
        )
        value = entry.get(field) if field else None
        if isinstance(value, str):
            parts.append(value)
    return "".join(parts)


def _thinking_text(extra: dict[str, Any]) -> str:
    """One source per chunk: reasoning_details, else reasoning, else reasoning_content."""
    details = extra.get("reasoning_details")
    if isinstance(details, list) and details:
        return _details_text(details)
    for name in ("reasoning", "reasoning_content"):
        value = extra.get(name)
        if isinstance(value, str):
            return value
    return ""


def _error_kind(exc: openai.OpenAIError) -> ErrorKind:
    if isinstance(exc, (openai.AuthenticationError, openai.PermissionDeniedError)):
        return "auth"
    if isinstance(exc, openai.NotFoundError):
        return "not_found"
    if isinstance(exc, openai.RateLimitError):
        return "rate_limit"
    if isinstance(exc, openai.BadRequestError):
        return "bad_request"
    if isinstance(exc, openai.APIConnectionError):
        return "connection"
    return "unknown"


def _usage_event(usage: Any) -> UsageEvent:
    details = usage.completion_tokens_details
    return UsageEvent(
        usage=Usage(
            input_tokens=usage.prompt_tokens,
            output_tokens=usage.completion_tokens,
            reasoning_tokens=details.reasoning_tokens if details is not None else None,
        )
    )


class OpenAICompatProvider:
    def __init__(
        self, client: openai.AsyncOpenAI, reasoning_style: str, secrets: list[str]
    ) -> None:
        self._client = client
        self._reasoning_style = reasoning_style
        self._secrets = secrets

    async def stream_chat(self, req: ChatRequest) -> AsyncIterator[StreamEvent]:
        messages = [{"role": m.role, "content": m.content} for m in req.messages]
        try:
            stream = await self._client.chat.completions.create(
                model=req.model,
                messages=messages,
                stream=True,
                stream_options={"include_usage": True},
                **_thinking_kwargs(self._reasoning_style, req.think),
            )
            finish_reason: str | None = None
            usage: Any = None
            async with aclosing(stream):
                chunk: ChatCompletionChunk
                async for chunk in stream:
                    if chunk.usage is not None:
                        usage = chunk.usage
                    if not chunk.choices:
                        continue
                    choice = chunk.choices[0]
                    if choice.finish_reason is not None:
                        finish_reason = choice.finish_reason
                    thinking = _thinking_text(choice.delta.model_extra or {})
                    if thinking:
                        yield ThinkingDelta(text=thinking)
                    if choice.delta.content:
                        yield TextDelta(text=choice.delta.content)
            # Usage arrives after the chunk with the finish reason, so both
            # are held back until the stream ends.
            if usage is not None:
                yield _usage_event(usage)
            if finish_reason is None:
                yield ErrorEvent(kind="unknown", message=_NO_FINISH_REASON)
            else:
                yield Done(finish_reason=finish_reason)
        except openai.OpenAIError as exc:
            message = redact(str(exc), secrets=self._secrets)
            yield ErrorEvent(kind=_error_kind(exc), message=message)

    async def aclose(self) -> None:
        await self._client.close()


def create(config: ProviderConfig, api_key: Secret | None) -> ChatProvider:
    if api_key is None:
        raise ConfigError(f"{config.type} provider requires an API key")
    key = api_key.reveal()
    # The constructor falls back to OPENAI_ORG_ID, OPENAI_PROJECT_ID,
    # OPENAI_CUSTOM_HEADERS (which can carry an Authorization header that
    # overrides our key) and others; only some can be blocked through arguments.
    with hidden_environment(prefixes=("OPENAI_",)):
        # max_retries=0: fail fast and deterministically; retries belong to the
        # later fallback feature.
        client = openai.AsyncOpenAI(
            api_key=key, base_url=config.base_url, max_retries=0
        )
    return OpenAICompatProvider(client, config.reasoning_style or "openai", [key])
