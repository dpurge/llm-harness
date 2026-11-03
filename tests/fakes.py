"""Test doubles shared by adapter, router, front-end and live tests."""

import asyncio
import math
import re
from collections.abc import AsyncIterator, Mapping, Sequence

from llm_harness.config import HarnessConfig, ModelConfig
from llm_harness.embeddings import EmbeddingError
from llm_harness.providers import ProviderFactory
from llm_harness.providers.base import ChatProvider
from llm_harness.router import RouterFactory, make_router_factory
from llm_harness.storage.vectors import (
    DISTANCES,
    VectorHit,
    VectorPoint,
    VectorStoreError,
)
from llm_harness.store import Store
from llm_harness.types import (
    ChatRequest,
    Done,
    ErrorEvent,
    Message,
    ModelSwitch,
    Notice,
    StreamEvent,
    TextDelta,
    ThinkingDelta,
    UsageEvent,
    UsageReport,
)


class ScriptedProvider:
    """Yields scripted events; optionally hangs after `hang_after` of them."""

    def __init__(
        self, events: list[StreamEvent], *, hang_after: int | None = None
    ) -> None:
        self._events = events
        self._hang_after = hang_after
        self.requests: list[ChatRequest] = []
        self.closed = False

    async def stream_chat(self, req: ChatRequest) -> AsyncIterator[StreamEvent]:
        self.requests.append(req.model_copy(deep=True))
        try:
            for index, event in enumerate(self._events):
                if index == self._hang_after:
                    await asyncio.Event().wait()
                yield event
            if self._hang_after == len(self._events):
                await asyncio.Event().wait()
        finally:
            self.closed = True

    async def aclose(self) -> None:
        return None


class RecordingRouterFactory:
    """A RouterFactory over make_router_factory that remembers (model id, is_manual)."""

    def __init__(self, inner: RouterFactory) -> None:
        self._inner = inner
        self.calls: list[tuple[str, bool]] = []

    def __call__(self, model: ModelConfig, is_manual: bool) -> ChatProvider:
        self.calls.append((model.id, is_manual))
        return self._inner(model, is_manual)


def router_factory_for(
    config: HarnessConfig,
    provider_factory: ProviderFactory,
    think_flag: bool | None = None,
    store: Store | None = None,
) -> RecordingRouterFactory:
    """The router factory the front ends get, over a scripted provider factory."""
    return RecordingRouterFactory(
        make_router_factory(config, provider_factory, think_flag, store)
    )


async def single_turn(
    provider: ChatProvider, model: str, text: str, *, think: bool | None = None
) -> list[StreamEvent]:
    """One user message straight through a provider: no history, no orchestrator."""
    request = ChatRequest(
        model=model, messages=[Message(role="user", content=text)], think=think
    )
    return [event async for event in provider.stream_chat(request)]


def assert_stream_contract(events: list[StreamEvent]) -> None:
    """Assert the adapter contract, plus the router's extra events.

    Deltas, then at most one usage, then exactly one terminal event last.
    ModelSwitch and Notice come before the first delta; at most one UsageReport
    comes after every delta and just before the terminal event.
    """
    terminals = [i for i, e in enumerate(events) if isinstance(e, (Done, ErrorEvent))]
    assert len(terminals) == 1, f"expected exactly one terminal event, got {terminals}"
    assert terminals[0] == len(events) - 1, "terminal event must be last"
    body = events[:-1]
    deltas = [
        i for i, e in enumerate(body) if isinstance(e, (ThinkingDelta, TextDelta))
    ]
    notes = [i for i, e in enumerate(body) if isinstance(e, (ModelSwitch, Notice))]
    assert all(not deltas or n < deltas[0] for n in notes), (
        "ModelSwitch and Notice must come before the first delta"
    )
    usages = [i for i, e in enumerate(body) if isinstance(e, UsageEvent)]
    reports = [i for i, e in enumerate(body) if isinstance(e, UsageReport)]
    assert len(usages) <= 1, "at most one usage event"
    assert len(reports) <= 1, "at most one usage report"
    assert len(body) == len(deltas) + len(notes) + len(usages) + len(reports)
    after_deltas = usages + reports
    assert all(not deltas or i > deltas[-1] for i in after_deltas), (
        "usage must come after every delta"
    )
    if reports:
        assert reports[0] == len(body) - 1, "usage report must be just before terminal"
    if usages:
        assert usages[0] == len(body) - 1 - len(reports), "usage must come last"


class FakeEmbedder:
    """Deterministic EmbeddingProvider: vector i of a text is built from its
    length and position; records calls; can be scripted to raise."""

    def __init__(self, *, size: int = 4, raises: EmbeddingError | None = None) -> None:
        self.size = size
        self.raises = raises
        self.calls: list[tuple[str, list[str], int | None]] = []
        self.closed = False

    async def embed(
        self, model: str, texts: list[str], dimensions: int | None
    ) -> list[list[float]]:
        self.calls.append((model, list(texts), dimensions))
        if self.raises is not None:
            raise self.raises
        size = dimensions if dimensions is not None else self.size
        return [[float(len(text) + i) for i in range(size)] for text in texts]

    async def aclose(self) -> None:
        self.closed = True


