"""Repair of unindexed memories: `reindex`, the lazy repair on search, `/memory reindex`.

Real SQLite on `tmp_path`, the concept embedder behind the real `Embedder`, the fake
vector store. No network.
"""

import asyncio
from pathlib import Path

import pytest

from llm_harness.embeddings import Embedder, EmbeddingError
from llm_harness.memory import MemoryStore
from llm_harness.memory_command import (
    USAGE,
    VECTORS_OFF,
    MemoryCommand,
    handle_memory_text,
    parse_memory_command,
    run_memory_command,
)
from llm_harness.memory_index import (
    LAZY_REPAIR_LIMIT,
    REINDEX_BATCH_SIZE,
    IndexedMemoryStore,
    collection_name,
)
from llm_harness.memory_tools import recall_store
from llm_harness.storage import Storage
from llm_harness.storage.vectors import VectorStoreError, VectorStoreLockedError
from tests.fakes import ConceptEmbedder
from tests.test_memory_index_store import Rig, model
from tests.test_memory_vectors_wiring import (  # noqa: F401  (storage is a fixture)
    failing_factory,
    storage,
    vector_registry,
    working_factory,
)

COLLECTION = collection_name("concepts")


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "data" / "memory.db"


async def seed_unindexed(db: Path, count: int, user_id: str = "u1") -> None:
    """Rows written without any index, as if saved before the flag was on."""
    plain = MemoryStore(Rig(db).store._relational)
    for number in range(count):
        await plain.save(user_id, f"note {number}", [f"kw{number}"])


def embeds(rig: Rig) -> int:
    return len(rig.provider.calls)


def indexed_ids(rig: Rig) -> list[int]:
    return sorted(memory_id for _, memory_id in rig.markers())


async def test_reindex_repairs_rows_left_unindexed_by_a_failed_write(db: Path) -> None:
    broken = Rig(db, upsert=VectorStoreError("disk full"))
    for number in range(3):
        await broken.store.save("u1", f"note {number}", ["tea"])
    assert broken.markers() == []
    fresh = Rig(db)
    assert await fresh.store.reindex("u1") == (3, 3)
    assert indexed_ids(fresh) == [1, 2, 3]
    assert len(fresh.points()) == 3
    assert all(p.payload == {"user_id": "u1"} for p in fresh.points().values())
    assert fresh.provider.calls[0][1] == [
        "note 0 [tea]",
        "note 1 [tea]",
        "note 2 [tea]",
    ]
    assert fresh.store.index_off_reason is None


async def test_the_limit_is_respected_then_the_rest_is_repaired(db: Path) -> None:
    await seed_unindexed(db, 5)
    rig = Rig(db)
    assert await rig.store.reindex("u1", limit=2) == (2, 5)
    assert indexed_ids(rig) == [1, 2]  # oldest first
    assert await rig.store.reindex("u1") == (3, 3)
    assert indexed_ids(rig) == [1, 2, 3, 4, 5]


async def test_texts_go_to_the_embedder_in_batches_of_16(db: Path) -> None:
    await seed_unindexed(db, 40)
    rig = Rig(db)
    assert await rig.store.reindex("u1") == (40, 40)
    assert [len(call[1]) for call in rig.provider.calls] == [16, 16, 8]
    assert REINDEX_BATCH_SIZE == 16
    assert len(rig.markers()) == 40


async def test_a_second_call_is_a_no_op_without_any_call(db: Path) -> None:
    await seed_unindexed(db, 3)
    rig = Rig(db)
    await rig.store.reindex("u1")
    embed_calls, vector_calls = embeds(rig), len(rig.vectors.calls)
    assert await rig.store.reindex("u1") == (0, 0)
    assert (embeds(rig), len(rig.vectors.calls)) == (embed_calls, vector_calls)


