"""The vector store backend on qdrant-edge (an embedded, in-process engine).

Layout: `<root>/<name>/shard/` is the engine's directory and `<root>/<name>/meta.json`
records the collection's dimension and distance, because `EdgeShard.info()` reports
only counts (qdrant-edge-py 0.9.0). Construction creates nothing; `ensure_collection`
does, with directories 0700 and `meta.json` 0600.

The engine allows one process per shard directory: a second opener fails at once,
which is reported as `VectorStoreLockedError`. Shards stay open until `close()`.
Any other engine failure is reduced to the collection name and the exception type,
because the engine's own text repeats the absolute data path.
"""

import asyncio
import contextlib
import json
import os
import re
import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import qdrant_edge as q

from llm_harness.storage.files import make_private_dirs
from llm_harness.storage.vectors import (
    DISTANCES,
    VectorHit,
    VectorPoint,
    VectorStoreError,
    VectorStoreLockedError,
)

# Never change this: every stored point id is derived from it, so a new value
# would orphan every point already on disk.
_ID_NAMESPACE = uuid.UUID("7c0b6f3e-5d1a-4e8b-9a52-3f4d8e1c6b27")
_ORIGINAL_ID_KEY = "_id"
_META_FILENAME = "meta.json"
_SHARD_DIRNAME = "shard"
_NAME_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,63}")
_LOCK_MARKERS = ("WouldBlock", "Can't init WAL")

_ENGINE_DISTANCES = {
    "cosine": q.Distance.Cosine,
    "dot": q.Distance.Dot,
    "euclid": q.Distance.Euclid,
}


def _check_name(name: str) -> None:
    if not isinstance(name, str) or not _NAME_PATTERN.fullmatch(name):
        raise VectorStoreError(
            "a collection name is a lowercase letter then up to 63 of a-z, 0-9 and '_'"
        )


def _point_id(original: str) -> str:
    return str(uuid.uuid5(_ID_NAMESPACE, original))


def _write_private(path: Path, text: str) -> None:
    """Write `path` atomically with mode 0600 (the umask would mask a mode given to open)."""
    tmp_path = path.with_name(path.name + ".tmp")
    descriptor = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        os.fchmod(handle.fileno(), 0o600)
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, path)


def existing_collection_names(root: Path) -> list[str]:
    """Names of the collections already on disk under `root`; reads, never creates."""
    try:
        entries = list(root.iterdir())
    except FileNotFoundError:
        return []
    return sorted(
        entry.name
        for entry in entries
        if _NAME_PATTERN.fullmatch(entry.name) and (entry / _META_FILENAME).is_file()
    )


def read_collection_meta(root: Path, name: str) -> tuple[int, str]:
    """The stored (dimension, distance) of an existing collection, else `VectorStoreError`."""
    _check_name(name)
    return QdrantEdgeVectorStore._read_meta(name, root / name / _META_FILENAME)


class _Collection:
    def __init__(self, shard: Any, dim: int, distance: str) -> None:
        self.shard = shard
        self.dim = dim
        self.distance = distance


