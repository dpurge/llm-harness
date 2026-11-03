"""The vector store backend on a qdrant server (`qdrant-client`, async, over HTTP).

Mirrors the observable contract of `qdrant_edge`: the same point-id mapping (uuid5,
the original id kept in payload `_id`), the same refusals and messages, euclid
scores negated so that higher is nearer. The optional extra `remote` provides
`qdrant-client` (`uv sync --extra remote`); this module imports without it, and
the client is imported and created at first use, never at construction.

Verified against qdrant server 1.20.0 with qdrant-client 1.20.0: string ids such as
"a" are refused with HTTP 400, a wrong dimension is HTTP 400, a missing collection
is HTTP 404, creating an existing one is HTTP 409, deleting a missing id succeeds
silently, and `get_collection(...).config.params.vectors` carries `size` and
`distance`, so no metadata file is needed.

Error text names the collection and the exception type (plus the HTTP status), and
never the URL, the api key or a response body: a body can echo request data.
"""

import contextlib
from collections.abc import Callable, Iterator, Mapping, Sequence
from types import ModuleType
from typing import Any
from urllib.parse import urlsplit

from llm_harness.redact import Secret
from llm_harness.storage.vector_common import (
    ORIGINAL_ID_KEY,
    check_name,
    checked_filter,
    point_id,
)
from llm_harness.storage.vectors import (
    DISTANCES,
    VectorHit,
    VectorPoint,
    VectorStoreError,
)

DEFAULT_TIMEOUT_SECONDS = 10
_HTTP_NOT_FOUND = 404
_HTTP_CONFLICT = 409
_MISSING_CLIENT = (
    "the qdrant backend needs the optional package qdrant-client: "
    "run `uv sync --extra remote`"
)


def insecure_key_warning(url: str, api_key: Secret | None) -> str | None:
    """One warning line when an api key would travel over plain http, else None.

    Names the host only: the line contains neither the key nor the full url.
    """
    if api_key is None:
        return None
    parts = urlsplit(url)
    if parts.scheme.lower() != "http":
        return None
    return (
        f"warning: the qdrant api key is sent unencrypted to {parts.hostname} "
        "over plain http; use an https:// url in [storage.vector] to protect it"
    )


def _status_code(exc: Exception) -> int | None:
    code = getattr(exc, "status_code", None)
    return code if isinstance(code, int) and not isinstance(code, bool) else None


def _unknown_collection(name: str) -> VectorStoreError:
    return VectorStoreError(
        f"unknown collection {name!r}: call ensure_collection first"
    )


def _failure(
    name: str, exc: Exception, *, missing_is_unknown: bool
) -> VectorStoreError:
    """Reduce a client failure to the collection, the exception type and the status."""
    code = _status_code(exc)
    if code == _HTTP_NOT_FOUND and missing_is_unknown:
        return _unknown_collection(name)
    status = f" (HTTP {code})" if code is not None else ""
    return VectorStoreError(
        f"vector store failed on collection {name!r}: {type(exc).__name__}{status}"
    )


@contextlib.contextmanager
def _guard(name: str, *, missing_is_unknown: bool = True) -> Iterator[None]:
    try:
        yield
    except VectorStoreError:
        raise
    except Exception as exc:  # noqa: BLE001 - client failures have no common class
        # Exception, not BaseException: cancellation and Ctrl-C pass through.
        raise _failure(name, exc, missing_is_unknown=missing_is_unknown) from None


