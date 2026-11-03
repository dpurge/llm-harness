"""The remote vector store beyond the shared contract: laziness, secrets, error mapping.

Offline: the client is `tests/fake_qdrant_client.py`, never a socket.
"""

import asyncio
import subprocess
import sys
import warnings

import pytest
from qdrant_client import models

from llm_harness.redact import Secret
from llm_harness.storage.qdrant_remote import QdrantRemoteVectorStore
from llm_harness.storage.vectors import VectorPoint, VectorStoreError
from tests.fake_qdrant_client import (
    FakeQdrantClient,
    FakeQdrantServer,
    http_error,
)

URL = "http://user:pw-in-url@qdrant.internal.test:6333"
KEY = "sk-test-SECRET-123"
BODY = "echoed request data: do not show this"


class Harness:
    """A store wired to one fake server; `clients` lists every client the factory built."""

    def __init__(self, api_key: Secret | None = None) -> None:
        self.server = FakeQdrantServer()
        self.clients: list[FakeQdrantClient] = []
        self.store = QdrantRemoteVectorStore(URL, api_key, client_factory=self._build)

    def _build(self) -> FakeQdrantClient:
        client = FakeQdrantClient(self.server)
        self.clients.append(client)
        return client


@pytest.fixture
def harness() -> Harness:
    return Harness(Secret(KEY))


def _point(id: str, vector: list[float], **payload: str | int) -> VectorPoint:
    return VectorPoint(id=id, vector=vector, payload=payload)


def _assert_clean(error: BaseException) -> None:
    text = f"{error} {error!r}"
    for forbidden in (KEY, "qdrant.internal.test", "pw-in-url", BODY, "Raw response"):
        assert forbidden not in text


async def test_construction_calls_no_factory_and_makes_no_connection() -> None:
    calls: list[int] = []

    def factory() -> FakeQdrantClient:
        calls.append(1)
        raise AssertionError("the factory must not run at construction")

    QdrantRemoteVectorStore(URL, Secret(KEY), client_factory=factory)
    assert calls == []


async def test_the_default_client_is_not_built_at_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import qdrant_client

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("AsyncQdrantClient must not be built at construction")

    monkeypatch.setattr(qdrant_client, "AsyncQdrantClient", forbidden)
    QdrantRemoteVectorStore(URL, Secret(KEY))


async def test_the_default_client_gets_the_url_the_revealed_key_and_the_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import qdrant_client

    seen: dict[str, object] = {}

    class Recording(FakeQdrantClient):
        def __init__(self, **kwargs: object) -> None:
            seen.update(kwargs)
            super().__init__(FakeQdrantServer())

    monkeypatch.setattr(qdrant_client, "AsyncQdrantClient", Recording)
    store = QdrantRemoteVectorStore(URL, Secret(KEY), timeout=7)
    await store.ensure_collection("tools", 2)
    assert seen == {
        "url": URL,
        "api_key": KEY,
        "timeout": 7,
        "check_compatibility": False,
    }
    await store.close()


async def test_the_real_client_emits_no_version_warning_for_an_unreachable_server() -> (
    None
):
    # The default client starts a thread that warns when it cannot fetch the
    # server version; check_compatibility=False must prevent both.
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        store = QdrantRemoteVectorStore("http://127.0.0.1:1")
        _, _ = store._connect()
        await asyncio.sleep(1.5)
        await store.close()
    messages = [str(warning.message) for warning in caught]
    assert not [m for m in messages if "Failed to obtain server version" in m]


async def test_the_factory_runs_once_and_lazily(harness: Harness) -> None:
    assert harness.clients == []
    await harness.store.ensure_collection("tools", 2)
    await harness.store.upsert("tools", [_point("a", [1.0, 0.0])])
    await harness.store.search("tools", [1.0, 0.0], 1)
    assert len(harness.clients) == 1


async def test_validation_errors_do_not_create_a_client(harness: Harness) -> None:
    with pytest.raises(VectorStoreError):
        await harness.store.ensure_collection("Bad Name", 2)
    with pytest.raises(VectorStoreError):
        await harness.store.search("tools", [1.0], 1)  # unknown, refused locally
    assert harness.clients == []


async def test_repr_and_str_never_show_the_key_or_the_url(harness: Harness) -> None:
    for text in (repr(harness.store), str(harness.store), f"{harness.store}"):
        assert KEY not in text
        assert "qdrant.internal.test" not in text
        assert "pw-in-url" not in text


async def test_an_unauthenticated_store_says_so_in_its_repr() -> None:
    assert repr(QdrantRemoteVectorStore(URL)) == "QdrantRemoteVectorStore(api_key=none)"