async def test_nothing_to_do_in_a_fresh_process_needs_no_embed_or_vector_call(
    db: Path,
) -> None:
    await Rig(db).store.save("u1", "Likes tea", ["tea"])
    rig = Rig(db)
    assert await rig.store.reindex("u1") == (0, 0)
    assert embeds(rig) == 0 and rig.vectors.calls == []
    assert rig.factory_calls == 1


async def test_another_users_rows_are_untouched(db: Path) -> None:
    await seed_unindexed(db, 2, "u1")
    await seed_unindexed(db, 2, "u2")
    rig = Rig(db)
    assert await rig.store.reindex("u1") == (2, 2)
    assert indexed_ids(rig) == [1, 2]
    assert all(p.payload == {"user_id": "u1"} for p in rig.points().values())
    assert await rig.store.reindex("u2") == (2, 2)


async def test_an_edit_clears_the_marker_and_only_reindex_or_a_write_restores_it(
    db: Path,
) -> None:
    rig = Rig(db)
    memory = await rig.store.save("u1", "Likes tea", ["tea"])
    rig.sql("UPDATE memories SET text = 'Likes jam' WHERE id = ?", (memory.id,))
    assert rig.markers() == []
    assert await rig.store.reindex("u1") == (1, 1)
    assert rig.markers() == [(COLLECTION, memory.id)]
    assert rig.provider.calls[-1][1] == ["Likes jam [tea]"]


async def test_a_new_embedding_model_makes_every_row_unindexed_again(db: Path) -> None:
    first = Rig(db)
    await first.store.save("u1", "Likes tea", ["tea"])
    second = Rig(db)
    second.factory = lambda: _other_model_embedder(second)  # type: ignore[method-assign]
    second.store._embedder_factory = second.factory
    assert await second.store.reindex("u1") == (1, 1)
    other = collection_name("other-model")
    assert sorted(c for c, _ in second.markers()) == sorted([COLLECTION, other])


def _other_model_embedder(rig: Rig) -> Embedder:
    return Embedder(rig.provider, model("other-model"))


async def test_an_off_index_gives_none_and_makes_no_call(db: Path) -> None:
    await seed_unindexed(db, 2)
    rig = Rig(db, ensure_collection=VectorStoreError("down"))
    assert await rig.store.reindex("u1") is None
    assert rig.store.index_off_reason == "VectorStoreError: down"
    calls = (embeds(rig), len(rig.vectors.calls))
    assert await rig.store.reindex("u1") is None
    assert (embeds(rig), len(rig.vectors.calls)) == calls


async def test_a_failure_midway_keeps_earlier_markers_and_sets_the_reason_once(
    db: Path,
) -> None:
    await seed_unindexed(db, 20)
    rig = Rig(db)
    real_upsert = rig.vectors.upsert
    seen: list[int] = []

    async def flaky(collection, points):
        seen.append(len(points))
        if len(seen) == 2:
            raise VectorStoreError("disk full")
        await real_upsert(collection, points)

    rig.vectors.upsert = flaky  # type: ignore[method-assign]
    assert await rig.store.reindex("u1") is None
    assert len(rig.markers()) == 16
    assert rig.store.index_off_reason == "VectorStoreError: disk full"
    assert rig.store.take_notice() is not None
    assert rig.store.take_notice() is None


async def test_cancellation_passes_through(db: Path) -> None:
    await seed_unindexed(db, 1)
    rig = Rig(db)

    async def cancelled(collection, points):
        raise asyncio.CancelledError

    rig.vectors.upsert = cancelled  # type: ignore[method-assign]
    with pytest.raises(asyncio.CancelledError):
        await rig.store.reindex("u1")
    assert rig.store.index_off_reason is None


# --- lazy repair --------------------------------------------------------------------


