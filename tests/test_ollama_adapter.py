"""Ollama adapter tests with a fake SDK client built from real SDK types."""

import asyncio
import os
from typing import Any

import httpx
import ollama
import pytest

from llm_harness.config import ProviderConfig
from llm_harness.providers.ollama import OllamaProvider, create
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
    content: str = "",
    thinking: str | None = None,
    *,
    done: bool = False,
    prompt: int | None = None,
    evaluated: int | None = None,
    reason: str | None = None,
) -> ollama.ChatResponse:
    return ollama.ChatResponse(
        model="m",
        done=done,
        done_reason=reason,
        prompt_eval_count=prompt,
        eval_count=evaluated,
        message=ollama.Message(role="assistant", content=content, thinking=thinking),
    )


class FakeStream:
    """Async iterator over chunks; may raise or hang; records `aclose`."""

    def __init__(
        self,
        items: list[ollama.ChatResponse],
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

    async def __anext__(self) -> ollama.ChatResponse:
        for item in self._items:
            return item
        if self._raises is not None:
            raise self._raises
        if self._hang:
            await asyncio.Event().wait()
        raise StopAsyncIteration

    async def aclose(self) -> None:
        self.closed = True


class FakeClient:
    def __init__(
        self, stream: FakeStream | None = None, *, chat_raises: Exception | None = None
    ) -> None:
        self.stream = stream
        self.chat_raises = chat_raises
        self.calls: list[dict[str, Any]] = []
        self.closed = False

    async def chat(self, **kwargs: Any) -> FakeStream:
        self.calls.append(kwargs)
        if self.chat_raises is not None:
            raise self.chat_raises
        assert self.stream is not None
        return self.stream

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


async def collect(client: FakeClient, think: bool | None = None) -> list[StreamEvent]:
    provider = OllamaProvider(client)  # type: ignore[arg-type]
    return [event async for event in provider.stream_chat(request(think))]


async def test_request_is_mapped_and_streamed() -> None:
    client = FakeClient(FakeStream([chunk(done=True)]))
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
            "think": None,
        }
    ]


@pytest.mark.parametrize("think", [True, False, None])
async def test_think_is_forwarded_as_given(think: bool | None) -> None:
    client = FakeClient(FakeStream([chunk(done=True)]))
    await collect(client, think)
    assert client.calls[0]["think"] is think


async def test_event_order_thinking_text_usage_done() -> None:
    stream = FakeStream(
        [
            chunk(thinking="hm"),
            chunk(thinking="m"),
            chunk("Hel"),
            chunk("lo"),
            chunk(done=True, prompt=7, evaluated=3, reason="stop"),
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


async def test_no_thinking_delta_without_thinking() -> None:
    events = await collect(FakeClient(FakeStream([chunk("a"), chunk(done=True)])))
    assert not any(isinstance(e, ThinkingDelta) for e in events)


async def test_empty_strings_produce_no_deltas() -> None:
    stream = FakeStream([chunk("", ""), chunk("x", None), chunk(done=True)])
    events = await collect(FakeClient(stream))
    assert events == [TextDelta(text="x"), Done(finish_reason=None)]


@pytest.mark.parametrize(
    ("prompt", "evaluated", "expected"),
    [
        (5, 9, Usage(input_tokens=5, output_tokens=9)),
        (5, None, Usage(input_tokens=5, output_tokens=None)),
        (None, 9, Usage(input_tokens=None, output_tokens=9)),
        (0, 0, Usage(input_tokens=0, output_tokens=0)),
    ],
)
async def test_usage_mapping(
    prompt: int | None, evaluated: int | None, expected: Usage
) -> None:
    stream = FakeStream([chunk(done=True, prompt=prompt, evaluated=evaluated)])
    events = await collect(FakeClient(stream))
    assert events[0] == UsageEvent(usage=expected)
    assert expected.reasoning_tokens is None


async def test_no_usage_event_when_both_counts_are_missing() -> None:
    events = await collect(FakeClient(FakeStream([chunk(done=True)])))
    assert events == [Done(finish_reason=None)]


async def test_finish_reason_passes_through() -> None:
    events = await collect(FakeClient(FakeStream([chunk(done=True, reason="length")])))
    assert events[-1] == Done(finish_reason="length")


async def test_stream_stops_after_the_done_chunk() -> None:
    stream = FakeStream([chunk(done=True), chunk("late")])
    events = await collect(FakeClient(stream))
    assert events == [Done(finish_reason=None)]
    assert stream.closed


@pytest.mark.parametrize(
    ("status", "kind"),
    [
        (400, "bad_request"),
        (401, "auth"),
        (403, "auth"),
        (404, "not_found"),
        (429, "rate_limit"),
        (500, "unknown"),
    ],
)
async def test_response_error_status_maps_to_kind(status: int, kind: str) -> None:
    client = FakeClient(chat_raises=ollama.ResponseError("boom", status))
    events = await collect(client)
    assert events == [ErrorEvent(kind=kind, message=f"boom (status code: {status})")]


async def test_request_error_is_bad_request() -> None:
    events = await collect(FakeClient(chat_raises=ollama.RequestError("bad input")))
    assert events == [ErrorEvent(kind="bad_request", message="bad input")]


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectError("All connection attempts failed"),
        httpx.ReadTimeout("slow"),
        ConnectionError("refused"),
    ],
)
async def test_unreachable_server_is_a_connection_error(exc: Exception) -> None:
    events = await collect(FakeClient(chat_raises=exc))
    assert len(events) == 1
    assert isinstance(events[0], ErrorEvent)
    assert events[0].kind == "connection"
    assert_stream_contract(events)


