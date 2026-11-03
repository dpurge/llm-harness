"""Vector search for memory: rank fusion and the index-writing store.

`fuse_ranks` is pure, no I/O: keyword hits and vector hits become one list by
reciprocal rank fusion: each list gives an id `1 / (60 + rank)`,
rank counting from 1, and the scores of an id found in both lists add up. Ranks
are used instead of raw scores because a keyword score and a cosine score are not
comparable. Ties go to the keyword order, so the best keyword hit is never pushed
down by a vector hit that scores the same.

`IndexedMemoryStore` keeps a vector index in step with the memories; see its docstring.
"""

import hashlib
import sqlite3
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime
from functools import partial
from typing import TypeVar

from llm_harness.embeddings import Embedder
from llm_harness.memory import (
    _COLUMNS,
    MAX_SEARCH_RESULTS,
    Memory,
    MemoryStore,
    _check_user_id,
    _row_to_memory,
)
from llm_harness.redact import redact
from llm_harness.storage.relational import RelationalStore, database_error_classes
from llm_harness.storage.vectors import VectorPoint, VectorStore, VectorStoreError

RRF_OFFSET = 60
MAX_QUERY_EMBED_CHARS = 1000  # a long message is cut before it is embedded
VECTOR_SEARCH_LIMIT = 10
# The first search of a process repairs at most this many unindexed memories: it
# bounds the latency that one turn pays for the repair.
LAZY_REPAIR_LIMIT = 20
REINDEX_BATCH_SIZE = 16

_T = TypeVar("_T")


def _first_positions(ids: Sequence[int]) -> dict[int, int]:
    """Rank (from 1) of each id at its first appearance; repeats are ignored."""
    positions: dict[int, int] = {}
    for position, memory_id in enumerate(ids, start=1):
        positions.setdefault(memory_id, position)
    return positions


def fuse_ranks(
    keyword_ids: Sequence[int], vector_ids: Sequence[int], k: int
) -> list[int]:
    """Up to `k` unique ids, best first, by reciprocal rank fusion of both lists."""
    if k < 1:
        return []
    keyword_rank = _first_positions(keyword_ids)
    vector_rank = _first_positions(vector_ids)
    absent = len(keyword_rank) + len(vector_rank) + 1  # after every real rank

    def score(memory_id: int) -> float:
        return sum(
            1 / (RRF_OFFSET + ranks[memory_id])
            for ranks in (keyword_rank, vector_rank)
            if memory_id in ranks
        )

    candidates = keyword_rank.keys() | vector_rank.keys()
    ordered = sorted(
        candidates,
        key=lambda memory_id: (
            -score(memory_id),
            keyword_rank.get(memory_id, absent),
            vector_rank.get(memory_id, absent),
        ),
    )
    return ordered[:k]


_COLLECTION_PREFIX = "memories_"


def collection_name(model_id: str) -> str:
    """A collection per embedding model, so vectors of two models never mix.

    Hashed because a model id may hold characters the adapter's name rule refuses.
    """
    return _COLLECTION_PREFIX + hashlib.sha256(model_id.encode()).hexdigest()[:16]


def _embedded_text(memory: Memory) -> str:
    return f"{memory.text} [{', '.join(memory.keywords)}]"


def _describe(error: Exception) -> str:
    """What the notice says about a failed index step.

    A PostgreSQL driver message names the host and user or quotes the failing row, and
    `redact` only knows key and url shapes, so a driver error is shown by type alone
    (as `store.py` does). A SQLite message keeps its text, as before.
    """
    if isinstance(error, database_error_classes()) and not isinstance(
        error, sqlite3.Error
    ):
        return type(error).__name__
    return redact(f"{type(error).__name__}: {error}")