async def test_the_first_search_repairs_once_per_user_and_not_the_result(
    db: Path,
) -> None:
    await seed_unindexed(db, 3, "u1")
    await seed_unindexed(db, 2, "u2")
    rig = Rig(db)
    plain = MemoryStore(Rig(db).store._relational)
    expected = await plain.search("u1", "note", 5)
    first = await rig.store.search("u1", "note")
    assert {m.id for m in first} == {m.id for m in expected}
    assert indexed_ids(rig) == [1, 2, 3]
    after_first = embeds(rig)  # 1 query embed + 1 repair batch
    assert after_first == 2
    await rig.store.search("u1", "note")
    assert embeds(rig) == after_first + 1  # the query only, no second repair
    await rig.store.search("u2", "note")
    assert indexed_ids(rig) == [1, 2, 3, 4, 5]  # a second user gets its own repair


async def test_the_lazy_repair_is_bounded_to_20_rows(db: Path) -> None:
    await seed_unindexed(db, 30)
    rig = Rig(db)
    await rig.store.search("u1", "note")
    assert LAZY_REPAIR_LIMIT == 20
    assert len(rig.markers()) == 20
    await rig.store.search("u1", "note")
    assert len(rig.markers()) == 20


async def test_a_failing_repair_never_breaks_the_search(db: Path) -> None:
    await seed_unindexed(db, 2)
    rig = Rig(db)

    async def fail_after_search(collection, points):
        raise VectorStoreError("disk full")

    rig.vectors.upsert = fail_after_search  # type: ignore[method-assign]
    found = await rig.store.search("u1", "note")
    assert len(found) == 2
    assert rig.store.index_off_reason == "VectorStoreError: disk full"


# --- /memory reindex ----------------------------------------------------------------


def test_reindex_parses_and_takes_no_argument() -> None:
    assert parse_memory_command("/memory reindex") == MemoryCommand("reindex")
    assert parse_memory_command("  /memory   reindex ") == MemoryCommand("reindex")
    assert parse_memory_command("/memory reindex now") == MemoryCommand("usage")
    assert parse_memory_command("/memory REINDEX") == MemoryCommand("usage")


def test_reindex_all_parses_and_takes_nothing_after_it() -> None:
    assert parse_memory_command("/memory reindex --all") == MemoryCommand("reindex-all")
    assert parse_memory_command("  /memory  reindex   --all ") == MemoryCommand(
        "reindex-all"
    )
    for line in (
        "/memory reindex --all x",
        "/memory reindex foo",
        "/memory reindex -all",
    ):
        assert parse_memory_command(line) == MemoryCommand("usage")


def test_usage_mentions_reindex() -> None:
    first = "usage: /memory [list [PAGE]] | /memory show ID | /memory delete ID"
    assert (
        USAGE.splitlines()[0]
        == (
            first
            + " | /memory publish ID | /memory unpublish ID | /memory forget ID"  # memory-shared-admin
            + " | /memory search WORDS | /memory reindex [--all]"
            + " | /memory add [--shared] TEXT"  # memory-add-command
            + " | /memory export [--shared] [PATH] | /memory import [--shared] PATH"  # memory-yaml-transfer
        )
    )
    assert "  reindex rebuild the vector index of your memories" in USAGE.splitlines()
    assert "          --all re-embeds every memory into the current vector store" in (
        USAGE.splitlines()
    )


async def test_the_answer_counts_indexed_of_unindexed(db: Path) -> None:
    await seed_unindexed(db, 3)
    rig = Rig(db)
    reindex = MemoryCommand("reindex")
    assert await run_memory_command(rig.store, "u1", reindex) == (
        "reindexed 3 of 3 memories"
    )
    assert await run_memory_command(rig.store, "u1", reindex) == (
        "reindexed 0 of 0 memories"
    )


async def test_a_plain_store_says_vector_search_is_off(db: Path) -> None:
    plain = MemoryStore(Rig(db).store._relational)
    answer = await run_memory_command(plain, "u1", MemoryCommand("reindex"))
    assert (
        answer
        == VECTORS_OFF
        == ("vector search is off: set [tools] memory_vectors = true")
    )


