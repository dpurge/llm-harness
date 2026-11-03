"""Embedder: turns texts into vectors with one configured embedding model."""

import asyncio
from collections.abc import Callable
from typing import cast

from llm_harness.config import (
    ConfigError,
    HarnessConfig,
    ModelConfig,
    select_embedding_model,
)
from llm_harness.providers import ProviderFactory
from llm_harness.providers.base import EmbeddingProvider
from llm_harness.types import ErrorKind

# A server that accepts the connection and never answers must not block memory
# writes and searches. The first call includes the model load (about 2.5 s
# measured for bge-m3), so 60 s is generous; it matches REFLECTION_TIMEOUT_SECONDS.
EMBED_TIMEOUT_SECONDS = 60


class EmbeddingError(Exception):
    """An embedding call failed; `kind` is the neutral ErrorKind.

    The message must already be redacted by the raiser.
    """

    def __init__(self, kind: ErrorKind, message: str) -> None:
        super().__init__(message)
        self.kind: ErrorKind = kind
        self.message = message


class Embedder:
    """One provider and one embedding model; no fallback and no retry.

    `dimension` is the entry's `dimensions` when set; otherwise it is learned
    from the first returned vector; it is None until a call has returned one.
    """

    def __init__(self, provider: EmbeddingProvider, model: ModelConfig) -> None:
        self._provider = provider
        self._model = model
        self._dimension: int | None = model.dimensions

    @property
    def model_id(self) -> str:
        return self._model.id

    @property
    def dimension(self) -> int | None:
        return self._dimension

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        try:
            async with asyncio.timeout(EMBED_TIMEOUT_SECONDS):
                vectors = await self._provider.embed(
                    self._model.model, texts, self._model.dimensions
                )
        except TimeoutError as exc:
            raise EmbeddingError(
                "connection",
                f"embedding model {self._model.id} did not answer within "
                f"{EMBED_TIMEOUT_SECONDS} seconds",
            ) from exc
        for vector in vectors:
            if self._dimension is None:
                self._dimension = len(vector)
            elif len(vector) != self._dimension:
                raise EmbeddingError(
                    "unknown",
                    f"model {self._model.id!r} returned a vector of length "
                    f"{len(vector)} but the dimension is {self._dimension}",
                )
        return vectors


def build_embedder_factory(
    config: HarnessConfig, provider_factory: ProviderFactory
) -> Callable[[], Embedder]:
    """A factory that builds the `Embedder` on first call, never earlier.

    It raises `ConfigError` (no `embedding` model, or a provider without `embed`);
    the memory index turns that into its one-time "vector search is off" notice.
    The provider is not closed at exit (a known limit: no shared close hook yet).
    """

    def factory() -> Embedder:
        model = select_embedding_model(config, None)
        provider = provider_factory(config.providers[model.provider])
        if not hasattr(provider, "embed"):
            raise ConfigError(
                f"provider {model.provider!r} cannot embed; "
                f"embedding model {model.id!r} needs a provider that offers embeddings"
            )
        # The factory is typed for chat; the hasattr check above proves `embed`.
        return Embedder(cast(EmbeddingProvider, provider), model)

    return factory
