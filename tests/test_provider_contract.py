"""One contract test over every adapter; later steps only add entries."""

from collections.abc import Callable

import aiohttp
import httpx
import httpx2
import ollama
import openai
import pytest
from google.auth import exceptions as auth_exceptions

from llm_harness.providers.base import ChatProvider
from llm_harness.providers.ollama import OllamaProvider
from llm_harness.providers.openai_compat import OpenAICompatProvider
from llm_harness.providers.vertex import VertexProvider
from llm_harness.types import ChatRequest, ErrorEvent, Message, StreamEvent
from tests import test_ollama_adapter as ollama_fakes
from tests import test_openai_compat_adapter as openai_fakes
from tests import test_vertex_adapter as vertex_fakes
from tests.fakes import assert_stream_contract

# (happy factory, error factory): each builds a provider whose stream either
# completes normally or fails after emitting a delta.
ProviderFactory = Callable[[], ChatProvider]


def ollama_happy() -> ChatProvider:
    stream = ollama_fakes.FakeStream(
        [
            ollama_fakes.chunk(thinking="t"),
            ollama_fakes.chunk("hi"),
            ollama_fakes.chunk(done=True, prompt=1),
        ]
    )
    return OllamaProvider(ollama_fakes.FakeClient(stream))  # type: ignore[arg-type]


def ollama_mid_stream_error() -> ChatProvider:
    stream = ollama_fakes.FakeStream(
        [ollama_fakes.chunk("hi")], raises=httpx.ReadError("cut")
    )
    return OllamaProvider(ollama_fakes.FakeClient(stream))  # type: ignore[arg-type]


def ollama_server_error() -> ChatProvider:
    client = ollama_fakes.FakeClient(chat_raises=ollama.ResponseError("nope", 500))
    return OllamaProvider(client)  # type: ignore[arg-type]


def openai_happy() -> ChatProvider:
    f = openai_fakes
    stream = f.FakeStream(
        [f.chunk(reasoning="t"), f.chunk("hi"), f.chunk(finish="stop"), f.usage_chunk()]
    )
    return OpenAICompatProvider(f.FakeClient(stream), "openai", [])  # type: ignore[arg-type]


def openai_mid_stream_error() -> ChatProvider:
    f = openai_fakes
    error = openai.APIConnectionError(request=httpx2.Request("POST", "http://x"))
    stream = f.FakeStream([f.chunk("hi")], raises=error)
    return OpenAICompatProvider(f.FakeClient(stream), "openai", [])  # type: ignore[arg-type]


def openai_server_error() -> ChatProvider:
    f = openai_fakes
    error = f.status_error(openai.APIStatusError, 500, "nope")
    return OpenAICompatProvider(f.FakeClient(create_raises=error), "openai", [])  # type: ignore[arg-type]


def vertex_happy() -> ChatProvider:
    f = vertex_fakes
    stream = f.FakeStream(
        [f.chunk(f.part("t", True)), f.chunk(f.part("hi")), f.last_chunk()]
    )
    return VertexProvider(f.FakeClient(stream))  # type: ignore[arg-type]


def vertex_mid_stream_error() -> ChatProvider:
    f = vertex_fakes
    stream = f.FakeStream([f.chunk(f.part("hi"))], raises=httpx.ReadError("cut"))
    return VertexProvider(f.FakeClient(stream))  # type: ignore[arg-type]


def vertex_aiohttp_mid_stream_error() -> ChatProvider:
    f = vertex_fakes
    stream = f.FakeStream(
        [f.chunk(f.part("hi"))], raises=aiohttp.ClientPayloadError("cut")
    )
    return VertexProvider(f.FakeClient(stream))  # type: ignore[arg-type]


def vertex_credentials_error() -> ChatProvider:
    f = vertex_fakes
    error = auth_exceptions.DefaultCredentialsError("no ADC")
    return VertexProvider(f.FakeClient(start_raises=error))  # type: ignore[arg-type]


ADAPTERS: list[tuple[str, ProviderFactory, list[ProviderFactory]]] = [
    ("ollama", ollama_happy, [ollama_mid_stream_error, ollama_server_error]),
    ("openai", openai_happy, [openai_mid_stream_error, openai_server_error]),
    (
        "vertex",
        vertex_happy,
        [
            vertex_mid_stream_error,
            vertex_aiohttp_mid_stream_error,
            vertex_credentials_error,
        ],
    ),
]

REQUEST = ChatRequest(model="m", messages=[Message(role="user", content="hi")])


async def run(provider: ChatProvider) -> list[StreamEvent]:
    return [event async for event in provider.stream_chat(REQUEST)]


@pytest.mark.parametrize(("name", "happy", "failing"), ADAPTERS, ids=lambda v: str(v))
async def test_adapter_satisfies_the_stream_contract(
    name: str, happy: ProviderFactory, failing: list[ProviderFactory]
) -> None:
    happy_events = await run(happy())
    assert_stream_contract(happy_events)
    assert not isinstance(happy_events[-1], ErrorEvent), name
    for make in failing:
        error_events = await run(make())
        assert_stream_contract(error_events)
        assert isinstance(error_events[-1], ErrorEvent), name
