"""Vertex AI adapter: maps the `google-genai` stream (Vertex mode) to neutral events."""

from collections.abc import AsyncIterator
from contextlib import aclosing

import aiohttp
import httpx
from google import genai
from google.auth import exceptions as auth_exceptions
from google.genai import errors, types

from llm_harness.config import ProviderConfig
from llm_harness.providers.base import ChatProvider
from llm_harness.providers.environment import hidden_environment
from llm_harness.redact import Secret, redact
from llm_harness.types import (
    ChatRequest,
    Done,
    ErrorEvent,
    ErrorKind,
    Message,
    StreamEvent,
    TextDelta,
    ThinkingDelta,
    Usage,
    UsageEvent,
)

_STATUS_KINDS: dict[int, ErrorKind] = {
    400: "bad_request",
    401: "auth",
    403: "auth",
    404: "not_found",
    429: "rate_limit",
}
# google-genai uses aiohttp as its HTTP backend when it is installed (httpx
# otherwise), so network failures arrive as aiohttp errors. ClientConnectionError
# covers refused/reset/DNS/disconnect/server timeout; ClientPayloadError is a
# mid-stream cut; TimeoutError is aiohttp's total/read timeout. Other
# aiohttp.ClientError subclasses are not network failures and map to unknown.
_CONNECTION_ERRORS = (
    aiohttp.ClientConnectionError,
    aiohttp.ClientPayloadError,
    httpx.TransportError,
    ConnectionError,
    TimeoutError,
)
_NO_FINISH_REASON = "the stream ended without a finish reason"

# Variables the SDK constructor reads and that would override or add to the
# config (found by grepping google-genai 2.33.0). GOOGLE_APPLICATION_CREDENTIALS
# and CLOUDSDK_CONFIG are deliberately absent: they are the ADC inputs.
_HIDDEN_VARIABLES = (
    "GOOGLE_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_CLOUD_PROJECT",
    "GOOGLE_CLOUD_LOCATION",
    "GOOGLE_GENAI_USE_VERTEXAI",
    "GOOGLE_GENAI_USE_ENTERPRISE",
    "GOOGLE_VERTEX_BASE_URL",
    "GOOGLE_GEMINI_BASE_URL",
    "GOOGLE_GENAI_CLIENT_MODE",
    "GOOGLE_GENAI_REPLAYS_DIRECTORY",
    "GOOGLE_GENAI_REPLAY_ID",
)


def _error(kind: ErrorKind, exc: BaseException | str) -> ErrorEvent:
    return ErrorEvent(kind=kind, message=redact(str(exc)))


def _thinking_config(think: bool | None) -> types.ThinkingConfig | None:
    if think is None:
        return None
    if think:
        # flash-lite rejects include_thoughts without a budget (400); -1 is dynamic.
        return types.ThinkingConfig(include_thoughts=True, thinking_budget=-1)
    return types.ThinkingConfig(include_thoughts=False, thinking_budget=0)


def _generation_config(req: ChatRequest) -> types.GenerateContentConfig:
    system = "\n".join(m.content for m in req.messages if m.role == "system")
    return types.GenerateContentConfig(
        system_instruction=system or None,
        thinking_config=_thinking_config(req.think),
    )


def _contents(messages: list[Message]) -> list[types.Content]:
    return [
        types.Content(
            role="model" if m.role == "assistant" else "user",
            parts=[types.Part(text=m.content)],
        )
        for m in messages
        if m.role != "system"
    ]


def _usage_event(meta: types.GenerateContentResponseUsageMetadata) -> UsageEvent:
    # candidates_token_count excludes thought tokens (verified live: total =
    # prompt + candidates + thoughts), so output is their sum.
    candidates, thoughts = meta.candidates_token_count, meta.thoughts_token_count
    output = None
    if candidates is not None or thoughts is not None:
        output = (candidates or 0) + (thoughts or 0)
    return UsageEvent(
        usage=Usage(
            input_tokens=meta.prompt_token_count,
            output_tokens=output,
            reasoning_tokens=thoughts,
        )
    )


class VertexProvider:
    def __init__(self, client: genai.Client) -> None:
        self._client = client

    async def stream_chat(self, req: ChatRequest) -> AsyncIterator[StreamEvent]:
        try:
            stream = await self._client.aio.models.generate_content_stream(
                model=req.model,
                contents=_contents(req.messages),
                config=_generation_config(req),
            )
            finish_reason: str | None = None
            usage: types.GenerateContentResponseUsageMetadata | None = None
            async with aclosing(stream):
                async for chunk in stream:
                    if chunk.usage_metadata is not None:
                        usage = chunk.usage_metadata
                    if not chunk.candidates:
                        continue
                    candidate = chunk.candidates[0]
                    if candidate.finish_reason is not None:
                        finish_reason = candidate.finish_reason.name
                    parts = candidate.content.parts if candidate.content else None
                    for part in parts or []:
                        if not part.text:
                            continue
                        if part.thought:
                            yield ThinkingDelta(text=part.text)
                        else:
                            yield TextDelta(text=part.text)
            # Usage arrives with the finish reason on the last chunk; both are
            # held back so the usage event precedes the terminal event.
            if usage is not None:
                yield _usage_event(usage)
            if finish_reason is None:
                yield _error("unknown", _NO_FINISH_REASON)
            else:
                yield Done(finish_reason=finish_reason)
        except errors.APIError as exc:
            yield _error(_STATUS_KINDS.get(exc.code, "unknown"), exc)
        except auth_exceptions.TransportError as exc:
            yield _error("connection", exc)
        except auth_exceptions.GoogleAuthError as exc:
            yield _error("auth", exc)
        except _CONNECTION_ERRORS as exc:
            yield _error("connection", exc)
        except aiohttp.ClientError as exc:
            yield _error("unknown", exc)

    async def aclose(self) -> None:
        await self._client.aio.aclose()
        self._client.close()


def create(config: ProviderConfig, api_key: Secret | None) -> ChatProvider:
    # Vertex uses ADC only (config validation forbids keys and base_url); the
    # parameter keeps the factory signature uniform across adapters.
    del api_key
    # Project and location always come from the config, never from the
    # ambient environment (criterion 14).
    with hidden_environment(names=_HIDDEN_VARIABLES):
        client = genai.Client(
            vertexai=True, project=config.project, location=config.location
        )
    return VertexProvider(client)