async def test_an_off_index_answers_with_its_reason(db: Path) -> None:
    await seed_unindexed(db, 1)
    rig = Rig(db, ensure_collection=VectorStoreError("down"))
    answer = await run_memory_command(rig.store, "u1", MemoryCommand("reindex"))
    assert answer == "vector search is off: VectorStoreError: down"


async def test_the_notice_is_not_repeated_after_the_off_answer(
    storage: Storage,  # noqa: F811
) -> None:
    await MemoryStore(storage.relational).save("alice", "Likes tea", ["tea"])
    tools = vector_registry(failing_factory())
    answer = await handle_memory_text("/memory reindex", tools, storage, "alice")
    assert answer is not None
    assert answer.startswith("vector search is off: ConfigError: ")
    assert "\n" not in answer  # no second line with the same reason
    again = await handle_memory_text("/memory reindex", tools, storage, "alice")
    assert again == answer


async def test_handle_memory_text_reindexes_through_the_tools(storage: Storage) -> None:  # noqa: F811
    await MemoryStore(storage.relational).save("alice", "Likes tea", ["tea"])
    tools = vector_registry(working_factory())
    assert await handle_memory_text("/memory reindex", tools, storage, "alice") == (
        "reindexed 1 of 1 memories"
    )
    assert (
        await handle_memory_text("/memory reindex x", tools, storage, "alice") == USAGE
    )


# --- /memory reindex switches the index back on -------------------------------------


class Wired:
    """The store `/memory` reaches through the tools: real SQLite, fake vector store."""

    def __init__(self, opened: Storage) -> None:
        self.storage = opened
        self.provider = ConceptEmbedder()
        self.factory_calls = 0
        self.tools = vector_registry(self.factory)
        store = recall_store(self.tools, opened)
        assert isinstance(store, IndexedMemoryStore)
        self.store = store

    def factory(self) -> Embedder:
        self.factory_calls += 1
        return Embedder(self.provider, model("bge-m3-local"))

    async def command(self) -> str | None:
        return await handle_memory_text(
            "/memory reindex", self.tools, self.storage, "alice"
        )

    async def markers(self) -> int:
        rows = await self.storage.relational.query(
            "SELECT COUNT(*) FROM memory_vectors", ()
        )
        return rows[0][0]

    def vector_calls(self, method: str) -> int:
        return len([c for c in self.storage.vectors.calls if c[0] == method])  # type: ignore[attr-defined]


async def broken_wired(opened: Storage, error: Exception | None = None) -> Wired:
    """Two plain rows, then a save that fails and turns the index off: 3 unindexed."""
    await seed_unindexed_in(opened, 2)
    wired = Wired(opened)
    if error is None:
        wired.provider.raises = EmbeddingError("connection", "ollama down")
    else:
        wired.storage.vectors.fail_on["ensure_collection"] = error  # type: ignore[attr-defined]
    await wired.store.save("alice", "note 2", ["kw2"])
    assert wired.store.index_off_reason is not None
    return wired


async def seed_unindexed_in(opened: Storage, count: int) -> None:
    plain = MemoryStore(opened.relational)
    for number in range(count):
        await plain.save("alice", f"note {number}", [f"kw{number}"])


async def assert_back_on(wired: Wired) -> None:
    assert wired.store.index_off_reason is None
    before = await wired.markers()
    memory = await wired.store.save("alice", "Likes tea", ["tea"])
    points = wired.storage.vectors._points  # type: ignore[attr-defined]
    assert any(str(memory.id) in collection for collection in points.values())
    assert await wired.markers() == before + 1
    searches = wired.vector_calls("search")
    await wired.store.search("alice", "tea")
    # Two vector searches per store search: the user's notes and the shared ones.
    assert wired.vector_calls("search") == searches + 2


async def test_reindex_after_a_transient_embed_failure_switches_the_index_on(
    storage: Storage,  # noqa: F811
) -> None:
    wired = await broken_wired(storage)
    wired.provider.raises = None
    assert await wired.command() == "reindexed 3 of 3 memories"
    assert await wired.markers() == 3
    await assert_back_on(wired)


