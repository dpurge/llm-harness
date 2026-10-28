"""Live Ollama check; deselected by default, run with `pytest -m live`."""

import asyncio
import math
from typing import Any

import pytest

from llm_harness.config import (
    CONFIG_FILE,
    MODELS_FILE,
    ConfigError,
    default_config_dir,
    load_config,
    select_embedding_model,
    select_model,
)
from llm_harness.embeddings import Embedder, EmbeddingError
from llm_harness.providers import build_provider
from llm_harness.providers.ollama import OllamaProvider, create
from llm_harness.types import Done, TextDelta, UsageEvent
from tests.fakes import assert_stream_contract, single_turn

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
    try:
        async with asyncio.timeout(90):
            events = await single_turn(adapter, model.model, "Say ready", think=True)
    finally:
        await adapter.aclose()

    assert_stream_contract(events)
    assert isinstance(events[-1], Done)
    assert any(isinstance(e, TextDelta) for e in events)
    usage = [e.usage for e in events if isinstance(e, UsageEvent)]
    assert len(usage) == 1
    assert isinstance(usage[0].input_tokens, int)
    assert isinstance(usage[0].output_tokens, int)


async def test_live_ollama_embeds_two_texts() -> None:
    config_dir = default_config_dir()
    if (
        not (config_dir / CONFIG_FILE).exists()
        or not (config_dir / MODELS_FILE).exists()
    ):
        pytest.skip(f"no {CONFIG_FILE} / {MODELS_FILE} in {config_dir}")
    try:
        config = load_config(config_dir)
        model = select_embedding_model(config, None)
    except ConfigError as exc:
        pytest.skip(f"no usable embedding model: {exc}")
    provider_config = config.providers[model.provider]
    if provider_config.type != "ollama":
        pytest.skip(f"embedding model {model.id} is not on an ollama provider")

    provider = build_provider(provider_config)
    assert isinstance(provider, OllamaProvider)
    embedder = Embedder(provider, model)
    try:
        async with asyncio.timeout(90):
            vectors = await embedder.embed(["The sky is blue.", "Cats purr softly."])
    except EmbeddingError as exc:
        if exc.kind in ("connection", "not_found"):
            pytest.skip(f"ollama embedding unavailable ({exc.kind}): {exc.message}")
        raise
    finally:
        await provider.aclose()

    assert len(vectors) == 2
    # The expected length is the entry's `dimensions` when set, else the model's
    # own size (bge-m3: 1024), which the embedder learns from the first vector.
    expected = model.dimensions if model.dimensions is not None else 1024
    assert [len(v) for v in vectors] == [expected, expected]
    assert embedder.dimension == expected
    assert math.isclose(math.sqrt(sum(x * x for x in vectors[0])), 1.0, abs_tol=0.01)
    assert vectors[0] != vectors[1]
