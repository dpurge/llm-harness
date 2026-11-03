"""`IndexedMemoryStore`: index writes, the stale-marker triggers and the failure policy.

Real SQLite on `tmp_path`, the concept embedder behind the real `Embedder`, and the
fake vector store that mirrors the adapter's rules. No network.
"""

import re
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest

from llm_harness import embeddings
from llm_harness.config import ConfigError, ModelConfig
from llm_harness.embeddings import Embedder, EmbeddingError
from llm_harness.memory import MEMORY_MIGRATIONS, MemoryStore, MemoryValidationError
from llm_harness.memory_index import IndexedMemoryStore, collection_name
from llm_harness.storage.relational import SqliteRelationalStore
from llm_harness.storage.vectors import VectorStoreError, VectorStoreLockedError
from tests.fakes import ConceptEmbedder, FakeVectorStore, HangingEmbedder

NOW = datetime(2026, 11, 2, 9, 30, 15, tzinfo=UTC)
ADAPTER_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")


def model(model_id: str = "concepts") -> ModelConfig:
    return ModelConfig(
        id=model_id,
        name=model_id,
        provider="local",
        model=model_id,
        tags=["embedding"],
        priority=1,
    )


class Rig:
    def __init__(self, db: Path, **fail_on: Exception) -> None:
        self.db = db
        self.provider = ConceptEmbedder()
        self.vectors = FakeVectorStore(fail_on=fail_on)
        self.factory_calls = 0
        self.store = IndexedMemoryStore(
            SqliteRelationalStore(db),
            self.vectors,
            self.factory,
            lambda: NOW,
        )

    def factory(self) -> Embedder:
        self.factory_calls += 1
        return Embedder(self.provider, model())

    def sql(self, statement: str, params: tuple = ()) -> list[tuple]:
        with closing(sqlite3.connect(self.db)) as conn:
            rows = conn.execute(statement, params).fetchall()
            conn.commit()
            return rows

    def markers(self) -> list[tuple]:
        return self.sql("SELECT collection, memory_id FROM memory_vectors")

    def points(self) -> dict:
        return {
            pid: point
            for points in self.vectors._points.values()
            for pid, point in points.items()
        }

    def calls(self, method: str) -> list[tuple]:
        return [call for call in self.vectors.calls if call[0] == method]


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "data" / "memory.db"


@pytest.fixture
def rig(db: Path) -> Rig:
    return Rig(db)


COLLECTION = collection_name("concepts")


async def test_save_writes_one_vector_and_one_marker(rig: Rig) -> None:
    memory = await rig.store.save(
        "u1", "Partner is allergic to peanuts", ["allergy", "diet"]
    )
    point = rig.points()[str(memory.id)]
    assert point.payload == {"user_id": "u1"}
    assert list(point.vector) == ConceptEmbedder.vector_for(
        "Partner is allergic to peanuts [allergy, diet]"
    )
    assert rig.provider.calls[0][1] == [
        "Partner is allergic to peanuts [allergy, diet]"
    ]
    assert len(rig.points()) == 1
    assert rig.markers() == [(COLLECTION, memory.id)]
    assert rig.store.index_off_reason is None
    assert rig.store.take_notice() is None


async def test_update_clears_the_marker_then_reembeds_and_remarks(rig: Rig) -> None:
    memory = await rig.store.save("u1", "Likes tea", ["tea"])
    before = list(rig.points()[str(memory.id)].vector)
    updated = await rig.store.update("u1", memory.id, text="Wants a trip budget")
    assert updated is not None
    assert list(rig.points()[str(memory.id)].vector) != before
    assert rig.provider.calls[-1][1] == ["Wants a trip budget [tea]"]
    assert rig.markers() == [(COLLECTION, memory.id)]
    assert len(rig.calls("upsert")) == 2


async def test_update_of_a_missing_id_changes_nothing_in_the_index(rig: Rig) -> None:
    await rig.store.save("u1", "Likes tea", ["tea"])
    embeds, upserts = len(rig.provider.calls), len(rig.calls("upsert"))
    assert await rig.store.update("u1", 999, text="x") is None
    assert (len(rig.provider.calls), len(rig.calls("upsert"))) == (embeds, upserts)
    assert len(rig.markers()) == 1


async def test_forget_deletes_the_vector_and_the_marker(rig: Rig) -> None:
    memory = await rig.store.save("u1", "Likes tea", ["tea"])
    assert await rig.store.forget("u1", memory.id) is True
    assert rig.points() == {}
    assert rig.markers() == []
    assert rig.calls("delete") == [("delete", (COLLECTION, (str(memory.id),)))]


