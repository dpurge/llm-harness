"""Vertex adapter tests: fake client, real google-genai response and error types."""

import asyncio
import os
import socket
from typing import Any

import aiohttp
import google.oauth2.credentials
import httpx
import pytest
from google import genai
from google.auth import exceptions as auth_exceptions
from google.genai import errors, types

from llm_harness.config import ProviderConfig
from llm_harness.embeddings import EmbeddingError
from llm_harness.providers import vertex
from llm_harness.providers.environment import hidden_environment
from llm_harness.providers.vertex import VertexProvider, create
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


def part(text: str, thought: bool | None = None) -> dict[str, Any]:
    return {"text": text, "thought": thought}


def chunk(
    *parts: dict[str, Any],
    finish: str | None = None,
    usage: dict[str, int] | None = None,
) -> types.GenerateContentResponse:
    data: dict[str, Any] = {
        "candidates": [
            {
                "content": {"role": "model", "parts": list(parts)},
                "finish_reason": finish,
            }
        ]
    }
    if usage is not None:
        data["usage_metadata"] = usage
    return types.GenerateContentResponse.model_validate(data)


def last_chunk(
    prompt: int | None = 7,
    candidates: int | None = 3,
    thoughts: int | None = None,
    finish: str = "STOP",
) -> types.GenerateContentResponse:
    usage = {
        "prompt_token_count": prompt,
        "candidates_token_count": candidates,
        "thoughts_token_count": thoughts,
    }
    return chunk(
        part(""),
        finish=finish,
        usage={k: v for k, v in usage.items() if v is not None},
    )


