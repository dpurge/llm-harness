"""Live Ollama check; deselected by default, run with `pytest -m live`."""

import asyncio
from typing import Any

import pytest

from llm_harness.config import (
    CONFIG_FILE,
    MODELS_FILE,
    ConfigError,
    default_config_dir,
    load_config,
    select_model,
)
from llm_harness.providers.ollama import OllamaProvider, create
from llm_harness.session import ChatSession
from llm_harness.types import Done, StreamEvent, TextDelta, UsageEvent
from tests.fakes import assert_stream_contract

pytestmark = pytest.mark.live


class CappedOutput:
    """Wraps the SDK client to cap output length; keeps the adapter test-free."""

    def __init__(self, client: Any) -> None:
        self._client = client

    async def chat(self, **kwargs: Any) -> Any:
        return await self._client.chat(**kwargs, options={"num_predict": 32})

    async def close(self) -> None:
        await self._client.close()


async def test_live_ollama_streams_text_and_usage() -> None:
    config_dir = default_config_dir()
    if (
        not (config_dir / CONFIG_FILE).exists()
        or not (config_dir / MODELS_FILE).exists()
    ):
        pytest.skip(f"no {CONFIG_FILE} / {MODELS_FILE} in {config_dir}")
    try:
        config = load_config(config_dir)
        model = select_model(config, None)
    except ConfigError as exc:
        pytest.skip(f"no usable default model: {exc}")
    provider_config = config.providers[model.provider]
    if provider_config.type != "ollama":
        pytest.skip(f"default model {model.id} is not on an ollama provider")

    adapter = create(provider_config, None)
    assert isinstance(adapter, OllamaProvider)
    adapter._client = CappedOutput(adapter._client)  # type: ignore[assignment]
    session = ChatSession(adapter, model.model, think=True)
    events: list[StreamEvent] = []
    try:
        async with asyncio.timeout(90):
            async for event in session.send("Say ready"):
                events.append(event)
    finally:
        await adapter.aclose()

    assert_stream_contract(events)
    assert isinstance(events[-1], Done)
    assert any(isinstance(e, TextDelta) for e in events)
    usage = [e.usage for e in events if isinstance(e, UsageEvent)]
    assert len(usage) == 1
    assert isinstance(usage[0].input_tokens, int)
    assert isinstance(usage[0].output_tokens, int)