async def test_forget_of_a_missing_id_does_not_touch_the_index(rig: Rig) -> None:
    await rig.store.save("u1", "Likes tea", ["tea"])
    assert await rig.store.forget("u1", 999) is False
    assert rig.calls("delete") == []


async def test_forget_in_a_new_process_deletes_the_old_vector(db: Path) -> None:
    first = Rig(db)
    memory = await first.store.save("u1", "Likes tea", ["tea"])
    second = Rig(db)
    second.store = IndexedMemoryStore(
        SqliteRelationalStore(db), first.vectors, second.factory, lambda: NOW
    )
    assert await second.store.forget("u1", memory.id) is True
    assert first.points() == {}
    assert second.store.index_off_reason is None


async def test_forget_with_nothing_ever_indexed_makes_no_vector_call(rig: Rig) -> None:
    memory = await MemoryStore(SqliteRelationalStore(rig.db)).save(
        "u1", "Likes tea", ["tea"]
    )
    assert await rig.store.forget("u1", memory.id) is True
    assert rig.vectors.calls == []


async def test_a_vector_failure_keeps_the_memory_and_turns_the_index_off(
    db: Path,
) -> None:
    rig = Rig(db, upsert=VectorStoreError("disk full"))
    memory = await rig.store.save("u1", "Likes tea", ["tea"])
    assert memory.text == "Likes tea"
    assert (await rig.store.get("u1", memory.id)) == memory
    assert rig.markers() == []
    assert rig.store.index_off_reason == "VectorStoreError: disk full"
    assert rig.store.take_notice() == (
        "memory vector search is off: VectorStoreError: disk full; keyword search only"
    )
    assert rig.store.take_notice() is None


async def test_later_writes_with_the_index_off_make_no_embed_call(db: Path) -> None:
    rig = Rig(db, upsert=VectorStoreError("disk full"))
    await rig.store.save("u1", "Likes tea", ["tea"])
    embeds = len(rig.provider.calls)
    second = await rig.store.save("u1", "Likes jam", ["jam"])
    await rig.store.update("u1", second.id, text="Likes honey")
    await rig.store.forget("u1", second.id)
    assert len(rig.provider.calls) == embeds
    assert rig.markers() == []
    assert rig.store.take_notice() is not None  # the first failure, still unread
    assert rig.store.take_notice() is None


async def test_an_embedding_error_turns_the_index_off(db: Path) -> None:
    rig = Rig(db)
    rig.provider.raises = EmbeddingError("unknown", "provider down")
    memory = await rig.store.save("u1", "Likes tea", ["tea"])
    assert memory.id == 1 and rig.markers() == []
    assert rig.store.take_notice() == (
        "memory vector search is off: EmbeddingError: provider down; keyword search only"
    )
    assert rig.store.take_notice() is None