class FakeStream:
    """Async iterator over chunks; may raise or hang; records `aclose`."""

    def __init__(
        self,
        items: list[types.GenerateContentResponse],
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

    async def __anext__(self) -> types.GenerateContentResponse:
        for item in self._items:
            return item
        if self._raises is not None:
            raise self._raises
        if self._hang:
            await asyncio.Event().wait()
        raise StopAsyncIteration

    async def aclose(self) -> None:
        self.closed = True


class _Models:
    def __init__(self, owner: "FakeClient") -> None:
        self._owner = owner

    async def generate_content_stream(self, **kwargs: Any) -> FakeStream:
        self._owner.calls.append(kwargs)
        if self._owner.start_raises is not None:
            raise self._owner.start_raises
        assert self._owner.stream is not None
        return self._owner.stream


class _Aio:
    def __init__(self, owner: "FakeClient") -> None:
        self.models = _Models(owner)
        self._owner = owner

    async def aclose(self) -> None:
        self._owner.async_closed = True


class FakeClient:
    def __init__(
        self,
        stream: FakeStream | None = None,
        *,
        start_raises: Exception | None = None,
    ) -> None:
        self.stream = stream
        self.start_raises = start_raises
        self.calls: list[dict[str, Any]] = []
        self.async_closed = False
        self.sync_closed = False
        self.aio = _Aio(self)

    def close(self) -> None:
        self.sync_closed = True


def request(think: bool | None = None, *, system: bool = False) -> ChatRequest:
    messages = [
        Message(role="user", content="one"),
        Message(role="assistant", content="two"),
        Message(role="user", content="three"),
    ]
    if system:
        messages = [
            Message(role="system", content="be brief"),
            *messages,
            Message(role="system", content="in English"),
        ]
    return ChatRequest(model="gemini-x", messages=messages, think=think)


def make_provider(client: FakeClient) -> VertexProvider:
    return VertexProvider(client)  # type: ignore[arg-type]


async def collect(
    client: FakeClient, think: bool | None = None, *, system: bool = False
) -> list[StreamEvent]:
    provider = make_provider(client)
    return [e async for e in provider.stream_chat(request(think, system=system))]


def finished(*chunks: types.GenerateContentResponse) -> FakeClient:
    return FakeClient(FakeStream([*chunks, last_chunk()]))


def api_error(cls: type[errors.APIError], code: int, text: str = "boom"):
    return cls(code, {"error": {"message": text, "status": "X"}})


# --- request mapping ---------------------------------------------------------


async def test_request_maps_roles_order_and_whole_history() -> None:
    client = finished()
    await collect(client)
    assert len(client.calls) == 1
    call = client.calls[0]
    assert set(call) == {"model", "contents", "config"}
    assert call["model"] == "gemini-x"
    assert call["contents"] == [
        types.Content(role="user", parts=[types.Part(text="one")]),
        types.Content(role="model", parts=[types.Part(text="two")]),
        types.Content(role="user", parts=[types.Part(text="three")]),
    ]
    assert call["config"] == types.GenerateContentConfig()


async def test_response_schema_is_ignored() -> None:
    client = finished()
    req = request().model_copy(update={"response_schema": {"type": "object"}})
    [event async for event in make_provider(client).stream_chat(req)]
    plain = finished()
    await collect(plain)
    assert client.calls == plain.calls


async def test_system_messages_become_one_system_instruction() -> None:
    client = finished()
    await collect(client, system=True)
    call = client.calls[0]
    assert call["config"].system_instruction == "be brief\nin English"
    assert [c.role for c in call["contents"]] == ["user", "model", "user"]


@pytest.mark.parametrize(
    ("think", "expected"),
    [
        (None, None),
        (True, types.ThinkingConfig(include_thoughts=True, thinking_budget=-1)),
        (False, types.ThinkingConfig(include_thoughts=False, thinking_budget=0)),
    ],
)
async def test_thinking_toggle_maps_to_thinking_config(
    think: bool | None, expected: types.ThinkingConfig | None
) -> None:
    client = finished()
    await collect(client, think)
    config = client.calls[0]["config"]
    assert config.thinking_config == expected
    # nothing but the thinking config, and never a token cap
    assert config.max_output_tokens is None
    assert config.model_dump(exclude_none=True, exclude={"thinking_config"}) == {}


# --- stream mapping ----------------------------------------------------------


async def test_text_parts_become_text_deltas() -> None:
    events = await collect(finished(chunk(part("Hel")), chunk(part("lo"))))
    assert events[:2] == [TextDelta(text="Hel"), TextDelta(text="lo")]


async def test_thought_parts_become_thinking_deltas() -> None:
    events = await collect(
        finished(
            chunk(part("hm", True)), chunk(part("answer")), chunk(part("z", False))
        )
    )
    assert events[:3] == [
        ThinkingDelta(text="hm"),
        TextDelta(text="answer"),
        TextDelta(text="z"),
    ]


async def test_mixed_parts_in_one_chunk_keep_their_order() -> None:
    events = await collect(
        finished(chunk(part("t1", True), part("x"), part("t2", True), part("y")))
    )
    assert events[:4] == [
        ThinkingDelta(text="t1"),
        TextDelta(text="x"),
        ThinkingDelta(text="t2"),
        TextDelta(text="y"),
    ]


async def test_empty_text_parts_produce_nothing() -> None:
    events = await collect(finished(chunk(part("")), chunk(part("", True))))
    assert [type(e) for e in events] == [UsageEvent, Done]


async def test_chunks_without_candidates_content_or_parts_are_skipped() -> None:
    no_candidates = types.GenerateContentResponse()
    empty_candidates = types.GenerateContentResponse.model_validate({"candidates": []})
    no_content = types.GenerateContentResponse.model_validate({"candidates": [{}]})
    no_parts = types.GenerateContentResponse.model_validate(
        {"candidates": [{"content": {"role": "model"}}]}
    )
    assert (
        no_content.candidates is not None and no_content.candidates[0].content is None
    )
    assert no_parts.candidates is not None
    assert no_parts.candidates[0].content is not None
    assert no_parts.candidates[0].content.parts is None
    events = await collect(
        finished(
            no_candidates, empty_candidates, no_content, no_parts, chunk(part("ok"))
        )
    )
    assert events[0] == TextDelta(text="ok")
    assert [type(e) for e in events] == [TextDelta, UsageEvent, Done]


async def test_event_order_thinking_text_usage_done() -> None:
    stream = FakeStream(
        [
            chunk(part("hm", True)),
            chunk(part("Hi")),
            last_chunk(prompt=5, candidates=2, thoughts=9),
        ]
    )
    events = await collect(FakeClient(stream))
    assert events == [
        ThinkingDelta(text="hm"),
        TextDelta(text="Hi"),
        UsageEvent(usage=Usage(input_tokens=5, output_tokens=11, reasoning_tokens=9)),
        Done(finish_reason="STOP"),
    ]
    assert_stream_contract(events)
    assert stream.closed


@pytest.mark.parametrize(
    ("prompt", "candidates", "thoughts", "expected"),
    [
        (7, 3, None, Usage(input_tokens=7, output_tokens=3)),
        (7, 3, 0, Usage(input_tokens=7, output_tokens=3, reasoning_tokens=0)),
        (7, 3, 10, Usage(input_tokens=7, output_tokens=13, reasoning_tokens=10)),
        (7, None, 10, Usage(input_tokens=7, output_tokens=10, reasoning_tokens=10)),
        (7, None, None, Usage(input_tokens=7)),
        (None, 3, None, Usage(output_tokens=3)),
    ],
)
async def test_usage_mapping(
    prompt: int | None,
    candidates: int | None,
    thoughts: int | None,
    expected: Usage,
) -> None:
    stream = FakeStream([chunk(part("a")), last_chunk(prompt, candidates, thoughts)])
    events = await collect(FakeClient(stream))
    assert events[1] == UsageEvent(usage=expected)


async def test_usage_event_is_omitted_when_no_chunk_has_metadata() -> None:
    stream = FakeStream([chunk(part("a"), finish="STOP")])
    events = await collect(FakeClient(stream))
    assert events == [TextDelta(text="a"), Done(finish_reason="STOP")]


async def test_last_usage_metadata_wins() -> None:
    stream = FakeStream(
        [
            chunk(
                part("a"), usage={"prompt_token_count": 1, "candidates_token_count": 1}
            ),
            last_chunk(prompt=7, candidates=3),
        ]
    )
    events = await collect(FakeClient(stream))
    assert events[1] == UsageEvent(usage=Usage(input_tokens=7, output_tokens=3))


@pytest.mark.parametrize("reason", ["STOP", "MAX_TOKENS", "SAFETY"])
async def test_finish_reason_name_passes_through(reason: str) -> None:
    stream = FakeStream([chunk(part("a")), last_chunk(finish=reason)])
    events = await collect(FakeClient(stream))
    assert events[-1] == Done(finish_reason=reason)


async def test_stream_without_finish_reason_is_an_error_and_no_done() -> None:
    events = await collect(FakeClient(FakeStream([chunk(part("a"))])))
    assert events[-1] == ErrorEvent(
        kind="unknown", message="the stream ended without a finish reason"
    )
    assert not any(isinstance(e, Done) for e in events)
    assert_stream_contract(events)


# --- errors ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cls", "code", "kind"),
    [
        (errors.ClientError, 400, "bad_request"),
        (errors.ClientError, 401, "auth"),
        (errors.ClientError, 403, "auth"),
        (errors.ClientError, 404, "not_found"),
        (errors.ClientError, 429, "rate_limit"),
        (errors.ClientError, 418, "unknown"),
        (errors.ServerError, 500, "unknown"),
        (errors.ServerError, 503, "unknown"),
    ],
)
async def test_api_errors_map_to_kind(
    cls: type[errors.APIError], code: int, kind: str
) -> None:
    events = await collect(FakeClient(start_raises=api_error(cls, code, "boom")))
    assert len(events) == 1
    assert isinstance(events[0], ErrorEvent)
    assert events[0].kind == kind
    assert "boom" in events[0].message
    assert_stream_contract(events)


