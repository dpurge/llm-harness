"""OpenAI-compatible adapter tests: fake client, real SDK chunk and error types."""

import asyncio
import os
from typing import Any

import httpx2
import openai
import pytest
from openai.types import CreateEmbeddingResponse
from openai.types.chat import ChatCompletionChunk

from llm_harness.config import ConfigError, ProviderConfig
from llm_harness.embeddings import EmbeddingError
from llm_harness.providers.openai_compat import OpenAICompatProvider, create
from llm_harness.redact import Secret
from llm_harness.types import (
    ChatRequest,
    Done,
    ErrorEvent,
    Message,
    StreamEvent,
    TextDelta,
    ThinkingDelta,
    Usage,
    UsageEvent,
)
from tests.fakes import assert_stream_contract
from tests.test_vertex_adapter import closed_local_port


def chunk(
    content: str | None = None,
    *,
    finish: str | None = None,
    usage: dict[str, Any] | None = None,
    choices: bool = True,
    **delta_extras: Any,
) -> ChatCompletionChunk:
    delta: dict[str, Any] = {"content": content, **delta_extras}
    data: dict[str, Any] = {
        "id": "c1",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "m",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]
        if choices
        else [],
    }
    if usage is not None:
        data["usage"] = usage
    return ChatCompletionChunk.model_validate(data)


def usage_chunk(
    prompt: int = 7, completion: int = 3, reasoning: int | None = None
) -> ChatCompletionChunk:
    usage: dict[str, Any] = {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
    }
    if reasoning is not None:
        usage["completion_tokens_details"] = {"reasoning_tokens": reasoning}
    return chunk(choices=False, usage=usage)


class FakeStream:
    """Async iterator over chunks; may raise or hang; records `aclose`."""

    def __init__(
        self,
        items: list[ChatCompletionChunk],
        *,
        raises: Exception | None = None,
        hang: bool = False,
    ) -> None:
        self._items = iter(items)
        self._raises = raises
        self._hang = hang
        self.closed = False

    def __aiter__(self) -> "FakeStream":
        return self

    async def __anext__(self) -> ChatCompletionChunk:
        for item in self._items:
            return item
        if self._raises is not None:
            raise self._raises
        if self._hang:
            await asyncio.Event().wait()
        raise StopAsyncIteration

    async def aclose(self) -> None:
        self.closed = True


class _Completions:
    def __init__(self, owner: "FakeClient") -> None:
        self._owner = owner

    async def create(self, **kwargs: Any) -> FakeStream:
        self._owner.calls.append(kwargs)
        if self._owner.create_raises is not None:
            raise self._owner.create_raises
        assert self._owner.stream is not None
        return self._owner.stream


class _Chat:
    def __init__(self, owner: "FakeClient") -> None:
        self.completions = _Completions(owner)


class FakeClient:
    def __init__(
        self,
        stream: FakeStream | None = None,
        *,
        create_raises: Exception | None = None,
    ) -> None:
        self.stream = stream
        self.create_raises = create_raises
        self.calls: list[dict[str, Any]] = []
        self.closed = False
        self.chat = _Chat(self)

    async def close(self) -> None:
        self.closed = True


def request(think: bool | None = None) -> ChatRequest:
    return ChatRequest(
        model="gemma",
        messages=[
            Message(role="user", content="one"),
            Message(role="assistant", content="two"),
            Message(role="user", content="three"),
        ],
        think=think,
    )


def make_provider(
    client: FakeClient, style: str = "openai", secrets: list[str] | None = None
) -> OpenAICompatProvider:
    return OpenAICompatProvider(client, style, secrets or [])  # type: ignore[arg-type]


async def collect(
    client: FakeClient, think: bool | None = None, style: str = "openai"
) -> list[StreamEvent]:
    provider = make_provider(client, style)
    return [event async for event in provider.stream_chat(request(think))]


def finished(*chunks: ChatCompletionChunk) -> FakeClient:
    return FakeClient(FakeStream([*chunks, chunk(finish="stop"), usage_chunk()]))