class QdrantRemoteVectorStore:
    """Collections on one qdrant server. Construction connects to nothing.

    After `close()` every call raises `VectorStoreError`, like the edge backend.
    Dimension and distance are cached by `ensure_collection` so a wrong vector
    length is refused before it is sent.
    """

    def __init__(
        self,
        url: str,
        api_key: Secret | None = None,
        *,
        client_factory: Callable[[], Any] | None = None,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._url = url
        self._api_key = api_key
        self._client_factory = client_factory
        self._timeout = timeout
        self._client: Any = None
        self._models: ModuleType | None = None
        self._collections: dict[str, tuple[int, str]] = {}
        self._closed = False

    def __repr__(self) -> str:
        # Neither the url nor the key: either may carry credentials.
        return f"QdrantRemoteVectorStore(api_key={'set' if self._api_key else 'none'})"

    async def ensure_collection(
        self, name: str, dim: int, distance: str = "cosine"
    ) -> None:
        check_name(name)
        if isinstance(dim, bool) or not isinstance(dim, int) or dim < 1:
            raise VectorStoreError(f"the dimension must be a positive int, got {dim!r}")
        if distance not in DISTANCES:
            raise VectorStoreError(
                f"unknown distance {distance!r}: use one of {', '.join(DISTANCES)}"
            )
        self._check_open()
        held = self._collections.get(name)
        if held is not None:
            self._check_matches(name, held[0], held[1], dim, distance)
            return
        with _guard(name, missing_is_unknown=False):
            client, models = self._connect()
            if await client.collection_exists(name):
                await self._check_server_collection(client, models, name, dim, distance)
            else:
                await self._create(client, models, name, dim, distance)
        self._collections[name] = (dim, distance)

    async def upsert(self, collection: str, points: Sequence[VectorPoint]) -> None:
        check_name(collection)
        dim, _ = self._known(collection)
        with _guard(collection):
            # Every point is checked before anything is sent: a batch is written
            # whole or not at all.
            structs = []
            models = self._models_module()
            for point in points:
                self._check_vector(collection, dim, point.vector)
                if ORIGINAL_ID_KEY in point.payload:
                    raise VectorStoreError(
                        f"the payload key {ORIGINAL_ID_KEY!r} is reserved for the point id"
                    )
                structs.append(
                    models.PointStruct(
                        id=point_id(point.id),
                        vector=[float(value) for value in point.vector],
                        payload={**point.payload, ORIGINAL_ID_KEY: point.id},
                    )
                )
            if not structs:
                return
            client, _ = self._connect()
            # wait=True: a returned call means the change is applied (like edge `flush`).
            await client.upsert(collection_name=collection, points=structs, wait=True)

    async def search(
        self,
        collection: str,
        vector: Sequence[float],
        k: int,
        filter: Mapping[str, str | int | bool] | None = None,
    ) -> list[VectorHit]:
        check_name(collection)
        if isinstance(k, bool) or not isinstance(k, int) or k < 1:
            raise VectorStoreError(f"k must be a positive int, got {k!r}")
        dim, distance = self._known(collection)
        self._check_vector(collection, dim, vector)
        conditions_filter = checked_filter(filter)
        with _guard(collection):
            client, models = self._connect()
            conditions = [
                models.FieldCondition(key=key, match=models.MatchValue(value=value))
                for key, value in conditions_filter.items()
            ]
            response = await client.query_points(
                collection_name=collection,
                query=[float(value) for value in vector],
                limit=k,
                query_filter=models.Filter(must=conditions) if conditions else None,
                with_payload=True,
            )
            hits = []
            for found in response.points:
                payload = dict(found.payload or {})
                original = payload.pop(ORIGINAL_ID_KEY)
                # The server reports euclid as a distance (smaller is nearer); the
                # interface promises higher is nearer for every distance.
                score = -found.score if distance == "euclid" else found.score
                hits.append(VectorHit(id=original, score=score, payload=payload))
        return sorted(hits, key=lambda hit: hit.score, reverse=True)

    async def delete(self, collection: str, ids: Sequence[str]) -> None:
        check_name(collection)
        self._known(collection)
        if not ids:
            return
        with _guard(collection):
            client, models = self._connect()
            await client.delete(
                collection_name=collection,
                points_selector=models.PointIdsList(
                    points=[point_id(original) for original in ids]
                ),
                wait=True,
            )

    async def close(self) -> None:
        client, self._client = self._client, None
        self._closed = True
        self._collections = {}
        if client is None:
            return
        try:
            await client.close()
        except Exception as exc:  # noqa: BLE001 - client failures have no common class
            raise VectorStoreError(
                f"vector store did not close cleanly: {type(exc).__name__}"
            ) from None

    def _check_open(self) -> None:
        if self._closed:
            raise VectorStoreError("the vector store is closed")

    def _known(self, name: str) -> tuple[int, str]:
        self._check_open()
        known = self._collections.get(name)
        if known is None:
            raise _unknown_collection(name)
        return known

    def _models_module(self) -> ModuleType:
        if self._models is None:
            try:
                from qdrant_client import models
            except ImportError:
                raise VectorStoreError(_MISSING_CLIENT) from None
            self._models = models
        return self._models

    def _connect(self) -> tuple[Any, ModuleType]:
        """The client (created once, at first use) and the request models."""
        self._check_open()
        models = self._models_module()
        if self._client is None:
            factory = self._client_factory or self._default_client
            self._client = factory()
        return self._client, models

    def _default_client(self) -> Any:
        from qdrant_client import AsyncQdrantClient

        return AsyncQdrantClient(
            url=self._url,
            api_key=self._api_key.reveal() if self._api_key else None,
            timeout=self._timeout,
            # The default starts a daemon thread at construction that fetches the
            # server version and warns "Failed to obtain server version" on stderr
            # when it fails. Giving up the client/server version-mismatch warning
            # on purpose (decided by the human, 2026-11-03).
            check_compatibility=False,
        )

    async def _create(
        self, client: Any, models: ModuleType, name: str, dim: int, distance: str
    ) -> None:
        params = models.VectorParams(
            size=dim, distance=models.Distance(distance.capitalize())
        )
        try:
            await client.create_collection(name, vectors_config=params)
        except Exception as exc:
            if _status_code(exc) != _HTTP_CONFLICT:
                raise
            # Someone created it between our check and our create: compare instead.
            await self._check_server_collection(client, models, name, dim, distance)

    async def _check_server_collection(
        self, client: Any, models: ModuleType, name: str, dim: int, distance: str
    ) -> None:
        info = await client.get_collection(name)
        vectors = info.config.params.vectors
        if not isinstance(vectors, models.VectorParams):
            raise VectorStoreError(
                f"collection {name!r} on the server does not hold a single unnamed vector; "
                "use another name"
            )
        stored_distance = str(models.Distance(vectors.distance).value).lower()
        if stored_distance not in DISTANCES:
            raise VectorStoreError(
                f"collection {name!r} on the server uses the {stored_distance!r} distance, "
                f"which is not one of {', '.join(DISTANCES)}; use another name"
            )
        self._check_matches(name, vectors.size, stored_distance, dim, distance)

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

    @staticmethod
    def _check_vector(name: str, dim: int, vector: Sequence[float]) -> None:
        if len(vector) != dim:
            raise VectorStoreError(
                f"collection {name!r} holds {dim}-dimensional vectors, "
                f"got a vector of {len(vector)}"
            )