async def test_error_mid_stream_ends_with_one_error_and_no_done() -> None:
    stream = FakeStream(
        [chunk("part"), chunk(thinking="t")], raises=ollama.ResponseError("died", 500)
    )
    events = await collect(FakeClient(stream))
    assert events[:2] == [TextDelta(text="part"), ThinkingDelta(text="t")]
    assert isinstance(events[-1], ErrorEvent)
    assert not any(isinstance(e, Done) for e in events)
    assert_stream_contract(events)
    assert stream.closed


async def test_stream_ending_without_done_is_an_error() -> None:
    events = await collect(FakeClient(FakeStream([chunk("a")])))
    assert isinstance(events[-1], ErrorEvent)
    assert events[-1].kind == "unknown"
    assert_stream_contract(events)


async def test_programming_errors_are_not_swallowed() -> None:
    stream = FakeStream([chunk("a")], raises=TypeError("our bug"))
    with pytest.raises(TypeError, match="our bug"):
        await collect(FakeClient(stream))


async def test_error_message_is_redacted_and_truncated() -> None:
    secret = "sk-abcdefghij1234567890"
    text = f"bad key {secret} with Bearer tok123456 " + "x" * 500
    events = await collect(FakeClient(chat_raises=ollama.ResponseError(text, 401)))
    assert isinstance(events[0], ErrorEvent)
    assert secret not in events[0].message
    assert "tok123456" not in events[0].message
    assert "[REDACTED:api_key]" in events[0].message
    assert len(events[0].message) <= 301


async def test_cancel_closes_the_sdk_stream_and_propagates() -> None:
    stream = FakeStream([chunk("a")], hang=True)
    provider = OllamaProvider(FakeClient(stream))  # type: ignore[arg-type]
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
    stream = FakeStream([chunk("a"), chunk("b"), chunk(done=True)])
    provider = OllamaProvider(FakeClient(stream))  # type: ignore[arg-type]
    events = provider.stream_chat(request())
    assert await anext(events) == TextDelta(text="a")
    await events.aclose()  # type: ignore[attr-defined]
    assert stream.closed


async def test_aclose_closes_the_client() -> None:
    client = FakeClient()
    await OllamaProvider(client).aclose()  # type: ignore[arg-type]
    assert client.closed


def test_create_builds_a_client_for_the_configured_host() -> None:
    config = ProviderConfig(type="ollama", base_url="http://127.0.0.1:9")
    provider = create(config, None)
    assert isinstance(provider, OllamaProvider)
    assert str(provider._client._client.base_url) == "http://127.0.0.1:9"  # type: ignore[attr-defined]


def test_ambient_environment_does_not_reach_the_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OLLAMA_API_KEY", "canary-key")
    monkeypatch.setenv("OLLAMA_HOST", "http://env-canary.invalid:1")
    provider = create(
        ProviderConfig(type="ollama", base_url="http://127.0.0.1:9"), None
    )
    client = provider._client._client  # type: ignore[attr-defined]
    assert str(client.base_url) == "http://127.0.0.1:9"
    assert "authorization" not in {k.lower() for k in client.headers}
    assert "canary" not in repr(dict(client.headers))


def test_environment_is_restored_after_create(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OLLAMA_API_KEY", "canary-key")
    monkeypatch.setenv("OLLAMA_HOST", "http://env-canary.invalid:1")
    create(ProviderConfig(type="ollama", base_url="http://127.0.0.1:9"), None)
    assert os.environ["OLLAMA_API_KEY"] == "canary-key"
    assert os.environ["OLLAMA_HOST"] == "http://env-canary.invalid:1"


async def test_real_sdk_connection_refused_is_one_connection_error() -> None:
    port = closed_local_port()
    provider = create(
        ProviderConfig(type="ollama", base_url=f"http://127.0.0.1:{port}"), None
    )
    try:
        events = [e async for e in provider.stream_chat(request())]
    finally:
        await provider.aclose()
    assert [type(e) for e in events] == [ErrorEvent]
    assert events[0].kind == "connection"  # type: ignore[union-attr]
