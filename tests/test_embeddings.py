"""Embedder behaviour, tested against the fake embedding provider."""

import pytest

from llm_harness.config import ModelConfig
from llm_harness.embeddings import Embedder, EmbeddingError
from tests.fakes import FakeEmbedder


def model(dimensions: int | None = None) -> ModelConfig:
    return ModelConfig(
        id="bge-m3-local",
        name="bge-m3",
        provider="local",
        model="bge-m3",
        tags=["embedding"],
        priority=1,
        dimensions=dimensions,
    )


async def test_vectors_come_back_in_input_order() -> None:
    fake = FakeEmbedder()
    vectors = await Embedder(fake, model()).embed(["a", "bbb", "cc"])
    assert [v[0] for v in vectors] == [1.0, 3.0, 2.0]
    assert fake.calls == [("bge-m3", ["a", "bbb", "cc"], None)]


async def test_empty_list_makes_no_provider_call() -> None:
    fake = FakeEmbedder()
    assert await Embedder(fake, model()).embed([]) == []
    assert fake.calls == []


async def test_model_id_is_the_entry_id() -> None:
    assert Embedder(FakeEmbedder(), model()).model_id == "bge-m3-local"


async def test_dimension_is_none_before_any_call_then_learned() -> None:
    embedder = Embedder(FakeEmbedder(size=7), model())
    assert embedder.dimension is None
    await embedder.embed(["x"])
    assert embedder.dimension == 7


async def test_configured_dimension_is_known_and_passed_on() -> None:
    fake = FakeEmbedder()
    embedder = Embedder(fake, model(dimensions=256))
    assert embedder.dimension == 256
    vectors = await embedder.embed(["x"])
    assert len(vectors[0]) == 256
    assert fake.calls[0][2] == 256


async def test_later_vector_of_another_length_raises() -> None:
    fake = FakeEmbedder(size=4)
    embedder = Embedder(fake, model())
    await embedder.embed(["x"])
    fake.size = 5
    with pytest.raises(EmbeddingError) as info:
        await embedder.embed(["y"])
    assert info.value.kind == "unknown"
    assert "5" in str(info.value) and "4" in str(info.value)


async def test_configured_dimension_mismatch_raises() -> None:
    class WrongSize(FakeEmbedder):
        async def embed(
            self, model: str, texts: list[str], dimensions: int | None
        ) -> list[list[float]]:
            return [[0.0] * 3 for _ in texts]

    with pytest.raises(EmbeddingError, match="3.*256"):
        await Embedder(WrongSize(), model(dimensions=256)).embed(["x"])


async def test_provider_error_propagates_with_its_kind_and_is_not_retried() -> None:
    fake = FakeEmbedder(raises=EmbeddingError("rate_limit", "slow down"))
    with pytest.raises(EmbeddingError) as info:
        await Embedder(fake, model()).embed(["x"])
    assert info.value.kind == "rate_limit"
    assert str(info.value) == "slow down"
    assert len(fake.calls) == 1