class HangingEmbedder:
    """EmbeddingProvider whose server accepted the call and never answers."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str], int | None]] = []

    async def embed(
        self, model: str, texts: list[str], dimensions: int | None
    ) -> list[list[float]]:
        self.calls.append((model, list(texts), dimensions))
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def aclose(self) -> None:
        pass


_CONCEPT_WORDS: tuple[frozenset[str], ...] = (
    frozenset(
        {
            "diet",
            "cook",
            "cooking",
            "peanuts",
            "peanut",
            "allergic",
            "allergy",
            "eat",
            "meal",
        }
    ),
    frozenset({"partner", "her", "she", "wife", "girlfriend", "he", "him"}),
    frozenset({"trip", "flight", "hotel", "budget"}),
)
_WORD = re.compile(r"[a-z0-9']+")


class ConceptEmbedder:
    """EmbeddingProvider that maps words to concept axes (food, person, travel).

    The vector has one axis per concept plus a constant bias axis, so no vector is
    all zeros, and is normalised. Same text, same vector. Records calls; can be
    scripted to raise.
    """

    SIZE = len(_CONCEPT_WORDS) + 1

    def __init__(self, *, raises: EmbeddingError | None = None) -> None:
        self.raises = raises
        self.calls: list[tuple[str, list[str], int | None]] = []
        self.closed = False

    @staticmethod
    def vector_for(text: str) -> list[float]:
        words = _WORD.findall(text.lower())
        raw = [
            float(sum(word in concept for word in words)) for concept in _CONCEPT_WORDS
        ]
        raw.append(1.0)
        norm = math.sqrt(sum(value * value for value in raw))
        return [value / norm for value in raw]

    async def embed(
        self, model: str, texts: list[str], dimensions: int | None
    ) -> list[list[float]]:
        self.calls.append((model, list(texts), dimensions))
        if self.raises is not None:
            raise self.raises
        return [self.vector_for(text) for text in texts]

    async def aclose(self) -> None:
        self.closed = True


_COLLECTION_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")
_RESERVED_KEY = "_id"


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    norms = math.sqrt(sum(a * a for a in left)) * math.sqrt(sum(b * b for b in right))
    return (
        sum(a * b for a, b in zip(left, right, strict=True)) / norms if norms else 0.0
    )


class FakeVectorStore:
    """In-memory VectorStore that mirrors the rules of the real qdrant_edge adapter.

    Cosine only, higher score is nearer. Records every call as (method, args) in
    `calls`; `fail_on` maps a method name to an exception raised when it is called.
    """

    def __init__(self, *, fail_on: Mapping[str, Exception] | None = None) -> None:
        self.fail_on = dict(fail_on or {})
        self.calls: list[tuple[str, tuple]] = []
        self.closed = False
        self._collections: dict[str, tuple[int, str]] = {}
        self._points: dict[str, dict[str, VectorPoint]] = {}

    def _enter(self, method: str, *args: object) -> None:
        self.calls.append((method, args))
        if method in self.fail_on:
            raise self.fail_on[method]

    def _known(self, name: str) -> dict[str, VectorPoint]:
        if name not in self._collections:
            raise VectorStoreError(
                f"unknown collection {name!r}: call ensure_collection first"
            )
        return self._points[name]

    async def ensure_collection(
        self, name: str, dim: int, distance: str = "cosine"
    ) -> None:
        self._enter("ensure_collection", name, dim, distance)
        if not _COLLECTION_NAME.fullmatch(name):
            raise VectorStoreError(
                "a collection name is a lowercase letter then up to 63 of a-z, 0-9 and '_'"
            )
        if distance not in DISTANCES:
            raise VectorStoreError(f"unknown distance {distance!r}")
        stored = self._collections.get(name)
        if stored is None:
            self._collections[name] = (dim, distance)
            self._points[name] = {}
        elif stored[0] != dim:
            raise VectorStoreError(
                f"collection {name!r} holds {stored[0]}-dimensional vectors, got {dim}"
            )
        elif stored[1] != distance:
            raise VectorStoreError(
                f"collection {name!r} uses the {stored[1]!r} distance, got {distance!r}"
            )

    async def upsert(self, collection: str, points: Sequence[VectorPoint]) -> None:
        self._enter("upsert", collection, tuple(points))
        stored = self._known(collection)
        dim = self._collections[collection][0]
        for point in points:
            if len(point.vector) != dim:
                raise VectorStoreError(
                    f"collection {collection!r} holds {dim}-dimensional vectors, "
                    f"got a vector of {len(point.vector)}"
                )
            if _RESERVED_KEY in point.payload:
                raise VectorStoreError(
                    f"the payload key {_RESERVED_KEY!r} is reserved for the point id"
                )
        for point in points:
            stored[point.id] = point

    async def search(
        self,
        collection: str,
        vector: Sequence[float],
        k: int,
        filter: Mapping[str, str | int | bool] | None = None,
    ) -> list[VectorHit]:
        self._enter("search", collection, tuple(vector), k, dict(filter or {}))
        stored = self._known(collection)
        dim = self._collections[collection][0]
        if len(vector) != dim:
            raise VectorStoreError(
                f"collection {collection!r} holds {dim}-dimensional vectors, "
                f"got a vector of {len(vector)}"
            )
        for key, value in (filter or {}).items():
            if isinstance(value, bool):
                raise VectorStoreError(
                    f"cannot filter on {key!r} with a bool: store 0 or 1, or a string, instead"
                )
        wanted = filter or {}
        hits = [
            VectorHit(point.id, _cosine(vector, point.vector), dict(point.payload))
            for point in stored.values()
            if all(point.payload.get(key) == value for key, value in wanted.items())
        ]
        hits.sort(key=lambda hit: hit.score, reverse=True)
        return hits[: max(k, 0)]

    async def delete(self, collection: str, ids: Sequence[str]) -> None:
        self._enter("delete", collection, tuple(ids))
        stored = self._known(collection)
        for point_id in ids:
            stored.pop(point_id, None)

    async def close(self) -> None:
        self._enter("close")
        self.closed = True
