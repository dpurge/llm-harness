"""Live check of `QdrantRemoteVectorStore` against a real qdrant server.

Deselected by default (`pytest -m live`). Skips, with a printed reason, when
`LLM_HARNESS_LIVE_QDRANT_URL` is unset or the server does not answer within 3 s.

Run it:

    docker --context rancher-desktop run -d --rm --name llm-harness-live-qdrant -p 127.0.0.1:16333:6333 qdrant/qdrant:latest
    LLM_HARNESS_LIVE_QDRANT_URL=http://127.0.0.1:16333 uv run pytest -m live tests/test_live_qdrant.py
    docker --context rancher-desktop rm -f llm-harness-live-qdrant

Every collection has a unique name and is dropped through the raw client in a
`finally` block. Add `-s` to see the warnings the real client emitted.
"""

import asyncio
import os
import urllib.error
import urllib.request
import uuid
import warnings
from collections.abc import Awaitable, Callable

import pytest
from qdrant_client import AsyncQdrantClient

from llm_harness.redact import Secret
from llm_harness.storage.qdrant_remote import QdrantRemoteVectorStore
from llm_harness.storage.vectors import VectorPoint, VectorStoreError

pytestmark = pytest.mark.live

URL_VARIABLE = "LLM_HARNESS_LIVE_QDRANT_URL"
KEY = "sk-test-SECRET-123"
WHOLE_TEST_SECONDS = 30
REACHABLE_CHECK_SECONDS = 3


def live_url() -> str:
    url = os.environ.get(URL_VARIABLE)
    if not url:
        pytest.skip(f"{URL_VARIABLE} is not set (see the module docstring)")
    try:
        with urllib.request.urlopen(
            url.rstrip("/") + "/healthz", timeout=REACHABLE_CHECK_SECONDS
        ):
            pass
    except (urllib.error.URLError, OSError) as exc:
        pytest.skip(f"no qdrant answered at {url} within 3 s: {type(exc).__name__}")
    return url


def unique_name(label: str) -> str:
    return f"live_{label}_{uuid.uuid4().hex[:12]}"


def points() -> list[VectorPoint]:
    return [
        VectorPoint(id="a", vector=[1.0, 0.0], payload={"user_id": "u1"}),
        VectorPoint(id="b", vector=[0.9, 0.436], payload={"user_id": "u1"}),
        VectorPoint(id="c", vector=[0.0, 1.0], payload={"user_id": "u2"}),
    ]


async def run_bounded(
    body: Callable[[str, list[str], list[warnings.WarningMessage]], Awaitable[None]],
) -> None:
    """Run `body(url, names, warnings)` under one 30 s bound; drop `names` afterwards."""
    url = live_url()
    names: list[str] = []
    raw = AsyncQdrantClient(url=url, timeout=10)
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            await asyncio.wait_for(body(url, names, caught), WHOLE_TEST_SECONDS)
    finally:
        try:
            for name in names:
                await raw.delete_collection(name)
        finally:
            await raw.close()
    for warning in caught:
        print(f"client warning ({warning.category.__name__}): {warning.message}")
    if not caught:
        print("client warnings: none")


async def test_live_remote_store_end_to_end() -> None:
    async def body(
        url: str, names: list[str], caught: list[warnings.WarningMessage]
    ) -> None:
        store = QdrantRemoteVectorStore(url)  # no api key: see the warnings printed
        keyed = QdrantRemoteVectorStore(url, Secret(KEY))
        try:
            for distance in ("cosine", "dot", "euclid"):
                name = unique_name(distance)
                names.append(name)
                await store.ensure_collection(name, 2, distance)
                await store.upsert(name, points())
                hits = await store.search(name, [1.0, 0.0], 3)
                assert [hit.id for hit in hits] == ["a", "b", "c"], distance
                scores = [hit.score for hit in hits]
                assert scores == sorted(scores, reverse=True), distance
                if distance == "euclid":
                    # Distances are negated: nearest is the highest (0.0), all <= 0.
                    assert scores[0] == pytest.approx(0.0, abs=1e-6)
                    assert all(score <= 0 for score in scores)
                else:
                    assert scores[0] == pytest.approx(1.0, abs=1e-4)
                    assert scores[2] == pytest.approx(0.0, abs=1e-6)

            name = names[0]  # the cosine collection
            filtered = await store.search(name, [1.0, 0.0], 5, {"user_id": "u1"})
            assert sorted(hit.id for hit in filtered) == ["a", "b"]
            assert all(hit.payload == {"user_id": "u1"} for hit in filtered)
            with pytest.raises(VectorStoreError, match="bool"):
                await store.search(name, [1.0, 0.0], 5, {"flag": True})

            await store.upsert(
                name,
                [VectorPoint(id="a", vector=[1.0, 0.0], payload={"user_id": "u9"})],
            )
            everything = await store.search(name, [1.0, 0.0], 10)
            assert [hit.id for hit in everything].count("a") == 1
            assert len(everything) == 3

            await store.delete(name, ["a"])
            remaining = await store.search(name, [1.0, 0.0], 10)
            assert sorted(hit.id for hit in remaining) == ["b", "c"]

            # Wrong dimension: refused from the cache, and by the server's own
            # settings from a NEW instance (the server-side compare).
            with pytest.raises(VectorStoreError, match="2-dimensional"):
                await store.ensure_collection(name, 3)
            fresh = QdrantRemoteVectorStore(url, Secret(KEY))
            try:
                with pytest.raises(
                    VectorStoreError, match="2-dimensional"
                ) as dim_error:
                    await fresh.ensure_collection(name, 3)
                with pytest.raises(
                    VectorStoreError, match="'cosine' distance"
                ) as other:
                    await fresh.ensure_collection(name, 2, "dot")
                await fresh.ensure_collection(name, 2, "cosine")  # same settings: fine
            finally:
                await fresh.close()
            assert KEY not in str(dim_error.value) and KEY not in str(other.value)

            # A collection dropped behind the store's back: HTTP 404.
            gone = unique_name("gone")
            names.append(gone)
            await keyed.ensure_collection(gone, 2)
            raw = AsyncQdrantClient(url=url, timeout=10)
            try:
                await raw.delete_collection(gone)
            finally:
                await raw.close()
            with pytest.raises(VectorStoreError, match="unknown collection") as unknown:
                await keyed.search(gone, [1.0, 0.0], 1)
            assert KEY not in str(unknown.value)
        finally:
            await store.close()
            await keyed.close()
        for warning in caught:
            assert KEY not in str(warning.message)

    await run_bounded(body)


async def test_live_refused_connection_becomes_a_store_error_quickly() -> None:
    live_url()  # same skip rules: no docker, no live run
    store = QdrantRemoteVectorStore("http://127.0.0.1:1", Secret(KEY), timeout=3)
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            with pytest.raises(VectorStoreError) as refused:
                await asyncio.wait_for(
                    store.ensure_collection(unique_name("refused"), 2), 10
                )
    finally:
        await store.close()
    assert KEY not in str(refused.value)
    assert "127.0.0.1" not in str(refused.value)
    for warning in caught:
        print(f"client warning ({warning.category.__name__}): {warning.message}")