async def test_every_payload_key_and_id_round_trips(harness: Harness) -> None:
    await harness.store.ensure_collection("tools", 2)
    payload = {
        "user_id": "u1",
        "rank": 3,
        "ratio": 0.5,
        "active": True,
        "text": "héllo",
    }
    ids = ["a", "with space/and:slash", "ключ", "x" * 300, "7"]
    await harness.store.upsert(
        "tools", [VectorPoint(id=i, vector=[1.0, 0.0], payload=payload) for i in ids]
    )
    hits = await harness.store.search("tools", [1.0, 0.0], 10)
    assert sorted(hit.id for hit in hits) == sorted(ids)
    assert all(hit.payload == payload for hit in hits)


async def test_the_server_never_sees_the_original_id_as_its_point_id(
    harness: Harness,
) -> None:
    await harness.store.ensure_collection("tools", 2)
    await harness.store.upsert("tools", [_point("a", [1.0, 0.0])])
    stored = harness.server.collections["tools"]["points"]
    assert "a" not in stored
    assert [payload["_id"] for _, payload in stored.values()] == ["a"]


async def test_euclid_scores_are_negated_distances(harness: Harness) -> None:
    await harness.store.ensure_collection("tools", 2, "euclid")
    await harness.store.upsert(
        "tools", [_point("near", [1.0, 0.0]), _point("far", [4.0, 0.0])]
    )
    hits = await harness.store.search("tools", [1.0, 0.0], 2)
    assert [(h.id, h.score) for h in hits] == [("near", -0.0), ("far", -3.0)]


async def test_a_bool_filter_is_refused_before_any_request(harness: Harness) -> None:
    await harness.store.ensure_collection("tools", 2)
    harness.clients[0].calls.clear()
    with pytest.raises(VectorStoreError, match="'active' with a bool"):
        await harness.store.search("tools", [1.0, 0.0], 1, {"active": True})
    assert harness.clients[0].calls == []


async def test_the_filter_is_sent_as_exact_match_conditions(harness: Harness) -> None:
    await harness.store.ensure_collection("tools", 2)
    sent: list[models.Filter | None] = []
    client = harness.clients[0]
    original = client.query_points

    async def spy(*args: object, **kwargs: object):
        sent.append(kwargs["query_filter"])  # type: ignore[arg-type]
        return await original(*args, **kwargs)  # type: ignore[arg-type]

    client.query_points = spy  # type: ignore[method-assign]
    await harness.store.search("tools", [1.0, 0.0], 1, {"user_id": "u1", "rank": 2})
    await harness.store.search("tools", [1.0, 0.0], 1)
    assert sent[0] == models.Filter(
        must=[
            models.FieldCondition(key="user_id", match=models.MatchValue(value="u1")),
            models.FieldCondition(key="rank", match=models.MatchValue(value=2)),
        ]
    )
    assert sent[1] is None


async def test_a_bad_point_sends_nothing(harness: Harness) -> None:
    await harness.store.ensure_collection("tools", 2)
    client = harness.clients[0]
    with pytest.raises(VectorStoreError):
        await harness.store.upsert(
            "tools", [_point("ok", [1.0, 0.0]), _point("bad", [1.0, 0.0], _id="x")]
        )
    assert "upsert" not in client.calls


async def test_a_new_collection_is_created_with_the_requested_shape(
    harness: Harness,
) -> None:
    await harness.store.ensure_collection("tools", 5, "dot")
    params = harness.server.collections["tools"]["params"]
    assert (params.size, params.distance) == (5, models.Distance.DOT)


async def test_ensure_checks_a_collection_that_exists_on_the_server(
    harness: Harness,
) -> None:
    other = QdrantRemoteVectorStore(
        URL, client_factory=lambda: FakeQdrantClient(harness.server)
    )
    await other.ensure_collection("tools", 4, "dot")
    with pytest.raises(VectorStoreError, match="holds 4-dimensional vectors, got 2"):
        await harness.store.ensure_collection("tools", 2, "dot")
    with pytest.raises(VectorStoreError, match="'dot' distance, got 'cosine'"):
        await harness.store.ensure_collection("tools", 4)
    await harness.store.ensure_collection("tools", 4, "dot")
    await other.close()


async def test_a_collection_with_named_vectors_is_refused(harness: Harness) -> None:
    harness.server.collections["tools"] = {
        "params": {
            "dense": models.VectorParams(size=2, distance=models.Distance.COSINE)
        },
        "points": {},
    }
    with pytest.raises(VectorStoreError, match="single unnamed vector"):
        await harness.store.ensure_collection("tools", 2)


async def test_a_distance_outside_the_contract_is_refused(harness: Harness) -> None:
    harness.server.collections["tools"] = {
        "params": models.VectorParams(size=2, distance=models.Distance.MANHATTAN),
        "points": {},
    }
    with pytest.raises(VectorStoreError, match="'manhattan' distance"):
        await harness.store.ensure_collection("tools", 2)


