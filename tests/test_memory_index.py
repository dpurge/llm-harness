"""`fuse_ranks` and the shared fakes `ConceptEmbedder` and `FakeVectorStore`."""

import math

import pytest

from llm_harness.embeddings import EmbeddingError
from llm_harness.memory_index import fuse_ranks
from llm_harness.storage.vectors import (
    VectorPoint,
    VectorStoreError,
    VectorStoreLockedError,
)
from tests.fakes import ConceptEmbedder, FakeVectorStore

QUERY = "what can I cook for her, she has a diet"
ALLERGY = "Partner is allergic to peanuts allergy partner"
TRAVEL = "Booked a flight and hotel for the trip budget"


def cosine(left: list[float], right: list[float]) -> float:
    return sum(a * b for a, b in zip(left, right, strict=True))  # both unit length


# --- fuse_ranks -------------------------------------------------------------


def test_clean_merge_interleaves_by_rank() -> None:
    assert fuse_ranks([1, 2], [3, 4], 10) == [1, 3, 2, 4]


def test_id_in_both_lists_beats_id_in_one() -> None:
    assert fuse_ranks([1, 2], [3, 2], 10)[0] == 2


def test_keyword_first_stays_first_when_vector_disagrees() -> None:
    assert fuse_ranks([1, 2, 3], [4, 5, 6], 10)[0] == 1
    assert fuse_ranks([1, 2], [2, 1], 10)[0] == 1


def test_tie_goes_to_keyword_rank() -> None:
    assert fuse_ranks([1], [2], 10) == [1, 2]
    assert fuse_ranks([2], [1], 10) == [2, 1]


def test_tie_with_both_present_uses_keyword_then_vector_rank() -> None:
    # 1 and 2 swap ranks, so their scores are equal; keyword rank decides.
    assert fuse_ranks([1, 2], [2, 1], 10) == [1, 2]


def test_duplicates_count_once_at_first_position() -> None:
    assert fuse_ranks([1, 1, 2], [], 10) == [1, 2]
    assert fuse_ranks([1, 2, 1], [2, 2], 10) == fuse_ranks([1, 2], [2], 10)


def test_overlap_gives_unique_ids() -> None:
    result = fuse_ranks([1, 2, 3], [3, 2, 4], 10)
    assert len(result) == len(set(result)) == 4


def test_cut_to_k() -> None:
    assert fuse_ranks([1, 2, 3], [4, 5, 6], 2) == [1, 4]


@pytest.mark.parametrize("k", [0, -1])
def test_k_below_one_is_empty(k: int) -> None:
    assert fuse_ranks([1], [2], k) == []


def test_empty_inputs() -> None:
    assert fuse_ranks([], [], 5) == []
    assert fuse_ranks([7, 8], [], 5) == [7, 8]
    assert fuse_ranks([], [7, 8], 5) == [7, 8]


def test_score_is_reciprocal_rank_with_offset_60() -> None:
    # 9 is third in keywords and first in vectors: 1/63 + 1/61 beats 1/61 alone.
    assert fuse_ranks([1, 2, 9], [9], 10)[0] == 9


# --- ConceptEmbedder --------------------------------------------------------


async def test_concept_embedder_same_text_same_vector() -> None:
    embedder = ConceptEmbedder()
    first, second = await embedder.embed("m", [QUERY, QUERY], None)
    assert first == second
    assert len(first) == 4
    assert math.isclose(sum(v * v for v in first), 1.0)


async def test_concept_embedder_no_zero_vector() -> None:
    [vector] = await ConceptEmbedder().embed("m", ["zzz"], None)
    assert any(vector)


async def test_concept_embedder_sample_is_closer_to_allergy_than_travel() -> None:
    query, allergy, travel = await ConceptEmbedder().embed(
        "m", [QUERY, ALLERGY, TRAVEL], None
    )
    assert cosine(query, allergy) > cosine(query, travel)


async def test_concept_embedder_records_calls_and_closes() -> None:
    embedder = ConceptEmbedder()
    await embedder.embed("model-x", ["a", "b"], 4)
    await embedder.aclose()
    assert embedder.calls == [("model-x", ["a", "b"], 4)]
    assert embedder.closed


async def test_concept_embedder_raises_scripted_error() -> None:
    error = EmbeddingError("connection", "down")
    embedder = ConceptEmbedder(raises=error)
    with pytest.raises(EmbeddingError) as raised:
        await embedder.embed("m", ["a"], None)
    assert raised.value is error
    assert len(embedder.calls) == 1


# --- FakeVectorStore --------------------------------------------------------


def point(point_id: str, vector: list[float], **payload: str | int) -> VectorPoint:
    return VectorPoint(point_id, vector, payload)