async def test_reindex_after_a_locked_vector_store_switches_the_index_on(
    storage: Storage,  # noqa: F811
) -> None:
    wired = await broken_wired(storage, VectorStoreLockedError())
    assert "in use by another" in (wired.store.index_off_reason or "")
    del wired.storage.vectors.fail_on["ensure_collection"]  # type: ignore[attr-defined]
    assert await wired.command() == "reindexed 3 of 3 memories"
    await assert_back_on(wired)


async def test_a_persistent_failure_answers_with_the_new_reason_and_one_notice(
    storage: Storage,  # noqa: F811
) -> None:
    wired = await broken_wired(storage)
    first = wired.store.take_notice()  # the one notice the user sees
    assert first is not None and "ollama down" in first
    wired.provider.raises = EmbeddingError("unknown", "second failure")
    answer = await wired.command()
    assert answer is not None
    assert answer.startswith("vector search is off: ")
    assert "second failure" in answer and "ollama down" not in answer
    assert "\n" not in answer  # no notice line after the answer
    assert wired.store.index_off_reason is not None
    assert "second failure" in wired.store.index_off_reason
    assert wired.store.take_notice() is None
    assert await wired.markers() == 0


async def test_a_model_mismatch_is_still_refused_with_the_same_reason(db: Path) -> None:
    await seed_unindexed(db, 1)
    rig = Rig(db)
    rig.sql(
        "INSERT INTO memory_vector_meta (collection, model_id, dim) VALUES (?, ?, ?)",
        (COLLECTION, "concepts", 99),
    )
    assert await rig.store.reindex("u1") is None
    reason = rig.store.index_off_reason
    assert reason is not None and "changed" in reason
    reindex = MemoryCommand("reindex")
    assert await run_memory_command(rig.store, "u1", reindex) == (
        f"vector search is off: {reason}"
    )
    assert rig.store.index_off_reason == reason
    assert rig.markers() == []


async def test_a_store_that_is_on_is_not_resumed_and_makes_no_extra_call(
    tmp_path: Path,
) -> None:
    via_command = Rig(tmp_path / "a" / "memory.db")
    direct = Rig(tmp_path / "b" / "memory.db")
    for rig in (via_command, direct):
        await seed_unindexed(rig.db, 3)
    resumed: list[bool] = []
    via_command.store.resume = lambda: resumed.append(True)  # type: ignore[method-assign]
    answer = await run_memory_command(via_command.store, "u1", MemoryCommand("reindex"))
    assert answer == "reindexed 3 of 3 memories"
    assert await direct.store.reindex("u1") == (3, 3)
    assert resumed == []
    assert embeds(via_command) == embeds(direct)
    assert via_command.vectors.calls == direct.vectors.calls
    assert via_command.factory_calls == direct.factory_calls == 1


async def test_searches_and_writes_while_off_make_no_embed_or_vector_call(
    db: Path,
) -> None:
    rig = Rig(db, ensure_collection=VectorStoreError("down"))
    first = await rig.store.save("u1", "Likes tea", ["tea"])
    assert rig.store.index_off_reason is not None
    calls = (embeds(rig), len(rig.vectors.calls), rig.factory_calls)
    second = await rig.store.save("u1", "Likes jam", ["jam"])
    assert await rig.store.update("u1", first.id, text="Likes tea a lot") is not None
    assert [m.id for m in await rig.store.search("u1", "jam")] == [second.id]
    assert await rig.store.forget("u1", second.id) is True
    assert (embeds(rig), len(rig.vectors.calls), rig.factory_calls) == calls


