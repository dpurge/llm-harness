"""Live OpenAI-compatible check (local Ollama `/v1` by default); deselected by default.

Run with `pytest -m live`. Safe by default: only a model whose provider is
`type = "openai"` on localhost is used, unless LLM_HARNESS_LIVE_MODEL (a model
id from models.yaml; test-only variable) names another one explicitly.
"""

import asyncio
import os
from typing import Any
from urllib.parse import urlparse

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
from llm_harness.providers.openai_compat import OpenAICompatProvider, create
from llm_harness.types import Done, TextDelta, UsageEvent
from tests.fakes import assert_stream_contract, single_turn

pytestmark = pytest.mark.live

MODEL_VARIABLE = "LLM_HARNESS_LIVE_MODEL"
LOCAL_HOSTS = {"localhost", "127.0.0.1"}


class CappedOutput:
    """Wraps the SDK client to cap output length; keeps the adapter test-free."""

    def __init__(self, client: Any) -> None:
        self._client = client
        self.chat = self
        self.completions = self

    async def create(self, **kwargs: Any) -> Any:
        return await self._client.chat.completions.create(
            **kwargs, max_completion_tokens=32
        )

    async def close(self) -> None:
        await self._client.close()


def _pick_model(config: HarnessConfig) -> ModelConfig:
    wanted = os.environ.get(MODEL_VARIABLE)
    if wanted:
        for model in config.models:
            if model.id == wanted:
                return model
        pytest.skip(f"{MODEL_VARIABLE}={wanted!r} is not a model id in models.yaml")
    for model in config.models:
        provider = config.providers[model.provider]
        host = urlparse(provider.base_url or "").hostname
        if provider.type == "openai" and host in LOCAL_HOSTS:
            return model
    pytest.skip(
        f"no openai-type model on localhost; set {MODEL_VARIABLE} to a model id "
        "to use another one"
    )


async def test_live_openai_compat_streams_text_and_usage() -> None:
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
    if provider_config.type != "openai":
        pytest.skip(f"model {model.id} is not on an openai-type provider")

    adapter = create(provider_config, resolve_api_key(provider_config))
    assert isinstance(adapter, OpenAICompatProvider)
    adapter._client = CappedOutput(adapter._client)  # type: ignore[assignment]
    try:
        async with asyncio.timeout(90):
            events = await single_turn(adapter, model.model, "Say ready", think=None)
    finally:
        await adapter.aclose()

    assert_stream_contract(events)
    assert isinstance(events[-1], Done)
    assert any(isinstance(e, TextDelta) for e in events)
    usage = [e.usage for e in events if isinstance(e, UsageEvent)]
    assert len(usage) == 1
    assert isinstance(usage[0].input_tokens, int)
    assert isinstance(usage[0].output_tokens, int)