@pytest.mark.parametrize(
    "exc",
    [
        auth_exceptions.DefaultCredentialsError("no ADC"),
        auth_exceptions.RefreshError("expired"),
        auth_exceptions.GoogleAuthError("bad"),
    ],
)
async def test_credential_errors_are_auth(exc: Exception) -> None:
    events = await collect(FakeClient(start_raises=exc))
    assert events == [ErrorEvent(kind="auth", message=str(exc))]


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectError("refused"),
        httpx.ReadTimeout("slow"),
        ConnectionResetError("reset"),
        auth_exceptions.TransportError("token endpoint unreachable"),
    ],
)
async def test_connection_failures_are_connection(exc: Exception) -> None:
    events = await collect(FakeClient(start_raises=exc))
    assert len(events) == 1
    assert isinstance(events[0], ErrorEvent)
    assert events[0].kind == "connection"


async def test_error_mid_stream_ends_with_one_error_and_no_done() -> None:
    stream = FakeStream(
        [chunk(part("part")), chunk(part("t", True))],
        raises=api_error(errors.ServerError, 500, "died"),
    )
    events = await collect(FakeClient(stream))
    assert events[:2] == [TextDelta(text="part"), ThinkingDelta(text="t")]
    assert isinstance(events[-1], ErrorEvent)
    assert not any(isinstance(e, (Done, UsageEvent)) for e in events)
    assert sum(isinstance(e, ErrorEvent) for e in events) == 1
    assert_stream_contract(events)
    assert stream.closed


