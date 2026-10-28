"""The vector store contract, run against every adapter, plus the qdrant-edge specifics.

`store_factory` is parametrized over adapters so a later remote adapter reuses
every contract test by adding one entry. Everything lives under `tmp_path`.
"""

import asyncio
import os
import stat
import subprocess
import sys
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest

from llm_harness.storage.qdrant_edge import QdrantEdgeVectorStore
from llm_harness.storage.vectors import (
    VectorPoint,
    VectorStore,
    VectorStoreError,
    VectorStoreLockedError,
)

LOCK_MESSAGE = "vector store is in use by another llm-harness process"
CHILD_TIMEOUT_SECONDS = 20

StoreFactory = Callable[[], VectorStore]


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "vectors"


@pytest.fixture(params=["qdrant_edge"])
def store_factory(request: pytest.FixtureRequest, root: Path) -> StoreFactory:
    factories: dict[str, StoreFactory] = {
        "qdrant_edge": lambda: QdrantEdgeVectorStore(root),
    }
    return factories[request.param]


@pytest.fixture
async def store(store_factory: StoreFactory) -> AsyncIterator[VectorStore]:
    instance = store_factory()
    yield instance
    await instance.close()


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def _point(id: str, vector: list[float], **payload: str | float | bool) -> VectorPoint:
    return VectorPoint(id=id, vector=vector, payload=payload)


async def _seeded(store: VectorStore) -> VectorStore:
    await store.ensure_collection("tools", 2)
    await store.upsert(
        "tools",
        [
            _point("near", [1.0, 0.0], user_id="ann"),
            _point("mid", [0.9, 0.1], user_id="bob"),
            _point("far", [0.0, 1.0], user_id="ann"),
        ],
    )
    return store


async def test_upsert_and_search_returns_best_score_first_with_original_ids(
    store: VectorStore,
) -> None:
    await _seeded(store)
    hits = await store.search("tools", [1.0, 0.0], 3)
    assert [hit.id for hit in hits] == ["near", "mid", "far"]
    assert [hit.score for hit in hits] == sorted((h.score for h in hits), reverse=True)
    assert hits[0].payload == {"user_id": "ann"}  # the reserved `_id` is not leaked


async def test_k_limits_the_result(store: VectorStore) -> None:
    await _seeded(store)
    assert [h.id for h in await store.search("tools", [1.0, 0.0], 2)] == ["near", "mid"]


async def test_filter_isolates_users(store: VectorStore) -> None:
    await _seeded(store)
    ann = await store.search("tools", [1.0, 0.0], 5, {"user_id": "ann"})
    bob = await store.search("tools", [1.0, 0.0], 5, {"user_id": "bob"})
    assert [h.id for h in ann] == ["near", "far"]
    assert [h.id for h in bob] == ["mid"]


async def test_filter_requires_every_key(store: VectorStore) -> None:
    await store.ensure_collection("tools", 2)
    await store.upsert(
        "tools",
        [
            _point("a", [1.0, 0.0], user_id="ann", kind="x"),
            _point("b", [1.0, 0.0], user_id="ann", kind="y"),
        ],
    )
    hits = await store.search("tools", [1.0, 0.0], 5, {"user_id": "ann", "kind": "y"})
    assert [h.id for h in hits] == ["b"]


async def test_filter_on_a_key_nobody_has_returns_nothing(store: VectorStore) -> None:
    await _seeded(store)
    assert await store.search("tools", [1.0, 0.0], 5, {"tenant": "x"}) == []


async def test_filter_matches_ints(store: VectorStore) -> None:
    await store.ensure_collection("tools", 2)
    await store.upsert(
        "tools", [_point("a", [1.0, 0.0], rank=1), _point("b", [1.0, 0.0], rank=2)]
    )
    assert [h.id for h in await store.search("tools", [1.0, 0.0], 5, {"rank": 2})] == [
        "b"
    ]


async def test_a_bool_filter_is_refused_but_a_bool_payload_round_trips(
    store: VectorStore,
) -> None:
    await store.ensure_collection("tools", 2)
    await store.upsert("tools", [_point("a", [1.0, 0.0], active=True)])
    with pytest.raises(VectorStoreError, match="'active' with a bool"):
        await store.search("tools", [1.0, 0.0], 5, {"active": True})
    assert (await store.search("tools", [1.0, 0.0], 5))[0].payload == {"active": True}


async def test_same_id_twice_keeps_one_point_with_the_latest_payload(
    store: VectorStore,
) -> None:
    await store.ensure_collection("tools", 2)
    await store.upsert("tools", [_point("a", [1.0, 0.0], version=1)])
    await store.upsert("tools", [_point("a", [1.0, 0.0], version=2)])
    hits = await store.search("tools", [1.0, 0.0], 5)
    assert [(h.id, h.payload) for h in hits] == [("a", {"version": 2})]


async def test_delete_removes_only_the_given_ids(store: VectorStore) -> None:
    await _seeded(store)
    await store.delete("tools", ["near", "never-stored"])
    assert [h.id for h in await store.search("tools", [1.0, 0.0], 5)] == ["mid", "far"]