class IndexedMemoryStore(MemoryStore):
    """A MemoryStore that mirrors saves, updates and forgets into a vector store.

    SQLite stays the truth: every write goes there first and its result is returned
    whatever happens to the index. The first index failure switches the index off for
    the rest of the process and is reported once through `take_notice`.
    """

    def __init__(
        self,
        relational: RelationalStore,
        vectors: VectorStore,
        embedder_factory: Callable[[], Embedder],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if clock is None:
            super().__init__(relational)
        else:
            super().__init__(relational, clock)
        self._vectors = vectors
        self._embedder_factory = embedder_factory
        self._embedder: Embedder | None = None
        self._collection: str | None = None
        self._off_reason: str | None = None
        self._notice_pending = False
        self._repaired_users: set[str] = set()

    @property
    def index_off_reason(self) -> str | None:
        return self._off_reason

    def resume(self) -> None:
        """Switch the index back on and forget what the process learned about it.

        Only `/memory reindex` calls this: searches and writes stay off until the
        user asks, so there is no hidden retry. Synchronous without an `await`, so
        the reset is atomic on the event loop. `_repaired_users` is kept: the reindex
        that follows covers the whole user. SQLite is not touched.
        """
        self._off_reason = None
        self._notice_pending = False
        self._collection = None
        self._embedder = None

    def take_notice(self) -> str | None:
        """The one-line notice about the index being off, once; then None."""
        if not self._notice_pending:
            return None
        self._notice_pending = False
        return f"memory vector search is off: {self._off_reason}; keyword search only"

    async def save(self, user_id: str, text: str, keywords: Sequence[str]) -> Memory:
        memory = await super().save(user_id, text, keywords)
        await self._index(memory)
        return memory

    async def update(
        self,
        user_id: str,
        id: int,
        text: str | None = None,
        keywords: Sequence[str] | None = None,
    ) -> Memory | None:
        memory = await super().update(user_id, id, text, keywords)
        if memory is not None:
            await self._index(memory)
        return memory

    async def forget(self, user_id: str, id: int) -> bool:
        deleted = await super().forget(user_id, id)
        if deleted:
            await self._guarded(partial(self._remove, id))
        return deleted

    async def search(self, user_id: str, query: str, k: int = 5) -> list[Memory]:
        """Keyword hits and vector hits fused by rank; keyword only when the index is off."""
        keyword = await super().search(user_id, query, MAX_SEARCH_RESULTS)
        limit = max(1, min(MAX_SEARCH_RESULTS, k))
        if not query.strip():
            return keyword[:limit]
        vector_ids = await self._guarded(partial(self._vector_ids, user_id, query))
        if vector_ids is None:
            return keyword[:limit]
        result = await self._fused(user_id, keyword, vector_ids, limit)
        await self._repair_once(
            user_id
        )  # after the result: it never delays or alters it
        return result

    async def _fused(
        self,
        user_id: str,
        keyword: list[Memory],
        vector_ids: list[int],
        limit: int,
    ) -> list[Memory]:
        found = {memory.id: memory for memory in keyword}
        for memory_id in vector_ids:
            if memory_id not in found:
                memory = await self.get(
                    user_id, memory_id
                )  # drops orphans, other users
                if memory is not None:
                    found[memory_id] = memory
        fused = fuse_ranks([m.id for m in keyword], vector_ids, limit)
        return [found[memory_id] for memory_id in fused if memory_id in found]

    async def _repair_once(self, user_id: str) -> None:
        if user_id in self._repaired_users:
            return
        self._repaired_users.add(user_id)
        await self.reindex(
            user_id, limit=LAZY_REPAIR_LIMIT
        )  # failures switch the index off

    async def reindex(
        self, user_id: str, limit: int | None = None
    ) -> tuple[int, int] | None:
        """Index the user's memories that have no marker in the current collection.

        Returns `(indexed_now, unindexed_before)`, or None when the index is off (also
        when a failure inside has just switched it off; markers already written stay).
        """
        user_id = _check_user_id(user_id)
        return await self._guarded(partial(self._reindex, user_id, limit))

    async def _reindex(self, user_id: str, limit: int | None) -> tuple[int, int]:
        await self._ensure_migrated()
        collection = collection_name(self._get_embedder().model_id)
        unindexed = (
            "FROM memories WHERE user_id = ? AND NOT EXISTS ("
            "SELECT 1 FROM memory_vectors v "
            "WHERE v.collection = ? AND v.memory_id = memories.id)"
        )
        params = (user_id, collection)
        rows = await self._relational.query(f"SELECT COUNT(*) {unindexed}", params)
        total = rows[0][0]
        if total == 0:
            return (0, 0)
        take = total if limit is None else max(0, limit)
        rows = await self._relational.query(
            f"SELECT {_COLUMNS} {unindexed} ORDER BY id LIMIT ?", (*params, take)
        )
        memories = [_row_to_memory(row) for row in rows]
        for start in range(0, len(memories), REINDEX_BATCH_SIZE):
            await self._index_batch(memories[start : start + REINDEX_BATCH_SIZE])
        return (len(memories), total)

    async def _index_batch(self, batch: list[Memory]) -> None:
        collection, vectors = await self._embed_batch_and_ensure(
            [_embedded_text(memory) for memory in batch]
        )
        await self._vectors.upsert(
            collection,
            [
                VectorPoint(str(memory.id), vector, {"user_id": memory.user_id})
                for memory, vector in zip(batch, vectors, strict=True)
            ],
        )
        for memory in batch:
            await self._relational.execute(
                "INSERT INTO memory_vectors (collection, memory_id) VALUES (?, ?) "
                "ON CONFLICT (collection, memory_id) DO NOTHING",
                (collection, memory.id),
            )

    async def _vector_ids(self, user_id: str, query: str) -> list[int]:
        collection, vector = await self._embed_and_ensure(query[:MAX_QUERY_EMBED_CHARS])
        hits = await self._vectors.search(
            collection, vector, VECTOR_SEARCH_LIMIT, filter={"user_id": user_id}
        )
        return [int(hit.id) for hit in hits]

    async def _index(self, memory: Memory) -> None:
        await self._guarded(partial(self._add, memory))

    async def _guarded(self, step: Callable[[], Awaitable[_T]]) -> _T | None:
        """Run an index step; any failure turns the index off instead of escaping."""
        if self._off_reason is not None:
            return None
        try:
            return await step()
        except Exception as error:  # noqa: BLE001  (CancelledError is not an Exception)
            self._off_reason = _describe(error)
            self._notice_pending = True
            return None

    async def _embed_and_ensure(self, text: str) -> tuple[str, list[float]]:
        """Embed first (the dimension comes from the vector), then the collection."""
        collection, (vector,) = await self._embed_batch_and_ensure([text])
        return collection, vector

    async def _embed_batch_and_ensure(
        self, texts: list[str]
    ) -> tuple[str, list[list[float]]]:
        embedder = self._get_embedder()
        vectors = await embedder.embed(texts)
        collection = await self._ensure_collection(embedder.model_id, len(vectors[0]))
        return collection, vectors

    async def _add(self, memory: Memory) -> None:
        collection, vector = await self._embed_and_ensure(_embedded_text(memory))
        await self._vectors.upsert(
            collection,
            [VectorPoint(str(memory.id), vector, {"user_id": memory.user_id})],
        )
        await self._relational.execute(
            "INSERT INTO memory_vectors (collection, memory_id) VALUES (?, ?) "
            "ON CONFLICT (collection, memory_id) DO NOTHING",
            (collection, memory.id),
        )

    def _get_embedder(self) -> Embedder:
        if self._embedder is None:
            self._embedder = self._embedder_factory()
        return self._embedder

    async def _remove(self, memory_id: int) -> None:
        if self._collection is None:
            # A first call in this process: the dimension is only known from the meta row.
            model_id = self._get_embedder().model_id
            rows = await self._relational.query(
                "SELECT dim FROM memory_vector_meta WHERE collection = ?",
                (collection_name(model_id),),
            )
            if not rows:  # nothing was ever indexed for this model
                return
            await self._ensure_collection(model_id, rows[0][0])
        await self._vectors.delete(self._collection, [str(memory_id)])

    async def _ensure_collection(self, model_id: str, dim: int) -> str:
        if self._collection is None:
            name = collection_name(model_id)
            await self._check_model(name, model_id, dim)
            await self._vectors.ensure_collection(name, dim)
            self._collection = name
        return self._collection

    async def _check_model(self, collection: str, model_id: str, dim: int) -> None:
        rows = await self._relational.query(
            "SELECT model_id, dim FROM memory_vector_meta WHERE collection = ?",
            (collection,),
        )
        if not rows:
            await self._relational.execute(
                "INSERT INTO memory_vector_meta (collection, model_id, dim) "
                "VALUES (?, ?, ?)",
                (collection, model_id, dim),
            )
        elif tuple(rows[0]) != (model_id, dim):
            raise VectorStoreError(
                "embedding model or dimension changed for this collection"
            )