async def test_store_requires_ensure_collection_first() -> None:
    store = FakeVectorStore()
    with pytest.raises(VectorStoreError, match="ensure_collection first"):
        await store.upsert("mem", [point("1", [1.0, 0.0])])
    with pytest.raises(VectorStoreError, match="ensure_collection first"):
        await store.search("mem", [1.0, 0.0], 3)
    with pytest.raises(VectorStoreError, match="ensure_collection first"):
        await store.delete("mem", ["1"])


async def test_store_rejects_bad_collection_name() -> None:
    with pytest.raises(VectorStoreError, match="collection name"):
        await FakeVectorStore().ensure_collection("Bad-Name", 2)


async def test_store_ensure_is_idempotent_but_checks_dim_and_distance() -> None:
    store = FakeVectorStore()
    await store.ensure_collection("mem", 2)
    await store.ensure_collection("mem", 2)
    with pytest.raises(VectorStoreError, match="2-dimensional"):
        await store.ensure_collection("mem", 3)
    with pytest.raises(VectorStoreError, match="distance"):
        await store.ensure_collection("mem", 2, "dot")


async def test_store_refuses_bool_filter() -> None:
    store = FakeVectorStore()
    await store.ensure_collection("mem", 2)
    with pytest.raises(VectorStoreError, match="bool"):
        await store.search("mem", [1.0, 0.0], 3, {"flag": True})


async def test_store_refuses_reserved_id_payload_key() -> None:
    store = FakeVectorStore()
    await store.ensure_collection("mem", 2)
    with pytest.raises(VectorStoreError, match="reserved"):
        await store.upsert("mem", [VectorPoint("1", [1.0, 0.0], {"_id": "x"})])


async def test_store_refuses_wrong_vector_length() -> None:
    store = FakeVectorStore()
    await store.ensure_collection("mem", 2)
    with pytest.raises(VectorStoreError, match="2-dimensional"):
        await store.upsert("mem", [point("1", [1.0, 0.0, 0.0])])


async def test_store_user_filter_isolates_users() -> None:
    store = FakeVectorStore()
    await store.ensure_collection("mem", 2)
    await store.upsert(
        "mem",
        [point("1", [1.0, 0.0], user_id="a"), point("2", [1.0, 0.0], user_id="b")],
    )
    assert [
        h.id for h in await store.search("mem", [1.0, 0.0], 5, {"user_id": "a"})
    ] == ["1"]
    assert [
        h.id for h in await store.search("mem", [1.0, 0.0], 5, {"user_id": "b"})
    ] == ["2"]
    assert await store.search("mem", [1.0, 0.0], 5, {"user_id": "c"}) == []


async def test_store_orders_by_score_and_cuts_to_k() -> None:
    store = FakeVectorStore()
    await store.ensure_collection("mem", 2)
    await store.upsert(
        "mem",
        [
            point("far", [0.0, 1.0]),
            point("near", [1.0, 0.0]),
            point("mid", [1.0, 1.0]),
        ],
    )
    hits = await store.search("mem", [1.0, 0.0], 2)
    assert [h.id for h in hits] == ["near", "mid"]
    assert hits[0].score > hits[1].score


async def test_store_upsert_replaces_and_delete_removes() -> None:
    store = FakeVectorStore()
    await store.ensure_collection("mem", 2)
    await store.upsert("mem", [point("1", [1.0, 0.0], user_id="a")])
    await store.upsert("mem", [point("1", [0.0, 1.0], user_id="a")])
    assert len(await store.search("mem", [0.0, 1.0], 5)) == 1
    await store.delete("mem", ["1", "missing"])
    assert await store.search("mem", [0.0, 1.0], 5) == []


async def test_store_records_every_call() -> None:
    store = FakeVectorStore()
    await store.ensure_collection("mem", 2)
    await store.search("mem", [1.0, 0.0], 3, {"user_id": "a"})
    await store.close()
    assert [name for name, _ in store.calls] == ["ensure_collection", "search", "close"]
    assert store.calls[1][1] == ("mem", (1.0, 0.0), 3, {"user_id": "a"})
    assert store.closed


@pytest.mark.parametrize("error", [VectorStoreLockedError(), VectorStoreError("boom")])
async def test_store_scripted_failure_raises_on_named_method(error: Exception) -> None:
    store = FakeVectorStore(fail_on={"search": error})
    await store.ensure_collection("mem", 2)
    with pytest.raises(type(error)):
        await store.search("mem", [1.0, 0.0], 3)
    await store.upsert("mem", [point("1", [1.0, 0.0])])  # other methods still work