class QdrantEdgeVectorStore:
    """Collections under one directory. Construction creates nothing; `ensure_collection` does.

    After `close()` every call raises `VectorStoreError`: reopening silently would
    hide a caller that outlived its owner.
    """

    def __init__(self, root: Path) -> None:
        self._root = root
        self._collections: dict[str, _Collection] = {}
        self._closed = False
        # Thread-safety of EdgeShard is UNVERIFIED, so one lock serialises every
        # engine call of this adapter (it also guards `_collections`).
        self._lock = threading.Lock()

    async def ensure_collection(
        self, name: str, dim: int, distance: str = "cosine"
    ) -> None:
        _check_name(name)
        if isinstance(dim, bool) or not isinstance(dim, int) or dim < 1:
            raise VectorStoreError(f"the dimension must be a positive int, got {dim!r}")
        if distance not in DISTANCES:
            raise VectorStoreError(
                f"unknown distance {distance!r}: use one of {', '.join(DISTANCES)}"
            )
        await self._run(name, self._ensure, name, dim, distance)

    async def upsert(self, collection: str, points: Sequence[VectorPoint]) -> None:
        _check_name(collection)
        await self._run(collection, self._upsert, collection, points)

    async def search(
        self,
        collection: str,
        vector: Sequence[float],
        k: int,
        filter: Mapping[str, str | int | bool] | None = None,
    ) -> list[VectorHit]:
        _check_name(collection)
        if isinstance(k, bool) or not isinstance(k, int) or k < 1:
            raise VectorStoreError(f"k must be a positive int, got {k!r}")
        return await self._run(collection, self._search, collection, vector, k, filter)

    async def delete(self, collection: str, ids: Sequence[str]) -> None:
        _check_name(collection)
        await self._run(collection, self._delete, collection, ids)

    async def close(self) -> None:
        await asyncio.to_thread(self._close)

    async def _run(self, name: str, work: Callable[..., Any], *args: Any) -> Any:
        try:
            return await asyncio.to_thread(work, *args)
        except VectorStoreError:
            raise
        except Exception as exc:  # noqa: BLE001 - engine failures have no common class
            # Exception, not BaseException: cancellation and Ctrl-C pass through.
            raise VectorStoreError(
                f"vector store failed on collection {name!r}: {type(exc).__name__}"
            ) from None

    def _ensure(self, name: str, dim: int, distance: str) -> None:
        with self._lock:
            self._check_open()
            held = self._collections.get(name)
            if held is not None:
                self._check_matches(name, held.dim, held.distance, dim, distance)
                return
            directory = self._root / name
            shard_dir = directory / _SHARD_DIRNAME
            meta_path = directory / _META_FILENAME
            if meta_path.exists():
                stored_dim, stored_distance = self._read_meta(name, meta_path)
                self._check_matches(name, stored_dim, stored_distance, dim, distance)
                shard = self._open_shard(
                    shard_dir, create=False, dim=dim, distance=distance
                )
            else:
                make_private_dirs(shard_dir)
                shard = self._open_shard(
                    shard_dir, create=True, dim=dim, distance=distance
                )
                # After the shard, so a crash in between leaves a retryable
                # collection (no meta) and never a meta without a shard.
                _write_private(
                    meta_path, json.dumps({"dim": dim, "distance": distance})
                )
            self._collections[name] = _Collection(shard, dim, distance)

    def _open_shard(
        self, shard_dir: Path, *, create: bool, dim: int, distance: str
    ) -> Any:
        try:
            if create:
                config = q.EdgeConfig(
                    vectors=q.EdgeVectorParams(
                        size=dim, distance=_ENGINE_DISTANCES[distance]
                    )
                )
                return q.EdgeShard.create(str(shard_dir), config)
            return q.EdgeShard.load(str(shard_dir))
        except Exception as exc:
            # The engine raises a bare `Exception` here, so the message is all there is.
            if any(marker in str(exc) for marker in _LOCK_MARKERS):
                raise VectorStoreLockedError() from None
            raise

    @staticmethod
    def _read_meta(name: str, meta_path: Path) -> tuple[int, str]:
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            dim, distance = meta["dim"], meta["distance"]
            if not isinstance(dim, int) or distance not in DISTANCES:
                raise ValueError
        except (OSError, ValueError, KeyError, TypeError):
            raise VectorStoreError(
                f"the metadata of collection {name!r} is unreadable; "
                "delete the collection's directory and re-index it"
            ) from None
        return dim, distance

    @staticmethod
    def _check_matches(
        name: str, stored_dim: int, stored_distance: str, dim: int, distance: str
    ) -> None:
        if stored_dim != dim:
            raise VectorStoreError(
                f"collection {name!r} holds {stored_dim}-dimensional vectors, got {dim}; "
                "re-index the collection or use another name"
            )
        if stored_distance != distance:
            raise VectorStoreError(
                f"collection {name!r} uses the {stored_distance!r} distance, got {distance!r}; "
                "re-index the collection or use another name"
            )

    def _check_open(self) -> None:
        if self._closed:
            raise VectorStoreError("the vector store is closed")

    def _get(self, name: str) -> _Collection:
        self._check_open()
        collection = self._collections.get(name)
        if collection is None:
            raise VectorStoreError(
                f"unknown collection {name!r}: call ensure_collection first"
            )
        return collection

    @staticmethod
    def _check_vector(
        name: str, collection: _Collection, vector: Sequence[float]
    ) -> None:
        if len(vector) != collection.dim:
            raise VectorStoreError(
                f"collection {name!r} holds {collection.dim}-dimensional vectors, "
                f"got a vector of {len(vector)}"
            )

    @staticmethod
    def _checked_filter(
        filter: Mapping[str, str | int | bool] | None,
    ) -> Mapping[str, str | int | bool]:
        # Verified on qdrant-edge-py 0.9.0: `MatchValue(True)` matches the integer 1
        # and never a stored boolean, so a bool filter would return wrong results silently.
        for key, value in (filter or {}).items():
            if isinstance(value, bool):
                raise VectorStoreError(
                    f"cannot filter on {key!r} with a bool: store 0 or 1, or a string, instead"
                )
        return filter or {}

    def _upsert(self, name: str, points: Sequence[VectorPoint]) -> None:
        with self._lock:
            collection = self._get(name)
            engine_points = []
            for point in points:
                self._check_vector(name, collection, point.vector)
                if _ORIGINAL_ID_KEY in point.payload:
                    raise VectorStoreError(
                        f"the payload key {_ORIGINAL_ID_KEY!r} is reserved for the point id"
                    )
                payload = {**point.payload, _ORIGINAL_ID_KEY: point.id}
                engine_points.append(
                    q.Point(_point_id(point.id), list(point.vector), payload)
                )
            if not engine_points:
                return
            collection.shard.update(q.UpdateOperation.upsert_points(engine_points))
            collection.shard.flush()

    def _search(
        self,
        name: str,
        vector: Sequence[float],
        k: int,
        filter: Mapping[str, str | int | bool] | None,
    ) -> list[VectorHit]:
        with self._lock:
            collection = self._get(name)
            self._check_vector(name, collection, vector)
            conditions = [
                q.FieldCondition(key=key, match=q.MatchValue(value=value))
                for key, value in self._checked_filter(filter).items()
            ]
            request = q.SearchRequest(
                query=q.Query.Nearest(list(vector)),
                limit=k,
                filter=q.Filter(must=conditions) if conditions else None,
                with_payload=True,
            )
            found = collection.shard.search(request)
        hits = []
        for point in found:
            payload = dict(point.payload or {})
            original = payload.pop(_ORIGINAL_ID_KEY)
            # The engine reports euclid as a distance (smaller is nearer); the
            # interface promises higher is nearer for every distance.
            score = -point.score if collection.distance == "euclid" else point.score
            hits.append(VectorHit(id=original, score=score, payload=payload))
        return sorted(hits, key=lambda hit: hit.score, reverse=True)

    def _delete(self, name: str, ids: Sequence[str]) -> None:
        with self._lock:
            collection = self._get(name)
            if not ids:
                return
            collection.shard.update(
                q.UpdateOperation.delete_points([_point_id(i) for i in ids])
            )
            collection.shard.flush()

    def _close(self) -> None:
        with self._lock:
            self._closed = True
            collections, self._collections = self._collections, {}
            failed: list[str] = []
            for name, collection in collections.items():
                try:
                    collection.shard.flush()
                    collection.shard.close()
                except Exception:  # noqa: BLE001 - close every shard, then report
                    failed.append(name)
                    with contextlib.suppress(Exception):
                        collection.shard.close()
            if failed:
                raise VectorStoreError(
                    f"vector store did not close cleanly: {', '.join(failed)}"
                )