async def test_programming_errors_are_not_swallowed() -> None:
    stream = FakeStream([chunk(part("a"))], raises=TypeError("our bug"))
    with pytest.raises(TypeError, match="our bug"):
        await collect(FakeClient(stream))


async def test_error_message_is_redacted_and_truncated() -> None:
    shape = "sk-abcdefghij1234567890"
    text = f"bad key {shape} " + "x" * 500
    events = await collect(
        FakeClient(start_raises=api_error(errors.ClientError, 401, text))
    )
    assert isinstance(events[0], ErrorEvent)
    assert shape not in events[0].message
    assert "[REDACTED:api_key]" in events[0].message
    assert len(events[0].message) <= 301


# The real aiohttp types, not stand-ins: the SDK raises these since aiohttp is
# installed, and a test with httpx errors passed while this path was broken.
AIOHTTP_CONNECTION_ERRORS = [
    aiohttp.ClientConnectorError(
        aiohttp.client_reqrep.ConnectionKey("h", 1, False, True, None, None, None),
        OSError("refused"),
    ),
    aiohttp.ClientOSError("os"),
    aiohttp.ServerDisconnectedError("gone"),
    aiohttp.ServerTimeoutError("slow"),
    aiohttp.ClientPayloadError("cut"),
    TimeoutError(),
]


@pytest.mark.parametrize(
    "exc", AIOHTTP_CONNECTION_ERRORS, ids=lambda e: type(e).__name__
)
async def test_aiohttp_start_failures_are_connection(exc: Exception) -> None:
    events = await collect(FakeClient(start_raises=exc))
    assert [type(e) for e in events] == [ErrorEvent]
    assert events[0].kind == "connection"  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "exc", AIOHTTP_CONNECTION_ERRORS, ids=lambda e: type(e).__name__
)
async def test_aiohttp_mid_stream_failures_are_one_connection_error(
    exc: Exception,
) -> None:
    stream = FakeStream([chunk(part("partial"))], raises=exc)
    events = await collect(FakeClient(stream))
    assert events[0] == TextDelta(text="partial")
    assert isinstance(events[-1], ErrorEvent)
    assert events[-1].kind == "connection"
    assert sum(isinstance(e, ErrorEvent) for e in events) == 1
    assert not any(isinstance(e, (Done, UsageEvent)) for e in events)
    assert_stream_contract(events)
    assert stream.closed


async def test_other_aiohttp_errors_are_unknown() -> None:
    events = await collect(FakeClient(start_raises=aiohttp.InvalidURL("bad")))
    assert [type(e) for e in events] == [ErrorEvent]
    assert events[0].kind == "unknown"  # type: ignore[union-attr]


async def test_cancelled_error_still_propagates() -> None:
    with pytest.raises(asyncio.CancelledError):
        await collect(FakeClient(start_raises=asyncio.CancelledError()))


