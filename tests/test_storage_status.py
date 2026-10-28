"""`storage_status`: a read-only report that never creates, writes or migrates.

Everything lives under `tmp_path`; the report is asserted line by line.
"""

import hashlib
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

from llm_harness.config import (
    FilesConfig,
    HarnessConfig,
    ProviderConfig,
    RelationalConfig,
    StorageConfig,
    VectorConfig,
)
from llm_harness.storage.files import LocalFileStore
from llm_harness.storage.qdrant_edge import QdrantEdgeVectorStore
from llm_harness.storage.relational import read_schema_versions
from llm_harness.storage.status import storage_status
from llm_harness.store import Store
from tests.test_storage_vectors import _CHILD, CHILD_TIMEOUT_SECONDS, _read_line_within
from tests.test_store import write_old_format_file

PROVIDERS = {"p": ProviderConfig(type="ollama", base_url="http://localhost:11434")}


def make_config(storage: StorageConfig | None = None) -> HarnessConfig:
    return HarnessConfig(
        providers=PROVIDERS, models=[], storage=storage or StorageConfig()
    )


async def status(
    data_dir: Path, config: HarnessConfig | None = None, db: Path | None = None
) -> list[str]:
    return await storage_status(
        config or make_config(),
        relational_path=db or data_dir / "harness.db",
        data_dir=data_dir,
    )


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


async def test_a_missing_data_dir_reports_every_store_as_not_created_and_creates_nothing(
    tmp_path: Path,
) -> None:
    data = tmp_path / "data"
    lines = await status(data)
    assert lines == [
        f"relational: sqlite  {data / 'harness.db'}",
        "  file: not created yet (it is created on first use)",
        "  schema: nothing to read yet",
        f"vector: qdrant-edge  {data / 'vectors'}",
        "  not created yet",
        f"files: local  {data / 'files'}",
        "  not created yet",
    ]
    assert not data.exists()


async def test_an_empty_data_dir_stays_empty(tmp_path: Path) -> None:
    await status(tmp_path)
    assert list(tmp_path.iterdir()) == []


async def test_populated_stores_show_sizes_dimensions_and_counts(
    tmp_path: Path,
) -> None:
    db = tmp_path / "harness.db"
    store = Store(db)
    store.backoff_entries()  # first use migrates the ledger
    vectors = QdrantEdgeVectorStore(tmp_path / "vectors")
    await vectors.ensure_collection("tools", 3, "dot")
    await vectors.close()
    files = LocalFileStore(tmp_path / "files")
    await files.put("a.txt", b"x")
    await files.put("sub/b.txt", b"y")
    lines = await status(tmp_path)
    assert lines[1] == f"  file: exists ({db.stat().st_size} bytes)"
    assert lines[2] == "  schema: ledger version 1 (the code expects 1)"
    assert lines[4] == "  collection tools: 3 dimensions, dot, free"
    assert lines[6] == "  2 files"


async def test_one_file_is_singular(tmp_path: Path) -> None:
    await LocalFileStore(tmp_path / "files").put("a.txt", b"x")
    assert (await status(tmp_path))[-1] == "  1 file"


async def test_an_existing_root_without_collections_or_files_says_so(
    tmp_path: Path,
) -> None:
    (tmp_path / "vectors").mkdir()
    (tmp_path / "files").mkdir()
    lines = await status(tmp_path)
    assert lines[4] == "  no collections yet"
    assert lines[6] == "  0 files"


async def test_a_legacy_ledger_is_reported_and_left_byte_identical(
    tmp_path: Path,
) -> None:
    db = tmp_path / "harness.db"
    write_old_format_file(db)
    before = digest(db)
    lines = await status(tmp_path)
    assert lines[2] == (
        "  schema: legacy file (user_version 1), it will be migrated on the next start"
    )
    assert digest(db) == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["harness.db"]


async def test_a_ledger_from_a_newer_version_is_reported(tmp_path: Path) -> None:
    db = tmp_path / "harness.db"
    Store(db).backoff_entries()
    with closing(sqlite3.connect(db)) as conn:
        conn.execute("UPDATE schema_versions SET version = 99")
        conn.commit()
    line = (await status(tmp_path))[2]
    assert line == (
        "  schema: ledger version 99, newer than this version of llm-harness "
        "(the code expects 1)"
    )


async def test_a_legacy_user_version_from_a_newer_version_is_reported(
    tmp_path: Path,
) -> None:
    write_old_format_file(tmp_path / "harness.db", user_version=7)
    line = (await status(tmp_path))[2]
    assert (
        line
        == "  schema: legacy file (user_version 7), newer than this version of llm-harness"
    )