def status_error(cls: type[openai.APIStatusError], status: int, text: str = "boom"):
    http_request = httpx2.Request("POST", "http://localhost/v1/chat/completions")
    response = httpx2.Response(status, request=http_request)
    return cls(text, response=response, body=None)


# --- request mapping ---------------------------------------------------------


async def test_request_is_mapped_and_streamed() -> None:
    client = finished()
    await collect(client)
    assert client.calls == [
        {
            "model": "gemma",
            "messages": [
                {"role": "user", "content": "one"},
                {"role": "assistant", "content": "two"},
                {"role": "user", "content": "three"},
            ],
            "stream": True,
            "stream_options": {"include_usage": True},
        }
    ]


async def test_response_schema_is_ignored() -> None:
    client = finished()
    provider = make_provider(client)
    req = request().model_copy(update={"response_schema": {"type": "object"}})
    [event async for event in provider.stream_chat(req)]
    plain = finished()
    await collect(plain)
    assert client.calls == plain.calls


@pytest.mark.parametrize(
    ("style", "think", "extra"),
    [
        ("openai", False, {"reasoning_effort": "none"}),
        ("openai", True, {"reasoning_effort": "medium"}),
        ("openai", None, {}),
        ("openrouter", False, {"extra_body": {"reasoning": {"enabled": False}}}),
        ("openrouter", True, {"extra_body": {"reasoning": {"enabled": True}}}),
        ("openrouter", None, {}),
    ],
)
async def test_thinking_toggle_per_style(
    style: str, think: bool | None, extra: dict[str, Any]
) -> None:
    client = finished()
    await collect(client, think, style)
    call = client.calls[0]
    assert call["stream"] is True
    assert call["stream_options"] == {"include_usage": True}
    sent = {
        k: v
        for k, v in call.items()
        if k not in {"model", "messages", "stream", "stream_options"}
    }
    assert sent == extra  # no reasoning keys for None, and no token cap ever


# --- stream mapping ----------------------------------------------------------


async def test_event_order_thinking_text_usage_done() -> None:
    stream = FakeStream(
        [
            chunk(reasoning="hm"),
            chunk(reasoning="m"),
            chunk("Hel"),
            chunk("lo"),
            chunk(finish="stop"),
            usage_chunk(7, 3),
        ]
    )
    events = await collect(FakeClient(stream))
    assert events == [
        ThinkingDelta(text="hm"),
        ThinkingDelta(text="m"),
        TextDelta(text="Hel"),
        TextDelta(text="lo"),
        UsageEvent(usage=Usage(input_tokens=7, output_tokens=3)),
        Done(finish_reason="stop"),
    ]
    assert_stream_contract(events)
    assert stream.closed


async def test_extra_delta_fields_land_in_model_extra() -> None:
    # The adapter reads thinking from model_extra; pin that the SDK puts it there.
    parsed = chunk(reasoning="r")
    assert parsed.choices[0].delta.model_extra == {"reasoning": "r"}


async def test_empty_and_none_deltas_produce_nothing() -> None:
    events = await collect(
        finished(chunk(""), chunk(None), chunk("", reasoning=""), chunk("x"))
    )
    assert events[0] == TextDelta(text="x")
    assert [type(e) for e in events] == [TextDelta, UsageEvent, Done]


async def test_chunk_with_empty_choices_is_skipped() -> None:
    events = await collect(finished(chunk(choices=False)))
    assert events == [
        UsageEvent(usage=Usage(input_tokens=7, output_tokens=3)),
        Done(finish_reason="stop"),
    ]


async def test_thinking_from_reasoning_content() -> None:
    events = await collect(finished(chunk(reasoning_content="rc")))
    assert events[0] == ThinkingDelta(text="rc")


