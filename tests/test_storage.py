"""`open_storage` and `Storage`: the one object tools and agents get data through."""

from pathlib import Path

import pytest

from llm_harness.config import (
    FilesConfig,
    RelationalConfig,
    StorageConfig,
    VectorConfig,
)
from llm_harness.storage import DisabledVectorStore, Storage, open_storage
from llm_harness.storage.files import LocalFileStore
from llm_harness.storage.qdrant_edge import QdrantEdgeVectorStore
from llm_harness.storage.relational import SqliteRelationalStore
from llm_harness.storage.vectors import (
    VectorPoint,
    VectorStoreError,
    VectorStoreLockedError,
)

DISABLED_TEXT = (
    'the vector store is disabled: set [storage.vector] backend = "qdrant-edge" '
    "in config.toml"
)


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    # Not created: opening storage must not create it either.
    return tmp_path / "data"


def test_defaults_come_from_the_config_under_the_data_dir(data_dir: Path) -> None:
    storage = open_storage(StorageConfig(), data_dir)
    assert isinstance(storage.relational, SqliteRelationalStore)
    assert isinstance(storage.files, LocalFileStore)
    assert isinstance(storage.vectors, QdrantEdgeVectorStore)
    assert storage.relational._path == data_dir / "harness.db"
    assert storage.files._root == data_dir / "files"
    assert storage.vectors._root == data_dir / "vectors"


def test_opening_creates_nothing(data_dir: Path, tmp_path: Path) -> None:
    open_storage(StorageConfig(), data_dir)
    assert list(tmp_path.iterdir()) == []


def test_custom_storage_paths_are_honoured(tmp_path: Path, data_dir: Path) -> None:
    config = StorageConfig(
        relational=RelationalConfig(path=tmp_path / "db" / "ledger.db"),
        vector=VectorConfig(path=tmp_path / "vec"),
        files=FilesConfig(root=tmp_path / "blobs"),
    )
    storage = open_storage(config, data_dir)
    assert storage.relational._path == tmp_path / "db" / "ledger.db"
    assert storage.vectors._root == tmp_path / "vec"
    assert storage.files._root == tmp_path / "blobs"


def test_the_relational_path_argument_wins_over_the_config(
    tmp_path: Path, data_dir: Path
) -> None:
    config = StorageConfig(relational=RelationalConfig(path=tmp_path / "config.db"))
    storage = open_storage(config, data_dir, relational_path=tmp_path / "argument.db")
    assert storage.relational._path == tmp_path / "argument.db"


async def test_the_disabled_vector_backend_says_how_to_turn_it_on(
    data_dir: Path,
) -> None:
    config = StorageConfig(vector=VectorConfig(backend="none"))
    storage = open_storage(config, data_dir)
    assert isinstance(storage.vectors, DisabledVectorStore)
    calls = [
        storage.vectors.ensure_collection("notes", 2),
        storage.vectors.upsert("notes", [VectorPoint("a", [1.0, 0.0], {})]),
        storage.vectors.search("notes", [1.0, 0.0], 1),
        storage.vectors.delete("notes", ["a"]),
    ]
    for call in calls:
        with pytest.raises(VectorStoreError) as disabled:
            await call
        assert str(disabled.value) == DISABLED_TEXT


async def test_closing_works_when_vectors_are_disabled(data_dir: Path) -> None:
    storage = open_storage(StorageConfig(vector=VectorConfig(backend="none")), data_dir)
    await storage.close()


async def test_close_is_idempotent(data_dir: Path) -> None:
    storage = open_storage(StorageConfig(), data_dir)
    await storage.vectors.ensure_collection("notes", 2)
    await storage.close()
    await storage.close()


async def test_close_counts_one_close_of_the_vector_store(data_dir: Path) -> None:
    closed: list[bool] = []

    class SpyVectors(DisabledVectorStore):
        async def close(self) -> None:
            closed.append(True)

    storage = Storage(
        SqliteRelationalStore(data_dir / "harness.db"),
        LocalFileStore(data_dir / "files"),
        SpyVectors(),
    )
    await storage.close()
    await storage.close()
    assert closed == [True]


async def test_close_does_not_create_the_vector_directory(
    data_dir: Path, tmp_path: Path
) -> None:
    storage = open_storage(StorageConfig(), data_dir)
    await storage.close()
    assert list(tmp_path.iterdir()) == []


async def test_close_swallows_a_vector_store_that_did_not_close_cleanly() -> None:
    class FailingVectors(DisabledVectorStore):
        async def close(self) -> None:
            raise VectorStoreError("vector store did not close cleanly: notes")

    storage = Storage(
        SqliteRelationalStore(Path("unused.db")),
        LocalFileStore(Path("unused-files")),
        FailingVectors(),
    )
    await storage.close()


async def test_a_locked_vector_store_does_not_stop_opening(
    tmp_path: Path, data_dir: Path
) -> None:
    config = StorageConfig(vector=VectorConfig(path=tmp_path / "vec"))
    holder = QdrantEdgeVectorStore(tmp_path / "vec")
    await holder.ensure_collection("notes", 2)
    try:
        storage = open_storage(config, data_dir)
        with pytest.raises(VectorStoreLockedError):
            await storage.vectors.ensure_collection("notes", 2)
        await storage.close()
    finally:
        await holder.close()


async def test_all_three_stores_are_reachable(data_dir: Path) -> None:
    storage = open_storage(StorageConfig(), data_dir)
    try:
        await storage.relational.migrate("notes", [("CREATE TABLE t (x INTEGER)",)])
        await storage.relational.execute("INSERT INTO t VALUES (?)", [7])
        assert await storage.relational.query("SELECT x FROM t") == [(7,)]
        await storage.files.put("a/b.txt", b"hi")
        assert await storage.files.get("a/b.txt") == b"hi"
        await storage.vectors.ensure_collection("notes", 2)
        await storage.vectors.upsert("notes", [VectorPoint("a", [1.0, 0.0], {})])
        assert [h.id for h in await storage.vectors.search("notes", [1.0, 0.0], 1)] == [
            "a"
        ]
    finally:
        await storage.close()