def closed_local_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def test_real_sdk_connection_refused_is_one_connection_error() -> None:
    # attempts=1 is the SDK's public switch that skips its implicit 1-10 s
    # sleep before re-raising aiohttp errors; production code is unchanged.
    client = genai.Client(
        vertexai=True,
        project="p",
        location="l",
        credentials=google.oauth2.credentials.Credentials(token="fake-token"),
        http_options=types.HttpOptions(
            base_url=f"http://127.0.0.1:{closed_local_port()}",
            retry_options=types.HttpRetryOptions(attempts=1),
        ),
    )
    assert client._api_client._use_aiohttp()
    provider = VertexProvider(client)
    try:
        events = [e async for e in provider.stream_chat(request())]
    finally:
        await provider.aclose()
    assert [type(e) for e in events] == [ErrorEvent]
    assert events[0].kind == "connection"  # type: ignore[union-attr]


# --- cancellation and close --------------------------------------------------


async def test_cancel_closes_the_sdk_stream_and_propagates() -> None:
    stream = FakeStream([chunk(part("a"))], hang=True)
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
    stream = FakeStream([chunk(part("a")), chunk(part("b")), last_chunk()])
    events = make_provider(FakeClient(stream)).stream_chat(request())
    assert await anext(events) == TextDelta(text="a")
    await events.aclose()  # type: ignore[attr-defined]
    assert stream.closed


async def test_aclose_closes_both_clients() -> None:
    client = FakeClient()
    await make_provider(client).aclose()
    assert client.async_closed
    assert client.sync_closed


# --- create() ----------------------------------------------------------------

AMBIENT_CANARIES = {
    "GOOGLE_API_KEY": "sk-ENVCANARY-123456",
    "GEMINI_API_KEY": "sk-ENVCANARY-654321",
    "GOOGLE_CLOUD_PROJECT": "canary-project",
    "GOOGLE_CLOUD_LOCATION": "canary-location",
    "GOOGLE_GENAI_USE_VERTEXAI": "false",
    "GOOGLE_GENAI_USE_ENTERPRISE": "false",
    "GOOGLE_VERTEX_BASE_URL": "https://vertex-canary.invalid/",
    "GOOGLE_GEMINI_BASE_URL": "https://gemini-canary.invalid/",
    "GOOGLE_GENAI_CLIENT_MODE": "replay",
    "GOOGLE_GENAI_REPLAYS_DIRECTORY": "/canary/replays",
    "GOOGLE_GENAI_REPLAY_ID": "canary-replay",
}
# Legitimate ADC inputs: must stay visible to the SDK.
ADC_INPUTS = {
    "GOOGLE_APPLICATION_CREDENTIALS": "/adc/credentials.json",
    "CLOUDSDK_CONFIG": "/adc/gcloud",
}


def config(**fields: Any) -> ProviderConfig:
    base = {"type": "vertex", "project": "cfg-project", "location": "us-central1"}
    return ProviderConfig.model_validate(base | fields)


def test_create_passes_project_and_location_explicitly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in AMBIENT_CANARIES:
        monkeypatch.delenv(name, raising=False)
    provider = create(config(), None)
    assert isinstance(provider, VertexProvider)
    api_client = provider._client._api_client
    assert provider._client.vertexai is True
    assert api_client.project == "cfg-project"
    assert api_client.location == "us-central1"
    assert api_client.api_key is None
    assert api_client._http_options.base_url == (
        "https://us-central1-aiplatform.googleapis.com/"
    )


def test_hidden_variable_list_matches_the_canaries() -> None:
    assert set(vertex._HIDDEN_VARIABLES) == set(AMBIENT_CANARIES)
    assert not set(vertex._HIDDEN_VARIABLES) & set(ADC_INPUTS)


def test_ambient_environment_does_not_reach_the_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name, value in (AMBIENT_CANARIES | ADC_INPUTS).items():
        monkeypatch.setenv(name, value)

    provider = create(config(), None)

    assert isinstance(provider, VertexProvider)
    api_client = provider._client._api_client
    assert api_client.project == "cfg-project"
    assert api_client.location == "us-central1"
    assert api_client.api_key is None
    assert provider._client.vertexai is True
    base_url = api_client._http_options.base_url
    assert base_url == "https://us-central1-aiplatform.googleapis.com/"
    assert "canary" not in str(base_url)