async def test_empty_upsert_and_delete_are_no_ops(store: VectorStore) -> None:
    await _seeded(store)
    await store.upsert("tools", [])
    await store.delete("tools", [])
    assert len(await store.search("tools", [1.0, 0.0], 5)) == 3


async def test_data_survives_close_and_a_new_instance(
    store: VectorStore, store_factory: StoreFactory
) -> None:
    await _seeded(store)
    await store.close()
    reopened = store_factory()
    try:
        await reopened.ensure_collection("tools", 2)
        hits = await reopened.search("tools", [1.0, 0.0], 5, {"user_id": "ann"})
        assert [h.id for h in hits] == ["near", "far"]
    finally:
        await reopened.close()


async def test_ensure_with_the_same_shape_is_a_no_op(store: VectorStore) -> None:
    await _seeded(store)
    await store.ensure_collection("tools", 2)
    assert len(await store.search("tools", [1.0, 0.0], 5)) == 3


async def test_ensure_with_another_dimension_is_refused(
    store: VectorStore, store_factory: StoreFactory
) -> None:
    await store.ensure_collection("tools", 1024)
    message = (
        "collection 'tools' holds 1024-dimensional vectors, got 768; "
        "re-index the collection or use another name"
    )
    with pytest.raises(VectorStoreError) as held:
        await store.ensure_collection("tools", 768)
    assert str(held.value) == message
    await store.close()
    later = store_factory()  # the check also reads what an earlier run stored
    try:
        with pytest.raises(VectorStoreError) as stored:
            await later.ensure_collection("tools", 768)
        assert str(stored.value) == message
    finally:
        await later.close()


async def test_ensure_with_another_distance_is_refused(store: VectorStore) -> None:
    await store.ensure_collection("tools", 2, "cosine")
    with pytest.raises(VectorStoreError, match="'cosine' distance, got 'dot'"):
        await store.ensure_collection("tools", 2, "dot")


async def test_unknown_distance_lists_the_valid_ones(store: VectorStore) -> None:
    with pytest.raises(VectorStoreError, match="cosine, dot, euclid"):
        await store.ensure_collection("tools", 2, "manhattan")


@pytest.mark.parametrize("distance", ["cosine", "dot", "euclid"])
async def test_every_distance_ranks_the_nearer_vector_first_with_a_higher_score(
    store: VectorStore, distance: str
) -> None:
    await store.ensure_collection("tools", 2, distance)
    await store.upsert(
        "tools", [_point("same", [3.0, 4.0]), _point("other", [-3.0, -4.0])]
    )
    hits = await store.search("tools", [3.0, 4.0], 2)
    assert [h.id for h in hits] == ["same", "other"]
    assert hits[0].score > hits[1].score


@pytest.mark.parametrize("dim", [0, -1, True, 2.5, "2"])
async def test_ensure_rejects_a_bad_dimension(store: VectorStore, dim: object) -> None:
    with pytest.raises(VectorStoreError, match="positive int"):
        await store.ensure_collection("tools", dim)  # type: ignore[arg-type]


async def test_wrong_vector_length_is_refused(store: VectorStore) -> None:
    await _seeded(store)
    with pytest.raises(VectorStoreError, match="2-dimensional.*got a vector of 3"):
        await store.upsert("tools", [_point("bad", [1.0, 0.0, 0.0])])
    with pytest.raises(VectorStoreError, match="2-dimensional.*got a vector of 1"):
        await store.search("tools", [1.0], 3)
    assert len(await store.search("tools", [1.0, 0.0], 5)) == 3  # nothing was written


async def test_a_bad_point_leaves_the_whole_batch_unwritten(store: VectorStore) -> None:
    await store.ensure_collection("tools", 2)
    with pytest.raises(VectorStoreError):
        await store.upsert("tools", [_point("ok", [1.0, 0.0]), _point("bad", [1.0])])
    assert await store.search("tools", [1.0, 0.0], 5) == []


@pytest.mark.parametrize("k", [0, -1, True, 1.5])
async def test_k_must_be_a_positive_int(store: VectorStore, k: object) -> None:
    await _seeded(store)
    with pytest.raises(VectorStoreError, match="positive int"):
        await store.search("tools", [1.0, 0.0], k)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "name", ["", "Tools", "1tools", "to ols", "a/b", "..", "x" * 65]
)
async def test_invalid_collection_names_are_refused(
    store: VectorStore, name: str
) -> None:
    with pytest.raises(VectorStoreError, match="collection name"):
        await store.ensure_collection(name, 2)
    with pytest.raises(VectorStoreError, match="collection name"):
        await store.search(name, [1.0, 0.0], 1)


async def test_the_longest_valid_name_works(store: VectorStore) -> None:
    await store.ensure_collection("a" + "b" * 63, 2)


async def test_the_reserved_id_payload_key_is_refused(store: VectorStore) -> None:
    await store.ensure_collection("tools", 2)
    with pytest.raises(VectorStoreError, match="'_id' is reserved"):
        await store.upsert("tools", [_point("a", [1.0, 0.0], _id="x")])


