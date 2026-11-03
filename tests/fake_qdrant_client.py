"""An in-memory stand-in for `qdrant_client.AsyncQdrantClient`, for offline tests.

It follows what the spike against a real server (qdrant 1.20.0, qdrant-client 1.20.0)
showed: ids must be uuids or ints (HTTP 400), a wrong dimension is HTTP 400, a missing
collection is HTTP 404, creating an existing one is HTTP 409, deleting a missing id
succeeds silently, cosine scores are the normalised dot product, dot is raw, and euclid
is a distance (nearest 0.0). It raises the real `UnexpectedResponse` and
`ResponseHandlingException` classes and reads the real request models.

It is a test double: the `live` test (step 4b of storage-backends-qdrant) is what checks
it against a real server.
"""

import math
import uuid
from types import SimpleNamespace
from typing import Any

import httpx
from qdrant_client import models
from qdrant_client.http.exceptions import ResponseHandlingException, UnexpectedResponse

_SECRET_BODY = "echoed request data: do not show this"


def http_error(status: int) -> UnexpectedResponse:
    """The error a real server gives; its body echoes request data like the real one can."""
    return UnexpectedResponse(
        status_code=status,
        reason_phrase="Fake",
        content=_SECRET_BODY.encode(),
        headers=httpx.Headers(),
    )


class FakeQdrantServer:
    """Shared state: a second client sees what an earlier one stored, like a real server."""

    def __init__(self) -> None:
        self.collections: dict[str, dict[str, Any]] = {}
        self.refuse_connections = False
        self.fail_next_with: Exception | None = None


class FakeQdrantClient:
    def __init__(self, server: FakeQdrantServer) -> None:
        self.server = server
        self.closed_count = 0
        self.calls: list[str] = []

    async def collection_exists(self, collection_name: str) -> bool:
        self._enter("collection_exists")
        return collection_name in self.server.collections

    async def create_collection(
        self, collection_name: str, vectors_config: Any = None, **_: Any
    ) -> bool:
        self._enter("create_collection")
        if collection_name in self.server.collections:
            raise http_error(409)
        self.server.collections[collection_name] = {
            "params": vectors_config,
            "points": {},
        }
        return True

    async def get_collection(self, collection_name: str) -> Any:
        self._enter("get_collection")
        collection = self._collection(collection_name)
        params = SimpleNamespace(vectors=collection["params"])
        return SimpleNamespace(config=SimpleNamespace(params=params))

    async def upsert(
        self, collection_name: str, points: Any, wait: bool = True, **_: Any
    ) -> None:
        self._enter("upsert")
        collection = self._collection(collection_name)
        for point in points:
            self._check_id(point.id)
            self._check_dim(collection, point.vector)
        for point in points:
            collection["points"][str(point.id)] = (
                list(point.vector),
                dict(point.payload or {}),
            )

    async def delete(
        self, collection_name: str, points_selector: Any, wait: bool = True, **_: Any
    ) -> None:
        self._enter("delete")
        collection = self._collection(collection_name)
        for original in points_selector.points:
            self._check_id(original)
            collection["points"].pop(str(original), None)  # a missing id is no error

    async def query_points(
        self,
        collection_name: str,
        query: Any,
        limit: int = 10,
        query_filter: Any = None,
        with_payload: bool = True,
        **_: Any,
    ) -> Any:
        self._enter("query_points")
        collection = self._collection(collection_name)
        self._check_dim(collection, query)
        wanted = {
            condition.key: condition.match.value
            for condition in (query_filter.must if query_filter else [])
        }
        scored = []
        for key, (vector, payload) in collection["points"].items():
            if all(payload.get(k) == v for k, v in wanted.items()):
                score = _score(collection["params"].distance, query, vector)
                scored.append((score, key, payload))
        # Higher is nearer for cosine and dot; euclid's score is a distance.
        euclid = collection["params"].distance == models.Distance.EUCLID
        scored.sort(key=lambda item: item[0], reverse=not euclid)
        return SimpleNamespace(
            points=[
                SimpleNamespace(id=key, score=score, payload=dict(payload))
                for score, key, payload in scored[:limit]
            ]
        )

    async def close(self) -> None:
        self.closed_count += 1

    def _enter(self, call: str) -> None:
        self.calls.append(call)
        if self.server.refuse_connections:
            raise ResponseHandlingException(
                ConnectionError("All connection attempts failed")
            )
        failure, self.server.fail_next_with = self.server.fail_next_with, None
        if failure is not None:
            raise failure

    def _collection(self, name: str) -> dict[str, Any]:
        if name not in self.server.collections:
            raise http_error(404)
        return self.server.collections[name]

    @staticmethod
    def _check_id(point_id: Any) -> None:
        if isinstance(point_id, int):
            return
        try:
            uuid.UUID(str(point_id))
        except ValueError:
            raise http_error(400) from None

    @staticmethod
    def _check_dim(collection: dict[str, Any], vector: Any) -> None:
        if len(vector) != collection["params"].size:
            raise http_error(400)


def _score(distance: models.Distance, query: list[float], vector: list[float]) -> float:
    dot = sum(a * b for a, b in zip(query, vector, strict=True))
    if distance == models.Distance.DOT:
        return dot
    if distance == models.Distance.EUCLID:
        return math.dist(query, vector)
    norms = math.hypot(*query) * math.hypot(*vector)
    return dot / norms if norms else 0.0