async def test_an_embedder_that_never_answers_turns_the_index_off(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(embeddings, "EMBED_TIMEOUT_SECONDS", 0.05)
    rig = Rig(db)
    hanging = HangingEmbedder()
    rig.provider = hanging  # type: ignore[assignment]
    memory = await rig.store.save("u1", "Likes tea", ["tea"])
    assert memory.text == "Likes tea" and rig.markers() == []
    assert rig.store.index_off_reason is not None
    notice = rig.store.take_notice()
    assert notice is not None and "\n" not in notice
    assert "did not answer within 0.05 seconds" in notice
    assert rig.store.take_notice() is None
    await rig.store.save("u1", "Likes jam", ["jam"])
    assert len(hanging.calls) == 1
    assert [m.text for m in await rig.store.search("u1", "tea", 5)] == ["Likes tea"]


async def test_a_failing_embedder_factory_turns_the_index_off(db: Path) -> None:
    rig = Rig(db)

    def broken() -> Embedder:
        raise ConfigError("no embedding model configured")

    rig.store = IndexedMemoryStore(
        SqliteRelationalStore(db), rig.vectors, broken, lambda: NOW
    )
    memory = await rig.store.save("u1", "Likes tea", ["tea"])
    assert memory.text == "Likes tea" and rig.markers() == []
    assert rig.store.take_notice() == (
        "memory vector search is off: ConfigError: no embedding model configured; "
        "keyword search only"
    )
    assert rig.store.take_notice() is None
    assert rig.vectors.calls == []


async def test_a_locked_vector_store_turns_the_index_off(db: Path) -> None:
    rig = Rig(db, ensure_collection=VectorStoreLockedError())
    memory = await rig.store.save("u1", "Likes tea", ["tea"])
    assert memory.text == "Likes tea" and rig.markers() == []
    notice = rig.store.take_notice()
    assert notice is not None and "VectorStoreLockedError" in notice
    assert rig.store.take_notice() is None


async def test_a_model_or_dimension_mismatch_turns_the_index_off(rig: Rig) -> None:
    await MemoryStore(SqliteRelationalStore(rig.db)).save("u0", "seed", ["seed"])
    rig.sql(
        "INSERT INTO memory_vector_meta (collection, model_id, dim) VALUES (?, ?, ?)",
        (COLLECTION, "concepts", ConceptEmbedder.SIZE + 1),
    )
    memory = await rig.store.save("u1", "Likes tea", ["tea"])
    assert memory.text == "Likes tea"
    assert rig.markers() == []
    assert rig.points() == {}
    assert rig.store.index_off_reason is not None
    assert "embedding model or dimension changed for this collection" in (
        rig.store.index_off_reason
    )


async def test_a_matching_meta_row_is_accepted_and_one_is_recorded(rig: Rig) -> None:
    await rig.store.save("u1", "Likes tea", ["tea"])
    await rig.store.save("u1", "Likes jam", ["jam"])
    assert rig.sql("SELECT collection, model_id, dim FROM memory_vector_meta") == [
        (COLLECTION, "concepts", ConceptEmbedder.SIZE)
    ]
    assert len(rig.calls("ensure_collection")) == 1


def test_the_collection_name_fits_the_adapter_rule_and_depends_on_the_model() -> None:
    first, second = collection_name("bge-m3/local:v1"), collection_name("other")
    assert ADAPTER_NAME.fullmatch(first) and ADAPTER_NAME.fullmatch(second)
    assert first != second
    assert collection_name("other") == second


async def test_a_secret_is_refused_and_never_embedded(rig: Rig) -> None:
    with pytest.raises(MemoryValidationError):
        await rig.store.save("u1", "key sk-abcdefgh12345678", ["key"])
    assert rig.provider.calls == []
    assert rig.vectors.calls == []
    assert rig.factory_calls == 0


async def test_each_point_carries_only_its_own_user(rig: Rig) -> None:
    first = await rig.store.save("alice", "Likes tea", ["tea"])
    second = await rig.store.save("bob", "Likes jam", ["jam"])
    points = rig.points()
    assert points[str(first.id)].payload == {"user_id": "alice"}
    assert points[str(second.id)].payload == {"user_id": "bob"}


async def test_a_version_1_database_migrates_to_2_and_keeps_its_data(db: Path) -> None:
    relational = SqliteRelationalStore(db)
    await relational.migrate("memory", MEMORY_MIGRATIONS[:1])
    await relational.execute(
        "INSERT INTO memories (user_id, text, keywords, created_at, updated_at) "
        "VALUES ('u1', 'Likes tea', 'tea', 'then', 'then')"
    )
    store = MemoryStore(relational)
    assert [m.text for m in await store.list("u1")] == ["Likes tea"]
    rig = Rig(db)
    assert rig.sql(
        "SELECT version FROM schema_versions WHERE namespace = 'memory'"
    ) == [(2,)]
    assert rig.sql("SELECT text FROM memories") == [("Likes tea",)]
    names = {
        r[0] for r in rig.sql("SELECT name FROM sqlite_master WHERE type = 'trigger'")
    }
    assert {"memories_ai", "memories_ad", "memories_au"} <= names
    assert {"memories_vectors_stale", "memories_vectors_gone"} <= names
    assert rig.sql("SELECT count(*) FROM memory_vectors") == [(0,)]


async def test_the_triggers_fire_on_direct_sql_and_ignore_updated_at(rig: Rig) -> None:
    memory = await rig.store.save("u1", "Likes tea", ["tea"])
    rig.sql("UPDATE memories SET updated_at = 'later' WHERE id = ?", (memory.id,))
    assert len(rig.markers()) == 1
    rig.sql("UPDATE memories SET text = 'Likes jam' WHERE id = ?", (memory.id,))
    assert rig.markers() == []
    rig.sql("INSERT INTO memory_vectors VALUES (?, ?)", (COLLECTION, memory.id))
    rig.sql("UPDATE memories SET keywords = 'jam' WHERE id = ?", (memory.id,))
    assert rig.markers() == []
    rig.sql("INSERT INTO memory_vectors VALUES (?, ?)", (COLLECTION, memory.id))
    rig.sql("DELETE FROM memories WHERE id = ?", (memory.id,))
    assert rig.markers() == []


async def test_fts_still_follows_edits_next_to_the_new_triggers(rig: Rig) -> None:
    memory = await rig.store.save("u1", "Likes tea", ["tea"])
    await rig.store.update("u1", memory.id, text="Wants a hotel")
    keyword_only = MemoryStore(SqliteRelationalStore(rig.db))  # FTS, not the vectors
    found = await keyword_only.search("u1", "hotel")
    assert [m.id for m in found] == [memory.id]
    assert await keyword_only.search("u1", "likes") == []


# --- hybrid search -------------------------------------------------------------

DIET_QUERY = "what can I cook for her, she has a diet"


async def seed_sample(store: MemoryStore, user: str = "u1") -> dict[str, int]:
    peanuts = await store.save(
        user, "Partner is allergic to peanuts", ["allergy", "partner"]
    )
    trip = await store.save(
        user, "Booked a flight and hotel for the trip", ["trip", "budget"]
    )
    tea = await store.save(user, "Likes tea", ["tea"])
    return {"peanuts": peanuts.id, "trip": trip.id, "tea": tea.id}


def vector_searches(rig: Rig) -> list[tuple]:
    return rig.calls("search")


async def test_the_pinned_sample_is_found_by_meaning_only_in_the_indexed_store(
    rig: Rig,
) -> None:
    ids = await seed_sample(rig.store)
    base = MemoryStore(SqliteRelationalStore(rig.db), lambda: NOW)
    assert await base.search("u1", DIET_QUERY) == []
    found = await rig.store.search("u1", DIET_QUERY, 3)
    assert found and found[0].id == ids["peanuts"]


async def test_an_exact_keyword_hit_stays_first_when_the_vectors_disagree(
    rig: Rig,
) -> None:
    ids = await seed_sample(rig.store)
    # keyword: only "Likes tea"; vector: the peanut note (food axis) ranks first.
    query_vector = ConceptEmbedder.vector_for("tea diet")
    nearest = max(
        rig.points().values(),
        key=lambda p: sum(a * b for a, b in zip(query_vector, p.vector, strict=True)),
    )
    assert nearest.id == str(ids["peanuts"])
    found = await rig.store.search("u1", "tea diet", 3)
    assert found[0].id == ids["tea"]
    assert ids["peanuts"] in [m.id for m in found]


async def test_every_vector_search_carries_the_user_filter(rig: Rig) -> None:
    await seed_sample(rig.store, "alice")
    await rig.store.search("alice", DIET_QUERY)
    await rig.store.search("alice", "tea")
    searches = vector_searches(rig)
    assert len(searches) == 2
    assert all(call[1][3] == {"user_id": "alice"} for call in searches)
    assert all(call[1][2] == 10 for call in searches)


async def test_one_user_never_sees_the_notes_of_another(rig: Rig) -> None:
    await seed_sample(rig.store, "alice")
    await rig.store.save("bob", "Bob is allergic to shellfish", ["allergy"])
    found = await rig.store.search("alice", "allergic diet allergy", 10)
    assert found and all(m.user_id == "alice" for m in found)
    found_bob = await rig.store.search("bob", "allergic diet allergy", 10)
    assert [m.text for m in found_bob] == ["Bob is allergic to shellfish"]


async def test_a_vector_hit_of_another_user_is_dropped_even_without_the_filter(
    db: Path,
) -> None:
    class IgnoresFilter(FakeVectorStore):
        async def search(self, collection, vector, k, filter=None):
            return await super().search(collection, vector, k, None)

    rig = Rig(db)
    rig.vectors = IgnoresFilter()
    rig.store = IndexedMemoryStore(
        SqliteRelationalStore(db), rig.vectors, rig.factory, lambda: NOW
    )
    await rig.store.save("bob", "Partner is allergic to peanuts", ["allergy"])
    mine = await rig.store.save("alice", "Likes tea", ["tea"])
    found = await rig.store.search("alice", DIET_QUERY, 5)
    assert [m.id for m in found] == [mine.id]
    assert len(vector_searches(rig)) == 1


async def test_an_orphan_vector_is_dropped_and_the_rest_returned(rig: Rig) -> None:
    ids = await seed_sample(rig.store)
    rig.sql("DELETE FROM memories WHERE id = ?", (ids["peanuts"],))
    assert str(ids["peanuts"]) in rig.points()
    found = await rig.store.search("u1", DIET_QUERY, 5)
    assert ids["peanuts"] not in [m.id for m in found]
    assert {m.id for m in found} == {ids["trip"], ids["tea"]}


async def test_k_is_respected_and_clamped_to_ten(rig: Rig) -> None:
    for number in range(12):
        await rig.store.save("u1", f"Note {number} about tea", ["tea"])
    assert len(await rig.store.search("u1", "tea", 1)) == 1
    assert len(await rig.store.search("u1", "tea", 50)) == 10
    assert len(await rig.store.search("u1", "tea", 0)) == 1


async def test_a_vector_failure_gives_keywords_only_and_one_notice(db: Path) -> None:
    rig = Rig(db)
    await seed_sample(rig.store)
    rig.vectors.fail_on["search"] = VectorStoreError("boom")
    found = await rig.store.search("u1", "tea diet", 5)
    assert [m.text for m in found] == ["Likes tea"]
    assert rig.store.take_notice() == (
        "memory vector search is off: VectorStoreError: boom; keyword search only"
    )
    assert rig.store.take_notice() is None
    embeds, searches = len(rig.provider.calls), len(vector_searches(rig))
    assert [m.text for m in await rig.store.search("u1", "tea", 5)] == ["Likes tea"]
    assert (len(rig.provider.calls), len(vector_searches(rig))) == (embeds, searches)
    assert rig.store.take_notice() is None


async def test_an_embedding_failure_on_search_gives_keywords_only(rig: Rig) -> None:
    await seed_sample(rig.store)
    rig.provider.raises = EmbeddingError("unknown", "provider down")
    found = await rig.store.search("u1", "tea diet", 5)
    assert [m.text for m in found] == ["Likes tea"]
    assert rig.store.take_notice() == (
        "memory vector search is off: EmbeddingError: provider down; keyword search only"
    )
    assert rig.store.take_notice() is None
    embeds = len(rig.provider.calls)
    await rig.store.search("u1", "tea", 5)
    assert len(rig.provider.calls) == embeds


async def test_a_locked_store_on_search_gives_keywords_only(db: Path) -> None:
    rig = Rig(db, search=VectorStoreLockedError())
    await seed_sample(rig.store)
    found = await rig.store.search("u1", "tea", 5)
    assert [m.text for m in found] == ["Likes tea"]
    notice = rig.store.take_notice()
    assert notice is not None and "VectorStoreLockedError" in notice


async def test_with_the_index_already_off_search_makes_no_embed_or_vector_call(
    db: Path,
) -> None:
    rig = Rig(db, upsert=VectorStoreError("disk full"))
    await rig.store.save("u1", "Likes tea", ["tea"])
    assert rig.store.index_off_reason is not None
    embeds, calls = len(rig.provider.calls), len(rig.vectors.calls)
    assert [m.text for m in await rig.store.search("u1", "tea", 5)] == ["Likes tea"]
    assert (len(rig.provider.calls), len(rig.vectors.calls)) == (embeds, calls)


async def test_a_blank_query_returns_nothing_and_embeds_nothing(rig: Rig) -> None:
    await seed_sample(rig.store)
    embeds = len(rig.provider.calls)
    assert await rig.store.search("u1", "   \n", 5) == []
    assert await rig.store.search("u1", "", 5) == []
    assert len(rig.provider.calls) == embeds
    assert vector_searches(rig) == []


async def test_a_query_of_stop_words_still_reaches_the_vector_side(rig: Rig) -> None:
    ids = await seed_sample(rig.store)
    assert (
        await MemoryStore(SqliteRelationalStore(rig.db)).search("u1", "what can I do")
        == []
    )
    found = await rig.store.search("u1", "what can I do", 3)
    assert len(vector_searches(rig)) == 1
    assert {m.id for m in found} <= set(ids.values()) and found


async def test_the_embedded_query_is_capped(rig: Rig) -> None:
    from llm_harness.memory_index import MAX_QUERY_EMBED_CHARS

    await seed_sample(rig.store)
    await rig.store.search("u1", "tea " * 1000, 3)
    assert len(rig.provider.calls[-1][1][0]) == MAX_QUERY_EMBED_CHARS


async def test_search_does_not_load_keyword_hits_again(rig: Rig) -> None:
    ids = await seed_sample(rig.store)
    found = await rig.store.search("u1", "trip", 3)
    assert found[0].id == ids["trip"]
    assert len(rig.calls("ensure_collection")) == 1
