"""Live Vertex AI check; deselected by default.

Run with `pytest -m live -k vertex`. Uses the model whose id is in
LLM_HARNESS_LIVE_VERTEX_MODEL (test-only variable), else the first model on a
`type = "vertex"` provider, from the real home config and the user's ADC.
"""

import asyncio
import os
from typing import Any

import pytest

from llm_harness.config import (
    CONFIG_FILE,
    MODELS_FILE,
    ConfigError,
    HarnessConfig,
    ModelConfig,
    default_config_dir,
    load_config,
    resolve_api_key,
)
from llm_harness.providers.vertex import VertexProvider, create
from llm_harness.session import ChatSession
from llm_harness.types import Done, StreamEvent, TextDelta, UsageEvent
from tests.fakes import assert_stream_contract

pytestmark = pytest.mark.live

MODEL_VARIABLE = "LLM_HARNESS_LIVE_VERTEX_MODEL"


class _CappedModels:
    def __init__(self, models: Any) -> None:
        self._models = models

    async def generate_content_stream(self, **kwargs: Any) -> Any:
        config = kwargs["config"].model_copy(update={"max_output_tokens": 32})
        return await self._models.generate_content_stream(
            **{**kwargs, "config": config}
        )


class CappedOutput:
    """Wraps the SDK client to cap output length; keeps the adapter test-free."""

    def __init__(self, client: Any) -> None:
        self._client = client
        self.aio = self
        self.models = _CappedModels(client.aio.models)

    async def aclose(self) -> None:
        await self._client.aio.aclose()

    def close(self) -> None:
        self._client.close()


def _pick_model(config: HarnessConfig) -> ModelConfig:
    wanted = os.environ.get(MODEL_VARIABLE)
    if wanted:
        for model in config.models:
            if model.id == wanted:
                return model
        pytest.skip(f"{MODEL_VARIABLE}={wanted!r} is not a model id in models.yaml")
    for model in config.models:
        if config.providers[model.provider].type == "vertex":
            return model
    pytest.skip("no model on a vertex-type provider in models.yaml")


async def test_live_vertex_streams_text_and_usage() -> None:
    config_dir = default_config_dir()
    if (
        not (config_dir / CONFIG_FILE).exists()
        or not (config_dir / MODELS_FILE).exists()
    ):
        pytest.skip(f"no {CONFIG_FILE} / {MODELS_FILE} in {config_dir}")
    try:
        config = load_config(config_dir)
    except ConfigError as exc:
        pytest.skip(f"unusable config: {exc}")
    model = _pick_model(config)
    provider_config = config.providers[model.provider]
    if provider_config.type != "vertex":
        pytest.skip(f"model {model.id} is not on a vertex-type provider")

    adapter = create(provider_config, resolve_api_key(provider_config))
    assert isinstance(adapter, VertexProvider)
    adapter._client = CappedOutput(adapter._client)  # type: ignore[assignment]
    session = ChatSession(adapter, model.model, think=None)
    events: list[StreamEvent] = []
    try:
        async with asyncio.timeout(30):
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