async def test_thinking_from_reasoning_details_concatenates_in_order() -> None:
    details = [
        {"type": "reasoning.text", "text": "a"},
        {"type": "reasoning.summary", "summary": "b"},
        {"type": "reasoning.encrypted", "data": "SECRET-BLOB"},
        {"type": "reasoning.text", "text": "c"},
        {"type": "reasoning.text"},  # no text
        {"type": "reasoning.text", "text": 5},  # wrong type
        {"type": "reasoning.unknown", "text": "zzz"},
        "not a dict",
        {"text": "no type"},
    ]
    events = await collect(finished(chunk(reasoning_details=details)))
    assert events[0] == ThinkingDelta(text="abc")
    assert sum(isinstance(e, ThinkingDelta) for e in events) == 1


async def test_only_encrypted_details_produce_no_thinking() -> None:
    details = [{"type": "reasoning.encrypted", "data": "blob"}]
    events = await collect(finished(chunk(reasoning_details=details)))
    assert not any(isinstance(e, ThinkingDelta) for e in events)


@pytest.mark.parametrize(
    ("extras", "expected"),
    [
        (
            {
                "reasoning_details": [{"type": "reasoning.text", "text": "D"}],
                "reasoning": "R",
                "reasoning_content": "C",
            },
            "D",
        ),
        ({"reasoning": "R", "reasoning_content": "C"}, "R"),
        ({"reasoning_details": [], "reasoning": "R"}, "R"),
        ({"reasoning_details": "bad", "reasoning_content": "C"}, "C"),
        ({"reasoning": None, "reasoning_content": "C"}, "C"),
    ],
)
async def test_thinking_precedence_uses_one_source_per_chunk(
    extras: dict[str, Any], expected: str
) -> None:
    events = await collect(finished(chunk(**extras)))
    thinking = [e for e in events if isinstance(e, ThinkingDelta)]
    assert thinking == [ThinkingDelta(text=expected)]


@pytest.mark.parametrize(
    ("reasoning", "expected"),
    [(5, 5), (None, None)],
)
async def test_usage_reasoning_tokens_present_or_absent(
    reasoning: int | None, expected: int | None
) -> None:
    stream = FakeStream(
        [chunk("a"), chunk(finish="stop"), usage_chunk(4, 9, reasoning)]
    )
    events = await collect(FakeClient(stream))
    assert events[1] == UsageEvent(
        usage=Usage(input_tokens=4, output_tokens=9, reasoning_tokens=expected)
    )


async def test_usage_with_null_details_has_no_reasoning_tokens() -> None:
    last = chunk(
        choices=False,
        usage={
            "prompt_tokens": 1,
            "completion_tokens": 2,
            "total_tokens": 3,
            "completion_tokens_details": None,
        },
    )
    events = await collect(FakeClient(FakeStream([chunk(finish="stop"), last])))
    assert events[0] == UsageEvent(usage=Usage(input_tokens=1, output_tokens=2))


async def test_no_usage_event_when_the_server_sends_none() -> None:
    events = await collect(FakeClient(FakeStream([chunk("a"), chunk(finish="stop")])))
    assert events == [TextDelta(text="a"), Done(finish_reason="stop")]


async def test_done_comes_after_usage_even_though_finish_reason_is_earlier() -> None:
    events = await collect(finished(chunk("a")))
    assert isinstance(events[-1], Done)
    assert isinstance(events[-2], UsageEvent)


@pytest.mark.parametrize("reason", ["stop", "length", "content_filter"])
async def test_finish_reason_passes_through(reason: str) -> None:
    stream = FakeStream([chunk("a", finish=reason), usage_chunk()])
    events = await collect(FakeClient(stream))
    assert events[-1] == Done(finish_reason=reason)


async def test_stream_without_finish_reason_is_an_error_and_no_done() -> None:
    events = await collect(FakeClient(FakeStream([chunk("a"), usage_chunk()])))
    assert events[-1] == ErrorEvent(
        kind="unknown", message="the stream ended without a finish reason"
    )
    assert not any(isinstance(e, Done) for e in events)
    assert_stream_contract(events)