def test_every_hidden_variable_is_absent_while_the_client_is_built(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name, value in (AMBIENT_CANARIES | ADC_INPUTS).items():
        monkeypatch.setenv(name, value)
    seen: dict[str, str] = {}

    def recording_client(**kwargs: Any) -> object:
        seen.update(os.environ)
        seen["kwargs"] = repr(sorted(kwargs.items()))
        return object()

    monkeypatch.setattr(vertex.genai, "Client", recording_client)
    create(config(), None)

    assert not [name for name in AMBIENT_CANARIES if name in seen]
    assert {name: seen[name] for name in ADC_INPUTS} == ADC_INPUTS
    assert seen["kwargs"] == repr(
        [
            ("location", "us-central1"),
            ("project", "cfg-project"),
            ("vertexai", True),
        ]
    )


def test_environment_is_restored_after_create(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name, value in (AMBIENT_CANARIES | ADC_INPUTS).items():
        monkeypatch.setenv(name, value)
    create(config(), None)
    assert {n: os.environ[n] for n in AMBIENT_CANARIES | ADC_INPUTS} == (
        AMBIENT_CANARIES | ADC_INPUTS
    )


def test_environment_is_restored_when_the_constructor_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "keep-me")

    def failing_client(**kwargs: Any) -> object:
        raise ValueError("constructor failed")

    monkeypatch.setattr(vertex.genai, "Client", failing_client)
    with pytest.raises(ValueError, match="constructor failed"):
        create(config(), None)
    assert os.environ["GOOGLE_CLOUD_PROJECT"] == "keep-me"


async def test_real_client_can_be_closed_twice() -> None:
    provider = create(config(), None)
    await provider.aclose()
    await provider.aclose()


# --- hidden_environment ------------------------------------------------------


def test_hidden_environment_hides_by_prefix_and_name_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HIDE_ME_ONE", "1")
    monkeypatch.setenv("EXACT_NAME", "2")
    monkeypatch.setenv("KEEP_ME", "3")
    monkeypatch.setenv("EXACT_NAME_LONGER", "4")
    with hidden_environment(prefixes=("HIDE_",), names=("EXACT_NAME",)):
        assert "HIDE_ME_ONE" not in os.environ
        assert "EXACT_NAME" not in os.environ
        assert os.environ["KEEP_ME"] == "3"
        assert os.environ["EXACT_NAME_LONGER"] == "4"
    assert os.environ["HIDE_ME_ONE"] == "1"
    assert os.environ["EXACT_NAME"] == "2"


def test_hidden_environment_without_arguments_hides_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOME_VARIABLE", "x")
    with hidden_environment():
        assert os.environ["SOME_VARIABLE"] == "x"


def test_hidden_environment_restores_on_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HIDE_ME", "1")
    with pytest.raises(RuntimeError), hidden_environment(prefixes=("HIDE_",)):
        raise RuntimeError("boom")
    assert os.environ["HIDE_ME"] == "1"


# --- embeddings --------------------------------------------------------------


class _EmbedModels:
    def __init__(self, owner: "FakeEmbedClient") -> None:
        self._owner = owner

    async def embed_content(self, **kwargs: Any) -> types.EmbedContentResponse:
        self._owner.calls.append(kwargs)
        if self._owner.raises is not None:
            raise self._owner.raises
        return types.EmbedContentResponse.model_validate(
            {"embeddings": [{"values": v} for v in self._owner.vectors]}
        )


class _EmbedAio:
    def __init__(self, owner: "FakeEmbedClient") -> None:
        self.models = _EmbedModels(owner)


class FakeEmbedClient:
    """Stands in for genai.Client.aio.models.embed_content; records kwargs."""

    def __init__(
        self,
        vectors: list[list[float]] | None = None,
        *,
        raises: Exception | None = None,
    ) -> None:
        self.vectors = vectors if vectors is not None else [[0.5, 1.5]]
        self.raises = raises
        self.calls: list[dict[str, Any]] = []
        self.aio = _EmbedAio(self)


async def embed_with(
    client: FakeEmbedClient, texts: list[str], dimensions: int | None = None
) -> list[list[float]]:
    return await make_provider(client).embed("embed-x", texts, dimensions)  # type: ignore[arg-type]


async def test_embed_without_dimensions_sends_no_config() -> None:
    client = FakeEmbedClient([[1.0], [2.0]])
    vectors = await embed_with(client, ["a", "b"])
    assert vectors == [[1.0], [2.0]]
    assert client.calls == [{"model": "embed-x", "contents": ["a", "b"]}]


async def test_embed_with_dimensions_sends_output_dimensionality() -> None:
    client = FakeEmbedClient([[1.0]])
    await embed_with(client, ["a"], dimensions=256)
    config = client.calls[0]["config"]
    assert isinstance(config, types.EmbedContentConfig)
    assert config.output_dimensionality == 256


@pytest.mark.parametrize(
    ("cls", "code", "kind"),
    [
        (errors.ClientError, 400, "bad_request"),
        (errors.ClientError, 401, "auth"),
        (errors.ClientError, 403, "auth"),
        (errors.ClientError, 404, "not_found"),
        (errors.ClientError, 429, "rate_limit"),
        (errors.ClientError, 418, "unknown"),
        (errors.ServerError, 500, "unknown"),
    ],
)
async def test_embed_api_errors_map_to_kind(
    cls: type[errors.APIError], code: int, kind: str
) -> None:
    client = FakeEmbedClient(raises=api_error(cls, code, "boom"))
    with pytest.raises(EmbeddingError) as info:
        await embed_with(client, ["a"])
    assert info.value.kind == kind
    assert "boom" in str(info.value)


@pytest.mark.parametrize(
    "exc",
    [
        auth_exceptions.DefaultCredentialsError("no ADC"),
        auth_exceptions.RefreshError("expired"),
        auth_exceptions.GoogleAuthError("bad"),
    ],
)
async def test_embed_credential_errors_are_auth(exc: Exception) -> None:
    with pytest.raises(EmbeddingError) as info:
        await embed_with(FakeEmbedClient(raises=exc), ["a"])
    assert info.value.kind == "auth"


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectError("refused"),
        httpx.ReadTimeout("slow"),
        ConnectionResetError("reset"),
        auth_exceptions.TransportError("token endpoint unreachable"),
        *AIOHTTP_CONNECTION_ERRORS,
    ],
)
async def test_embed_connection_failures_are_connection(exc: Exception) -> None:
    with pytest.raises(EmbeddingError) as info:
        await embed_with(FakeEmbedClient(raises=exc), ["a"])
    assert info.value.kind == "connection"