async def test_cancellation_inside_the_retry_passes_through_and_a_later_reindex_works(
    db: Path,
) -> None:
    await seed_unindexed(db, 2)
    rig = Rig(db)
    rig.provider.raises = EmbeddingError("connection", "down")
    assert await rig.store.reindex("u1") is None
    rig.provider.raises = None

    async def cancelled(model_id, texts, dimensions):
        raise asyncio.CancelledError

    rig.provider.embed = cancelled  # type: ignore[method-assign]
    reindex = MemoryCommand("reindex")
    with pytest.raises(asyncio.CancelledError):
        await run_memory_command(rig.store, "u1", reindex)
    assert rig.store.index_off_reason is None  # consistent: on, nothing half-done
    assert rig.markers() == []
    del rig.provider.embed  # the class method again
    assert await run_memory_command(rig.store, "u1", reindex) == (
        "reindexed 2 of 2 memories"
    )


async def test_resume_on_a_store_that_is_on_makes_no_call_and_keeps_repaired_users(
    db: Path,
) -> None:
    await seed_unindexed(db, 2)
    rig = Rig(db)
    await rig.store.search("u1", "note")
    assert rig.store._repaired_users == {"u1"}
    calls = (embeds(rig), len(rig.vectors.calls))
    assert rig.store.resume() is None
    assert rig.store.index_off_reason is None
    assert rig.store._repaired_users == {"u1"}
    assert (embeds(rig), len(rig.vectors.calls)) == calls


async def test_a_recovered_store_does_not_repair_a_user_a_second_time(db: Path) -> None:
    await seed_unindexed(db, 2)
    rig = Rig(db)
    await rig.store.search("u1", "note")  # lazy repair: u1 is now in the set
    rig.provider.raises = EmbeddingError("connection", "down")
    await rig.store.search("u1", "note")
    assert rig.store.index_off_reason is not None
    rig.provider.raises = None
    reindex = MemoryCommand("reindex")
    assert await run_memory_command(rig.store, "u1", reindex) == (
        "reindexed 0 of 0 memories"
    )
    assert rig.store._repaired_users == {"u1"}
    before = embeds(rig)
    await rig.store.search("u1", "note")
    assert embeds(rig) == before + 1  # the query only, no lazy repair again


async def test_the_retry_rebuilds_the_embedder_and_ensures_the_collection_again(
    db: Path,
) -> None:
    rig = Rig(db)
    await rig.store.save("u1", "Likes tea", ["tea"])
    assert rig.factory_calls == 1 and len(rig.calls("ensure_collection")) == 1
    rig.provider.raises = EmbeddingError("connection", "down")
    await rig.store.save("u1", "Likes jam", ["jam"])
    assert rig.store.index_off_reason is not None
    rig.provider.raises = None
    answer = await run_memory_command(rig.store, "u1", MemoryCommand("reindex"))
    assert answer == "reindexed 1 of 1 memories"
    assert rig.factory_calls == 2
    assert len(rig.calls("ensure_collection")) == 2


# --- /memory reindex --all ----------------------------------------------------------


REINDEX_ALL = MemoryCommand("reindex-all")


async def test_rebuild_processes_every_note_of_the_user_ignoring_markers(
    db: Path,
) -> None:
    await seed_unindexed(db, 3)
    rig = Rig(db)
    assert await rig.store.reindex("u1") == (3, 3)
    assert await rig.store.reindex("u1", rebuild=True) == (3, 3)
    assert await rig.store.reindex("u1") == (0, 0)


async def test_all_fills_a_new_empty_vector_store_that_plain_reindex_leaves_empty(
    db: Path,
) -> None:
    await seed_unindexed(db, 3)
    old = Rig(db)
    assert await old.store.reindex("u1") == (3, 3)
    marked = old.markers()
    new = Rig(db)  # same SQLite and model, so the markers are stale; empty vectors
    assert new.points() == {}
    assert await run_memory_command(new.store, "u1", MemoryCommand("reindex")) == (
        "reindexed 0 of 0 memories"
    )
    assert new.points() == {}
    assert await run_memory_command(new.store, "u1", REINDEX_ALL) == (
        "reindexed 3 of 3 memories"
    )
    assert sorted(new.points()) == ["1", "2", "3"]
    assert new.markers() == marked