# --- errors ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cls", "status", "kind"),
    [
        (openai.AuthenticationError, 401, "auth"),
        (openai.PermissionDeniedError, 403, "auth"),
        (openai.NotFoundError, 404, "not_found"),
        (openai.RateLimitError, 429, "rate_limit"),
        (openai.BadRequestError, 400, "bad_request"),
        (openai.APIStatusError, 500, "unknown"),
    ],
)
async def test_status_errors_map_to_kind(
    cls: type[openai.APIStatusError], status: int, kind: str
) -> None:
    client = FakeClient(create_raises=status_error(cls, status, "boom"))
    events = await collect(client)
    assert events == [ErrorEvent(kind=kind, message="boom")]
    assert_stream_contract(events)


@pytest.mark.parametrize(
    "exc",
    [
        openai.APIConnectionError(request=httpx2.Request("POST", "http://x")),
        openai.APITimeoutError(httpx2.Request("POST", "http://x")),
    ],
)
async def test_connection_and_timeout_errors_are_connection(exc: Exception) -> None:
    events = await collect(FakeClient(create_raises=exc))
    assert len(events) == 1
    assert isinstance(events[0], ErrorEvent)
    assert events[0].kind == "connection"


async def test_other_sdk_errors_are_unknown() -> None:
    events = await collect(FakeClient(create_raises=openai.OpenAIError("odd")))
    assert events == [ErrorEvent(kind="unknown", message="odd")]


async def test_error_mid_stream_ends_with_one_error_and_no_done() -> None:
    stream = FakeStream(
        [chunk("part"), chunk(reasoning="t")],
        raises=status_error(openai.APIStatusError, 500, "died"),
    )
    events = await collect(FakeClient(stream))
    assert events[:2] == [TextDelta(text="part"), ThinkingDelta(text="t")]
    assert events[-1] == ErrorEvent(kind="unknown", message="died")
    assert not any(isinstance(e, (Done, UsageEvent)) for e in events)
    assert_stream_contract(events)
    assert stream.closed


async def test_programming_errors_are_not_swallowed() -> None:
    stream = FakeStream([chunk("a")], raises=TypeError("our bug"))
    with pytest.raises(TypeError, match="our bug"):
        await collect(FakeClient(stream))


async def test_error_message_is_redacted_and_truncated() -> None:
    key = "my-plain-key-value"
    shape = "sk-abcdefghij1234567890"
    text = f"bad key {key} and {shape} " + "x" * 500
    provider = make_provider(
        FakeClient(create_raises=status_error(openai.AuthenticationError, 401, text)),
        secrets=[key],
    )
    events = [e async for e in provider.stream_chat(request())]
    assert isinstance(events[0], ErrorEvent)
    message = events[0].message
    assert key not in message
    assert shape not in message
    assert "[REDACTED:api_key]" in message
    assert len(message) <= 301


# --- cancellation and close --------------------------------------------------


async def test_cancel_closes_the_sdk_stream_and_propagates() -> None:
    stream = FakeStream([chunk("a")], hang=True)
    provider = make_provider(FakeClient(stream))
    seen: list[StreamEvent] = []

    async def consume() -> None:
        async for event in provider.stream_chat(request()):
            seen.append(event)

    task = asyncio.create_task(consume())
    while not seen:
        await asyncio.sleep(0)
    await asyncio.sleep(0)  # let the consumer block on the hanging stream
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stream.closed


async def test_early_exit_closes_the_sdk_stream() -> None:
    stream = FakeStream([chunk("a"), chunk("b"), chunk(finish="stop")])
    provider = make_provider(FakeClient(stream))
    events = provider.stream_chat(request())
    assert await anext(events) == TextDelta(text="a")
    await events.aclose()  # type: ignore[attr-defined]
    assert stream.closed


async def test_aclose_closes_the_client() -> None:
    client = FakeClient()
    await make_provider(client).aclose()
    assert client.closed


# --- create() ----------------------------------------------------------------