async def test_a_concurrent_creator_is_not_an_error_when_the_shape_matches(
    harness: Harness,
) -> None:
    await _race(harness, size=2)
    await harness.store.ensure_collection("tools", 2)
    assert harness.clients[0].calls == [
        "collection_exists",
        "create_collection",
        "get_collection",
    ]


async def test_a_concurrent_creator_with_another_shape_is_refused(
    harness: Harness,
) -> None:
    await _race(harness, size=8)
    with pytest.raises(VectorStoreError, match="holds 8-dimensional vectors, got 2"):
        await harness.store.ensure_collection("tools", 2)


async def _race(harness: Harness, size: int) -> None:
    """Let the existence check say no, then have someone else create the collection."""
    client = harness._build()
    harness.store._client = client
    original = client.collection_exists

    async def exists_then_lose_the_race(name: str) -> bool:
        answer = await original(name)
        harness.server.collections[name] = {
            "params": models.VectorParams(size=size, distance=models.Distance.COSINE),
            "points": {},
        }
        return answer

    client.collection_exists = exists_then_lose_the_race  # type: ignore[method-assign]


@pytest.mark.parametrize("status", [400, 404, 409, 500, 503])
async def test_http_errors_become_store_errors_naming_the_status_not_the_body(
    harness: Harness, status: int
) -> None:
    await harness.store.ensure_collection("tools", 2)
    harness.server.fail_next_with = http_error(status)
    with pytest.raises(VectorStoreError) as failed:
        await harness.store.search("tools", [1.0, 0.0], 1)
    _assert_clean(failed.value)
    if status == 404:
        assert str(failed.value) == (
            "unknown collection 'tools': call ensure_collection first"
        )
    else:
        assert str(failed.value) == (
            f"vector store failed on collection 'tools': UnexpectedResponse (HTTP {status})"
        )


async def test_the_server_losing_a_collection_reads_as_unknown(
    harness: Harness,
) -> None:
    await harness.store.ensure_collection("tools", 2)
    harness.server.collections.clear()
    for call in (
        harness.store.upsert("tools", [_point("a", [1.0, 0.0])]),
        harness.store.search("tools", [1.0, 0.0], 1),
        harness.store.delete("tools", ["a"]),
    ):
        with pytest.raises(VectorStoreError, match="call ensure_collection first"):
            await call


async def test_a_404_while_ensuring_is_not_called_unknown_collection(
    harness: Harness,
) -> None:
    harness.server.fail_next_with = http_error(404)
    with pytest.raises(VectorStoreError) as failed:
        await harness.store.ensure_collection("tools", 2)
    assert "ensure_collection first" not in str(failed.value)
    assert str(failed.value).endswith("UnexpectedResponse (HTTP 404)")


async def test_a_refused_connection_becomes_a_store_error_without_the_url(
    harness: Harness,
) -> None:
    await harness.store.ensure_collection("tools", 2)
    harness.server.refuse_connections = True
    for call in (
        harness.store.upsert("tools", [_point("a", [1.0, 0.0])]),
        harness.store.search("tools", [1.0, 0.0], 1),
        harness.store.delete("tools", ["a"]),
    ):
        with pytest.raises(VectorStoreError) as failed:
            await call
        _assert_clean(failed.value)
        assert str(failed.value) == (
            "vector store failed on collection 'tools': ResponseHandlingException"
        )


async def test_a_refused_connection_while_ensuring_becomes_a_store_error(
    harness: Harness,
) -> None:
    harness.server.refuse_connections = True
    with pytest.raises(VectorStoreError) as failed:
        await harness.store.ensure_collection("tools", 2)
    _assert_clean(failed.value)
    assert str(failed.value) == (
        "vector store failed on collection 'tools': ResponseHandlingException"
    )


async def test_a_failure_without_a_status_names_only_the_type(harness: Harness) -> None:
    await harness.store.ensure_collection("tools", 2)
    harness.server.fail_next_with = RuntimeError(f"{KEY} {URL} {BODY}")
    with pytest.raises(VectorStoreError) as failed:
        await harness.store.search("tools", [1.0, 0.0], 1)
    _assert_clean(failed.value)
    assert (
        str(failed.value) == "vector store failed on collection 'tools': RuntimeError"
    )


async def test_a_factory_that_fails_becomes_a_store_error() -> None:
    def broken() -> FakeQdrantClient:
        raise ValueError(f"bad url {URL} {KEY}")

    store = QdrantRemoteVectorStore(URL, Secret(KEY), client_factory=broken)
    with pytest.raises(VectorStoreError) as failed:
        await store.ensure_collection("tools", 2)
    _assert_clean(failed.value)
    assert "ValueError" in str(failed.value)