async def test_an_unknown_namespace_is_listed(tmp_path: Path) -> None:
    db = tmp_path / "harness.db"
    Store(db).backoff_entries()
    with closing(sqlite3.connect(db)) as conn:
        conn.execute("INSERT INTO schema_versions VALUES ('later', 4)")
        conn.commit()
    assert (await status(tmp_path))[3] == (
        "  schema: later version 4 "
        "(this version of llm-harness does not know this namespace)"
    )


async def test_an_empty_file_is_not_yet_set_up(tmp_path: Path) -> None:
    (tmp_path / "harness.db").touch()
    lines = await status(tmp_path)
    assert lines[1] == "  file: exists (0 bytes)"
    assert lines[2] == "  schema: ledger not set up yet (it is set up on first use)"


async def test_a_file_that_is_not_sqlite_is_not_readable_and_untouched(
    tmp_path: Path,
) -> None:
    db = tmp_path / "harness.db"
    db.write_bytes(b"this is not a database file, " * 20)
    before = digest(db)
    lines = await status(tmp_path)
    assert lines[2].startswith("  schema: not readable: ")
    assert digest(db) == before


async def test_a_directory_at_the_ledger_path_is_not_readable(tmp_path: Path) -> None:
    (tmp_path / "harness.db").mkdir()
    assert (await status(tmp_path))[2].startswith("  schema: not readable: ")


async def test_a_ledger_path_with_a_space_and_a_hash_is_read(tmp_path: Path) -> None:
    db = tmp_path / "my ledger#1.db"
    Store(db).backoff_entries()
    lines = await status(tmp_path, db=db)
    assert lines[2] == "  schema: ledger version 1 (the code expects 1)"


async def test_the_disabled_vector_backend_is_reported_and_its_root_ignored(
    tmp_path: Path,
) -> None:
    config = make_config(StorageConfig(vector=VectorConfig(backend="none")))
    lines = await status(tmp_path, config)
    assert 'vector: none  (disabled: [storage.vector] backend = "none")' in lines
    assert not any("collection" in line for line in lines)
    assert list(tmp_path.iterdir()) == []


async def test_custom_storage_paths_are_the_ones_printed(tmp_path: Path) -> None:
    config = make_config(
        StorageConfig(
            relational=RelationalConfig(path=tmp_path / "x" / "ledger.db"),
            vector=VectorConfig(path=tmp_path / "y"),
            files=FilesConfig(root=tmp_path / "z"),
        )
    )
    lines = await storage_status(
        config,
        relational_path=config.storage.relational.path,
        data_dir=tmp_path / "unused",
    )
    assert lines[0] == f"relational: sqlite  {tmp_path / 'x' / 'ledger.db'}"
    assert f"vector: qdrant-edge  {tmp_path / 'y'}" in lines
    assert f"files: local  {tmp_path / 'z'}" in lines
    assert list(tmp_path.iterdir()) == []


async def test_a_collection_with_unreadable_metadata_is_reported(
    tmp_path: Path,
) -> None:
    collection = tmp_path / "vectors" / "tools"
    collection.mkdir(parents=True)
    (collection / "meta.json").write_text("{not json")
    line = (await status(tmp_path))[4]
    assert line.startswith("  collection tools: unreadable: ")
    assert str(tmp_path) not in line


async def test_directories_that_are_not_collections_are_ignored(
    tmp_path: Path,
) -> None:
    (tmp_path / "vectors" / "Not-A-Name").mkdir(parents=True)
    (tmp_path / "vectors" / "empty_dir").mkdir()
    assert (await status(tmp_path))[4] == "  no collections yet"


async def test_a_collection_held_by_another_process_is_locked(
    tmp_path: Path,
) -> None:
    root = tmp_path / "vectors"
    child = subprocess.Popen(  # noqa: ASYNC220 - spawn is instant; every wait is bounded
        [sys.executable, "-c", _CHILD, str(root)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        ready = _read_line_within(child, CHILD_TIMEOUT_SECONDS)
        assert ready == "ready", (
            child.stderr.read() if child.poll() is not None else ready
        )
        lines = await status(tmp_path)
        assert lines[4] == (
            "  collection tools: locked (in use by another llm-harness process)"
        )
        assert child.stdin is not None
        child.stdin.close()
        child.wait(timeout=CHILD_TIMEOUT_SECONDS)
        assert (await status(tmp_path))[4] == (
            "  collection tools: 2 dimensions, cosine, free"
        )
    finally:
        child.kill()
        child.wait(timeout=CHILD_TIMEOUT_SECONDS)
        for stream in (child.stdin, child.stdout, child.stderr):
            if stream is not None:
                stream.close()


def test_read_schema_versions_refuses_to_create_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(sqlite3.Error):
        read_schema_versions(tmp_path / "absent.db")
    assert list(tmp_path.iterdir()) == []