def config(**fields: Any) -> ProviderConfig:
    base = {"type": "openai", "base_url": "http://127.0.0.1:9/v1", "api_key": "k-given"}
    return ProviderConfig.model_validate(base | fields)


def test_create_passes_key_and_base_url_explicitly() -> None:
    provider = create(config(), Secret("k-given"))
    assert isinstance(provider, OpenAICompatProvider)
    client = provider._client
    assert client.api_key == "k-given"
    assert str(client.base_url) == "http://127.0.0.1:9/v1/"
    assert client.max_retries == 0
    assert provider._secrets == ["k-given"]
    assert provider._reasoning_style == "openai"


def test_create_uses_the_configured_reasoning_style() -> None:
    provider = create(config(reasoning_style="openrouter"), Secret("k"))
    assert isinstance(provider, OpenAICompatProvider)
    assert provider._reasoning_style == "openrouter"


def test_create_without_a_key_is_a_config_error() -> None:
    with pytest.raises(ConfigError, match="openai provider requires an API key"):
        create(config(), None)


def test_ambient_environment_does_not_reach_the_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canaries = {
        "OPENAI_ORG_ID": "org-CANARY",
        "OPENAI_PROJECT_ID": "proj-CANARY",
        "OPENAI_API_KEY": "sk-ENVCANARY",
        "OPENAI_BASE_URL": "https://env-canary.invalid/v1",
        "OPENAI_ADMIN_KEY": "adm-CANARY",
        "OPENAI_WEBHOOK_SECRET": "wh-CANARY",
        "OPENAI_CUSTOM_HEADERS": "X-Env-Canary: header-CANARY\nAuthorization: Bearer cust-CANARY",
    }
    for name, value in canaries.items():
        monkeypatch.setenv(name, value)

    provider = create(config(), Secret("k-given"))

    assert isinstance(provider, OpenAICompatProvider)
    client = provider._client
    assert client.organization is None
    assert client.project is None
    assert client.admin_api_key is None
    assert client.webhook_secret is None
    assert str(client.base_url) == "http://127.0.0.1:9/v1/"
    assert client.auth_headers == {"Authorization": "Bearer k-given"}
    assert not any("CANARY" in str(v) for v in client.default_headers.values())
    assert "X-Env-Canary" not in client.default_headers