async def test_a_status_code_that_is_not_an_int_is_ignored(harness: Harness) -> None:
    await harness.store.ensure_collection("tools", 2)
    odd = RuntimeError("x")
    odd.status_code = "404"  # type: ignore[attr-defined]
    harness.server.fail_next_with = odd
    with pytest.raises(VectorStoreError) as failed:
        await harness.store.search("tools", [1.0, 0.0], 1)
    assert str(failed.value).endswith("RuntimeError")


async def test_a_foreign_point_without_an_original_id_is_a_store_error(
    harness: Harness,
) -> None:
    await harness.store.ensure_collection("tools", 2)
    harness.server.collections["tools"]["points"][
        "8c6f1a0e-0000-4000-8000-000000000001"
    ] = ([1.0, 0.0], {"user_id": "u1"})
    with pytest.raises(VectorStoreError, match="KeyError"):
        await harness.store.search("tools", [1.0, 0.0], 1)


async def test_cancellation_passes_through(harness: Harness) -> None:
    await harness.store.ensure_collection("tools", 2)
    harness.server.fail_next_with = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await harness.store.search("tools", [1.0, 0.0], 1)


async def test_a_task_cancelled_mid_call_is_cancelled(harness: Harness) -> None:
    await harness.store.ensure_collection("tools", 2)
    client = harness.clients[0]
    started = asyncio.Event()

    async def hang(*args: object, **kwargs: object) -> None:
        started.set()
        await asyncio.sleep(30)

    client.query_points = hang  # type: ignore[method-assign]
    task = asyncio.ensure_future(harness.store.search("tools", [1.0, 0.0], 1))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_close_closes_the_client_once_and_is_idempotent(harness: Harness) -> None:
    await harness.store.ensure_collection("tools", 2)
    await harness.store.close()
    await harness.store.close()
    assert [client.closed_count for client in harness.clients] == [1]
    with pytest.raises(VectorStoreError, match="the vector store is closed"):
        await harness.store.search("tools", [1.0, 0.0], 1)
    with pytest.raises(VectorStoreError, match="the vector store is closed"):
        await harness.store.ensure_collection("tools", 2)
    assert len(harness.clients) == 1  # nothing reconnected


async def test_close_without_use_builds_no_client(harness: Harness) -> None:
    await harness.store.close()
    assert harness.clients == []


async def test_a_failing_close_is_reported_by_type_only(harness: Harness) -> None:
    await harness.store.ensure_collection("tools", 2)

    async def broken_close() -> None:
        raise RuntimeError(f"{KEY} {URL}")

    harness.clients[0].close = broken_close  # type: ignore[method-assign]
    with pytest.raises(VectorStoreError) as failed:
        await harness.store.close()
    _assert_clean(failed.value)
    assert str(failed.value) == "vector store did not close cleanly: RuntimeError"
    await harness.store.close()  # already closed


async def test_writes_wait_for_the_server(harness: Harness) -> None:
    await harness.store.ensure_collection("tools", 2)
    waits: list[object] = []
    client = harness.clients[0]
    original_upsert, original_delete = client.upsert, client.delete

    async def upsert(*args: object, **kwargs: object) -> None:
        waits.append(kwargs.get("wait"))
        await original_upsert(*args, **kwargs)  # type: ignore[arg-type]

    async def delete(*args: object, **kwargs: object) -> None:
        waits.append(kwargs.get("wait"))
        await original_delete(*args, **kwargs)  # type: ignore[arg-type]

    client.upsert, client.delete = upsert, delete  # type: ignore[method-assign]
    await harness.store.upsert("tools", [_point("a", [1.0, 0.0])])
    await harness.store.delete("tools", ["a"])
    assert waits == [True, True]


def test_the_module_imports_without_qdrant_client_until_first_use() -> None:
    # A fresh interpreter, so this process's imported qdrant_client cannot help.
    script = """
import asyncio, sys
sys.modules["qdrant_client"] = None
from llm_harness.storage.qdrant_remote import QdrantRemoteVectorStore
from llm_harness.storage.vectors import VectorStoreError

store = QdrantRemoteVectorStore("http://qdrant.test:6333")
try:
    asyncio.run(store.ensure_collection("tools", 2))
except VectorStoreError as error:
    print(error)
else:
    raise SystemExit("no error")
"""
    done = subprocess.run(
        [sys.executable, "-I", "-c", script],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert "uv sync --extra remote" in done.stdout


async def test_a_blocked_qdrant_client_is_a_store_error_naming_the_extra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "qdrant_client", None)
    monkeypatch.setitem(sys.modules, "qdrant_client.models", None)
    store = QdrantRemoteVectorStore(URL, Secret(KEY))
    with pytest.raises(VectorStoreError, match="uv sync --extra remote"):
        await store.ensure_collection("tools", 2)
