"""Storage interfaces and their local backends, and the one object tools receive.

`open_storage` builds the three stores from `[storage]` and creates nothing: files
and directories appear on first use, and the vector shard is never opened here, so
a vector store another process holds cannot stop startup.
"""

from collections.abc import Mapping, Sequence
from pathlib import Path

from llm_harness.config import StorageConfig
from llm_harness.storage.files import FileStore, LocalFileStore
from llm_harness.storage.qdrant_edge import QdrantEdgeVectorStore
from llm_harness.storage.relational import RelationalStore, SqliteRelationalStore
from llm_harness.storage.vectors import (
    VectorHit,
    VectorPoint,
    VectorStore,
    VectorStoreError,
)

_VECTORS_DISABLED = (
    'the vector store is disabled: set [storage.vector] backend = "qdrant-edge" '
    "in config.toml"
)


class DisabledVectorStore:
    """The `none` backend: every use says how to turn vector search on."""

    async def ensure_collection(
        self, name: str, dim: int, distance: str = "cosine"
    ) -> None:
        raise VectorStoreError(_VECTORS_DISABLED)

    async def upsert(self, collection: str, points: Sequence[VectorPoint]) -> None:
        raise VectorStoreError(_VECTORS_DISABLED)

    async def search(
        self,
        collection: str,
        vector: Sequence[float],
        k: int,
        filter: Mapping[str, str | int | bool] | None = None,
    ) -> list[VectorHit]:
        raise VectorStoreError(_VECTORS_DISABLED)

    async def delete(self, collection: str, ids: Sequence[str]) -> None:
        raise VectorStoreError(_VECTORS_DISABLED)

    async def close(self) -> None:
        # Closing is part of every exit, so it must work when vectors are off.
        return None


class Storage:
    """The relational, file and vector stores of one process; closed once on exit."""

    def __init__(
        self,
        relational: RelationalStore,
        files: FileStore,
        vectors: VectorStore,
    ) -> None:
        self.relational = relational
        self.files = files
        self.vectors = vectors
        self._closed = False

    async def close(self) -> None:
        """Release what was opened; safe to call again, and never raises on exit."""
        if self._closed:
            return
        self._closed = True
        try:
            await self.vectors.close()
        except VectorStoreError:
            # A shard that did not close cleanly must not turn a normal exit into
            # a traceback; the engine has already been told to close every shard.
            pass


def open_storage(
    config: StorageConfig,
    data_dir: Path,
    *,
    relational_path: Path | None = None,
) -> Storage:
    """Build the stores `config` names under `data_dir`; nothing is created or opened."""
    vectors: VectorStore
    if config.vector.backend == "none":
        vectors = DisabledVectorStore()
    else:
        vectors = QdrantEdgeVectorStore(config.vector.resolved_path(data_dir))
    return Storage(
        relational=SqliteRelationalStore(
            relational_path or config.relational.resolved_path(data_dir)
        ),
        files=LocalFileStore(config.files.resolved_root(data_dir)),
        vectors=vectors,
    )