async def test_embed_other_aiohttp_errors_are_unknown() -> None:
    client = FakeEmbedClient(raises=aiohttp.InvalidURL("bad"))
    with pytest.raises(EmbeddingError) as info:
        await embed_with(client, ["a"])
    assert info.value.kind == "unknown"


async def test_embed_error_message_is_redacted() -> None:
    shape = "sk-abcdefghij1234567890"
    text = f"bad key {shape} Bearer tok123456 at http://u:hunter2@host/x"
    client = FakeEmbedClient(raises=api_error(errors.ClientError, 401, text))
    with pytest.raises(EmbeddingError) as info:
        await embed_with(client, ["a"])
    message = str(info.value)
    assert shape not in message
    assert "tok123456" not in message
    assert "hunter2" not in message
    assert "[REDACTED:api_key]" in message


async def test_embed_vector_count_mismatch_is_unknown() -> None:
    client = FakeEmbedClient([[1.0]])
    with pytest.raises(EmbeddingError) as info:
        await embed_with(client, ["a", "b"])
    assert info.value.kind == "unknown"
    assert "1" in str(info.value) and "2" in str(info.value)


async def test_embed_programming_errors_are_not_wrapped() -> None:
    client = FakeEmbedClient(raises=TypeError("our bug"))
    with pytest.raises(TypeError, match="our bug"):
        await embed_with(client, ["a"])