async def test_all_twice_is_idempotent(db: Path) -> None:
    await seed_unindexed(db, 3)
    rig = Rig(db)
    assert await run_memory_command(rig.store, "u1", REINDEX_ALL) == (
        "reindexed 3 of 3 memories"
    )
    points, markers = len(rig.points()), sorted(rig.markers())
    assert await run_memory_command(rig.store, "u1", REINDEX_ALL) == (
        "reindexed 3 of 3 memories"
    )
    assert len(rig.points()) == points == 3
    assert sorted(rig.markers()) == markers


async def test_all_leaves_other_users_untouched(db: Path) -> None:
    await seed_unindexed(db, 2, "u1")
    await seed_unindexed(db, 2, "u2")
    rig = Rig(db)
    assert await run_memory_command(rig.store, "u1", REINDEX_ALL) == (
        "reindexed 2 of 2 memories"
    )
    assert sorted(rig.points()) == ["1", "2"]
    assert all(p.payload == {"user_id": "u1"} for p in rig.points().values())
    assert indexed_ids(rig) == [1, 2]


async def test_all_with_a_failing_embedder_answers_off_and_keeps_markers(
    db: Path,
) -> None:
    await seed_unindexed(db, 2)
    rig = Rig(db)
    assert await rig.store.reindex("u1") == (2, 2)
    marked = sorted(rig.markers())
    rig.provider.raises = EmbeddingError("connection", "ollama down")
    answer = await run_memory_command(rig.store, "u1", REINDEX_ALL)
    assert answer.startswith("vector search is off: ")
    assert "ollama down" in answer
    assert sorted(rig.markers()) == marked


async def test_all_lets_cancellation_pass_through(db: Path) -> None:
    await seed_unindexed(db, 1)
    rig = Rig(db)

    async def cancelled(collection, points):
        raise asyncio.CancelledError

    rig.vectors.upsert = cancelled  # type: ignore[method-assign]
    with pytest.raises(asyncio.CancelledError):
        await run_memory_command(rig.store, "u1", REINDEX_ALL)
    assert rig.store.index_off_reason is None


async def test_all_on_a_plain_store_says_vector_search_is_off(db: Path) -> None:
    plain = MemoryStore(Rig(db).store._relational)
    assert await run_memory_command(plain, "u1", REINDEX_ALL) == VECTORS_OFF


async def test_all_through_the_tools_drops_the_duplicate_notice(
    storage: Storage,  # noqa: F811
) -> None:
    await MemoryStore(storage.relational).save("alice", "Likes tea", ["tea"])
    tools = vector_registry(failing_factory())
    answer = await handle_memory_text("/memory reindex --all", tools, storage, "alice")
    assert answer is not None
    assert answer.startswith("vector search is off: ConfigError: ")
    assert "\n" not in answer


# /memory search through an index


async def test_search_with_the_index_off_answers_keyword_hits_and_the_notice(
    db: Path,
) -> None:
    await seed_unindexed(db, 2)
    rig = Rig(db, ensure_collection=VectorStoreError("down"))
    answer = await run_memory_command(
        rig.store,
        "u1",
        parse_memory_command("/memory search note"),  # type: ignore[arg-type]
    )
    lines = answer.splitlines()
    assert len(lines) == 2
    assert all(line.startswith("#") and "note" in line for line in lines)
    assert VECTORS_OFF not in answer
    assert rig.store.index_off_reason is not None
    assert rig.store.take_notice()  # the one-time notice handle_memory_text appends


async def test_search_on_a_plain_store_gives_keyword_hits_not_vectors_off(
    db: Path,
) -> None:
    await seed_unindexed(db, 1)
    plain = MemoryStore(Rig(db).store._relational)
    command = parse_memory_command("/memory search note")
    assert command is not None
    answer = await run_memory_command(plain, "u1", command)
    assert answer.startswith("#1 [kw0]") and VECTORS_OFF not in answer
