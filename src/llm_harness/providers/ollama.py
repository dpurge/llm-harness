"""Ollama adapter: maps the `ollama` SDK stream to neutral events."""

from collections.abc import AsyncIterator
from contextlib import aclosing

import httpx
import ollama

from llm_harness.config import ProviderConfig
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

_STATUS_KINDS: dict[int, ErrorKind] = {
    400: "bad_request",
    401: "auth",
    403: "auth",
    404: "not_found",
    429: "rate_limit",
}
# The only variables the ollama client reads (grepped in ollama 0.6.3:
# _client.py); OLLAMA_API_KEY at line 712 is in a separate web-search call.
_HIDDEN_VARIABLES = ("OLLAMA_API_KEY", "OLLAMA_HOST")
_NO_DONE = "the Ollama stream ended without a final chunk"


def _error(kind: ErrorKind, exc: BaseException | str) -> ErrorEvent:
    return ErrorEvent(kind=kind, message=redact(str(exc)))


class OllamaProvider:
    def __init__(self, client: ollama.AsyncClient) -> None:
        self._client = client

    async def stream_chat(self, req: ChatRequest) -> AsyncIterator[StreamEvent]:
        messages = [{"role": m.role, "content": m.content} for m in req.messages]
        try:
            stream = await self._client.chat(
                model=req.model, messages=messages, stream=True, think=req.think
            )
            async with aclosing(stream):
                async for chunk in stream:
                    if chunk.message.thinking:
                        yield ThinkingDelta(text=chunk.message.thinking)
                    if chunk.message.content:
                        yield TextDelta(text=chunk.message.content)
                    if chunk.done:
                        # Usage is on the final chunk only; omit it if absent.
                        if (
                            chunk.prompt_eval_count is not None
                            or chunk.eval_count is not None
                        ):
                            yield UsageEvent(
                                usage=Usage(
                                    input_tokens=chunk.prompt_eval_count,
                                    output_tokens=chunk.eval_count,
                                )
                            )
                        yield Done(finish_reason=chunk.done_reason)
                        return
            yield _error("unknown", _NO_DONE)
        except ollama.ResponseError as exc:
            yield _error(_STATUS_KINDS.get(exc.status_code, "unknown"), exc)
        except ollama.RequestError as exc:
            yield _error("bad_request", exc)
        except (httpx.TransportError, ConnectionError) as exc:
            yield _error("connection", exc)

    async def aclose(self) -> None:
        await self._client.close()


def create(config: ProviderConfig, api_key: Secret | None) -> ChatProvider:
    # Ollama takes no key in this slice; the parameter keeps the factory
    # signature uniform across adapters.
    del api_key
    # The constructor sends OLLAMA_API_KEY as a bearer token to whatever host
    # is configured, and falls back to OLLAMA_HOST; the config alone decides
    # both (criterion 14).
    with hidden_environment(names=_HIDDEN_VARIABLES):
        client = ollama.AsyncClient(host=config.base_url)
    return OllamaProvider(client)
