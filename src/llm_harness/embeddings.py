"""Embedder: turns texts into vectors with one configured embedding model."""

from llm_harness.config import ModelConfig
from llm_harness.providers.base import EmbeddingProvider
from llm_harness.types import ErrorKind


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
        vectors = await self._provider.embed(
            self._model.model, texts, self._model.dimensions
        )
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