def test_environment_is_restored_after_create(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_ORG_ID", "org-keep")
    create(config(), Secret("k"))
    assert os.environ["OPENAI_ORG_ID"] == "org-keep"


async def test_real_sdk_connection_refused_is_one_connection_error() -> None:
    port = closed_local_port()
    provider = create(config(base_url=f"http://127.0.0.1:{port}/v1"), Secret("k-given"))
    try:
        events = [e async for e in provider.stream_chat(request())]
    finally:
        await provider.aclose()
    assert [type(e) for e in events] == [ErrorEvent]
    assert events[0].kind == "connection"  # type: ignore[union-attr]


# --- embeddings --------------------------------------------------------------


class _Embeddings:
    def __init__(self, owner: "FakeEmbedClient") -> None:
        self._owner = owner

    async def create(self, **kwargs: Any) -> CreateEmbeddingResponse:
        self._owner.calls.append(kwargs)
        if self._owner.raises is not None:
            raise self._owner.raises
        return CreateEmbeddingResponse.model_validate(
            {
                "object": "list",
                "model": "m",
                "usage": {"prompt_tokens": 1, "total_tokens": 1},
                "data": [
                    {"object": "embedding", "index": index, "embedding": vector}
                    for index, vector in self._owner.items
                ],
            }
        )


class FakeEmbedClient:
    """Stands in for openai.AsyncOpenAI.embeddings; records the exact kwargs.

    `items` are (index, vector) pairs returned in the given order.
    """

    def __init__(
        self,
        items: list[tuple[int, list[float]]] | None = None,
        *,
        raises: Exception | None = None,
    ) -> None:
        self.items = items if items is not None else [(0, [0.5, 1.5])]
        self.raises = raises
        self.calls: list[dict[str, Any]] = []
        self.embeddings = _Embeddings(self)


async def embed_with(
    client: FakeEmbedClient,
    texts: list[str],
    dimensions: int | None = None,
    secrets: list[str] | None = None,
) -> list[list[float]]:
    provider = make_provider(client, secrets=secrets)  # type: ignore[arg-type]
    return await provider.embed("text-embed", texts, dimensions)


async def test_embed_without_dimensions_sends_no_dimensions_argument() -> None:
    client = FakeEmbedClient([(0, [1.0]), (1, [2.0])])
    vectors = await embed_with(client, ["a", "b"])
    assert vectors == [[1.0], [2.0]]
    assert client.calls == [
        {"model": "text-embed", "input": ["a", "b"], "encoding_format": "float"}
    ]
    assert "dimensions" not in client.calls[0]


async def test_embed_with_dimensions_sends_them() -> None:
    client = FakeEmbedClient([(0, [1.0])])
    await embed_with(client, ["a"], dimensions=256)
    assert client.calls[0]["dimensions"] == 256


async def test_embed_returns_vectors_in_input_order() -> None:
    client = FakeEmbedClient([(2, [3.0]), (0, [1.0]), (1, [2.0])])
    vectors = await embed_with(client, ["a", "b", "c"])
    assert vectors == [[1.0], [2.0], [3.0]]


@pytest.mark.parametrize(
    ("cls", "status", "kind"),
    [
        (openai.AuthenticationError, 401, "auth"),
        (openai.PermissionDeniedError, 403, "auth"),
        (openai.NotFoundError, 404, "not_found"),
        (openai.RateLimitError, 429, "rate_limit"),
        (openai.BadRequestError, 400, "bad_request"),
        (openai.APIStatusError, 500, "unknown"),
    ],
)
async def test_embed_status_errors_map_to_kind(
    cls: type[openai.APIStatusError], status: int, kind: str
) -> None:
    client = FakeEmbedClient(raises=status_error(cls, status, "boom"))
    with pytest.raises(EmbeddingError) as info:
        await embed_with(client, ["a"])
    assert info.value.kind == kind
    assert str(info.value) == "boom"


@pytest.mark.parametrize(
    "exc",
    [
        openai.APIConnectionError(request=httpx2.Request("POST", "http://x")),
        openai.APITimeoutError(httpx2.Request("POST", "http://x")),
    ],
)
async def test_embed_connection_and_timeout_errors_are_connection(
    exc: Exception,
) -> None:
    with pytest.raises(EmbeddingError) as info:
        await embed_with(FakeEmbedClient(raises=exc), ["a"])
    assert info.value.kind == "connection"


async def test_embed_other_sdk_errors_are_unknown() -> None:
    with pytest.raises(EmbeddingError) as info:
        await embed_with(FakeEmbedClient(raises=openai.OpenAIError("odd")), ["a"])
    assert info.value.kind == "unknown"


async def test_embed_error_message_is_redacted() -> None:
    key = "my-plain-key-value"
    shape = "sk-abcdefghij1234567890"
    text = f"bad key {key} and {shape} Bearer tok123456"
    client = FakeEmbedClient(raises=status_error(openai.AuthenticationError, 401, text))
    with pytest.raises(EmbeddingError) as info:
        await embed_with(client, ["a"], secrets=[key])
    message = str(info.value)
    assert key not in message
    assert shape not in message
    assert "tok123456" not in message
    assert "[REDACTED:api_key]" in message


async def test_embed_vector_count_mismatch_is_unknown() -> None:
    client = FakeEmbedClient([(0, [1.0])])
    with pytest.raises(EmbeddingError) as info:
        await embed_with(client, ["a", "b"])
    assert info.value.kind == "unknown"
    assert "1" in str(info.value) and "2" in str(info.value)


async def test_embed_programming_errors_are_not_wrapped() -> None:
    client = FakeEmbedClient(raises=TypeError("our bug"))
    with pytest.raises(TypeError, match="our bug"):
        await embed_with(client, ["a"])
