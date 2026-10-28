"""The vector store: an async interface and the value types its backends share.

Framework-free. A collection is a named set of fixed-dimension vectors with a
payload; `search` returns the nearest ones, optionally restricted by payload
equality. Point ids are any string, so callers never see an engine's id rules.
Backends live in their own modules (`qdrant_edge`), and error text names the
collection, never a data path.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

DISTANCES = ("cosine", "dot", "euclid")


class VectorStoreError(Exception):
    """A vector step failed; `str(exc)` says what to change and never shows a data path."""


class VectorStoreLockedError(VectorStoreError):
    """Another process holds the collection's files open."""

    def __init__(self) -> None:
        super().__init__("vector store is in use by another llm-harness process")


@dataclass(frozen=True)
class VectorPoint:
    id: str
    vector: Sequence[float]
    payload: Mapping[str, str | int | float | bool]


@dataclass(frozen=True)
class VectorHit:
    id: str
    score: float  # higher is nearer, whatever the distance
    payload: dict


class VectorStore(Protocol):
    async def ensure_collection(
        self, name: str, dim: int, distance: str = "cosine"
    ) -> None:
        """Create the collection, or check an existing one has this dimension and distance."""
        ...

    async def upsert(self, collection: str, points: Sequence[VectorPoint]) -> None:
        """Insert or replace by id: a repeated id keeps one point, the latest."""
        ...

    async def search(
        self,
        collection: str,
        vector: Sequence[float],
        k: int,
        filter: Mapping[str, str | int | bool] | None = None,
    ) -> list[VectorHit]:
        """Up to `k` hits, highest score first; every `filter` key must equal its value."""
        ...

    async def delete(self, collection: str, ids: Sequence[str]) -> None: ...

    async def close(self) -> None: ...