async def test_an_unknown_collection_says_to_ensure_it_first(
    store: VectorStore,
) -> None:
    for call in (
        store.upsert("nope", [_point("a", [1.0, 0.0])]),
        store.search("nope", [1.0, 0.0], 1),
        store.delete("nope", ["a"]),
    ):
        with pytest.raises(VectorStoreError, match="call ensure_collection first"):
            await call


async def test_close_is_idempotent_and_later_use_is_refused(store: VectorStore) -> None:
    await _seeded(store)
    await store.close()
    await store.close()
    with pytest.raises(VectorStoreError, match="closed"):
        await store.search("tools", [1.0, 0.0], 1)
    with pytest.raises(VectorStoreError, match="closed"):
        await store.ensure_collection("tools", 2)


async def test_close_without_any_collection_creates_nothing(
    store: VectorStore, root: Path
) -> None:
    await store.close()
    assert not root.exists()


def test_construction_creates_nothing(root: Path) -> None:
    QdrantEdgeVectorStore(root)
    assert not root.exists()


async def test_ensure_creates_private_directories_and_metadata(root: Path) -> None:
    previous = os.umask(0)  # a permissive umask would show a missing explicit chmod
    store = QdrantEdgeVectorStore(root)
    try:
        await store.ensure_collection("tools", 2)
        await store.upsert("tools", [_point("a", [1.0, 0.0])])
    finally:
        os.umask(previous)
        await store.close()
    assert _mode(root) == 0o700
    assert _mode(root / "tools") == 0o700
    assert _mode(root / "tools" / "shard") == 0o700
    assert _mode(root / "tools" / "meta.json") == 0o600


async def test_engine_errors_never_show_the_data_path(root: Path) -> None:
    class BrokenShard:
        def update(self, operation: object) -> None:
            raise RuntimeError(f"disk failure at {root}/tools/shard")

        def close(self) -> None:
            pass

        flush = close

    store = QdrantEdgeVectorStore(root)
    await store.ensure_collection("tools", 2)
    store._collections[
        "tools"
    ].shard = BrokenShard()  # the engine has no way to fail on demand
    with pytest.raises(VectorStoreError) as failed:
        await store.upsert("tools", [_point("a", [1.0, 0.0])])
    await store.close()
    assert (
        str(failed.value) == "vector store failed on collection 'tools': RuntimeError"
    )


async def test_cancellation_is_not_swallowed(root: Path) -> None:
    store = QdrantEdgeVectorStore(root)
    await store.ensure_collection("tools", 2)
    task = asyncio.ensure_future(store.search("tools", [1.0, 0.0], 1))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await store.close()


_CHILD = """
import asyncio, sys
from pathlib import Path
from llm_harness.storage.qdrant_edge import QdrantEdgeVectorStore

async def main():
    store = QdrantEdgeVectorStore(Path(sys.argv[1]))
    await store.ensure_collection("tools", 2)
    print("ready", flush=True)
    # The parent closes our stdin to release the lock; 25 s is the safety net.
    await asyncio.wait_for(asyncio.to_thread(sys.stdin.read), 25)
    await store.close()

asyncio.run(main())
"""


async def test_a_second_process_gets_a_locked_error_until_the_first_exits(
    root: Path,
) -> None:
    child = subprocess.Popen(  # noqa: ASYNC220 - spawn is instant; every wait is bounded
        [sys.executable, "-c", _CHILD, str(root)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    parent = QdrantEdgeVectorStore(root)
    try:
        assert child.stdout is not None
        ready = _read_line_within(child, CHILD_TIMEOUT_SECONDS)
        assert ready == "ready", (
            child.stderr.read() if child.poll() is not None else ready
        )
        with pytest.raises(VectorStoreLockedError) as locked:
            await parent.ensure_collection("tools", 2)
        assert str(locked.value) == LOCK_MESSAGE
        assert isinstance(locked.value, VectorStoreError)
        assert child.stdin is not None
        child.stdin.close()
        child.wait(timeout=CHILD_TIMEOUT_SECONDS)
        await parent.ensure_collection("tools", 2)
        await parent.upsert("tools", [_point("a", [1.0, 0.0])])
        assert [h.id for h in await parent.search("tools", [1.0, 0.0], 1)] == ["a"]
    finally:
        child.kill()
        child.wait(timeout=CHILD_TIMEOUT_SECONDS)
        for stream in (child.stdin, child.stdout, child.stderr):
            if stream is not None:
                stream.close()
        await parent.close()


def _read_line_within(child: subprocess.Popen, seconds: float) -> str:
    """The child's first stdout line, or fail after `seconds` (a bare readline can block forever)."""
    import selectors

    assert child.stdout is not None
    with selectors.DefaultSelector() as selector:
        selector.register(child.stdout, selectors.EVENT_READ)
        if not selector.select(seconds):
            pytest.fail(f"the child process printed nothing within {seconds} s")
    return child.stdout.readline().strip()
