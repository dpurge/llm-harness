import asyncio
import os
import sqlite3
import stat
import threading
from contextlib import closing
from pathlib import Path

import pytest

from llm_harness.storage.relational import (
    SchemaTooNewError,
    SqliteRelationalStore,
)

NOTES = (
    ("CREATE TABLE notes_item (id INTEGER PRIMARY KEY, body TEXT NOT NULL)",),
    ("ALTER TABLE notes_item ADD COLUMN tag TEXT NULL",),
)


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "data" / "relational.db"


def raw(path: Path, sql: str) -> list[tuple]:
    with closing(sqlite3.connect(path)) as conn:
        return conn.execute(sql).fetchall()


def test_construction_creates_nothing(db: Path):
    SqliteRelationalStore(db)
    assert not db.parent.exists()


def test_a_fresh_file_migrates_and_records_the_version(db: Path):
    store = SqliteRelationalStore(db)
    assert store.migrate_sync("notes", NOTES) == 2
    assert raw(db, "SELECT * FROM schema_versions") == [("notes", 2)]
    columns = raw(db, "SELECT name FROM pragma_table_info('notes_item')")
    assert [name for (name,) in columns] == ["id", "body", "tag"]
    # Without adoption the legacy user_version is ignored and left alone.
    assert raw(db, "PRAGMA user_version") == [(0,)]


def test_a_second_migrate_is_a_no_op(db: Path):
    store = SqliteRelationalStore(db)
    store.migrate_sync("notes", NOTES)
    store.execute_sync("INSERT INTO notes_item (body) VALUES (?)", ("kept",))
    assert store.migrate_sync("notes", NOTES) == 2
    assert store.query_sync("SELECT body FROM notes_item") == [("kept",)]


def test_a_longer_list_applies_only_the_new_steps(db: Path):
    store = SqliteRelationalStore(db)
    store.migrate_sync("notes", NOTES[:1])
    store.execute_sync("INSERT INTO notes_item (body) VALUES (?)", ("kept",))
    assert store.migrate_sync("notes", NOTES) == 2
    assert store.query_sync("SELECT body, tag FROM notes_item") == [("kept", None)]


def test_namespaces_are_versioned_independently(db: Path):
    store = SqliteRelationalStore(db)
    store.migrate_sync("notes", NOTES)
    store.migrate_sync("other", (("CREATE TABLE other_t (a)",),))
    assert raw(db, "SELECT * FROM schema_versions ORDER BY namespace") == [
        ("notes", 2),
        ("other", 1),
    ]


def test_execute_and_query_round_trip_with_bound_parameters(db: Path):
    store = SqliteRelationalStore(db)
    store.migrate_sync("notes", NOTES)
    evil = "x'); DROP TABLE notes_item;--"
    assert store.execute_sync("INSERT INTO notes_item (body) VALUES (?)", (evil,)) == 1
    assert store.query_sync("SELECT body FROM notes_item WHERE body = ?", (evil,)) == [
        (evil,)
    ]
    assert store.execute_sync("UPDATE notes_item SET tag = ?", ("t",)) == 1
    assert store.execute_sync("DELETE FROM notes_item WHERE id = ?", (999,)) == 0


@pytest.mark.parametrize(
    "name", ["", "Notes", "1notes", "no-te", "no te", "a" * 65, "näme", "x;y", "a\n"]
)
def test_an_invalid_namespace_is_rejected_before_any_file_is_made(db: Path, name: str):
    store = SqliteRelationalStore(db)
    with pytest.raises(ValueError, match="lowercase letters"):
        store.migrate_sync(name, NOTES)
    assert not db.parent.exists()


def test_a_failing_statement_rolls_everything_back(db: Path):
    store = SqliteRelationalStore(db)
    store.migrate_sync("other", (("CREATE TABLE other_t (a)",),))
    before = raw(db, "SELECT type, name, sql FROM sqlite_master ORDER BY name")
    bad = (
        ("CREATE TABLE notes_one (a)", "INSERT INTO notes_one VALUES (1)"),
        ("CREATE TABLE notes_two (a)", "THIS IS NOT SQL"),
    )
    with pytest.raises(sqlite3.Error):
        store.migrate_sync("notes", bad)
    assert raw(db, "SELECT * FROM schema_versions") == [("other", 1)]
    assert raw(db, "SELECT type, name, sql FROM sqlite_master ORDER BY name") == before


def test_a_failing_first_migration_leaves_no_schema_versions_table(db: Path):
    store = SqliteRelationalStore(db)
    with pytest.raises(sqlite3.Error):
        store.migrate_sync("notes", (("THIS IS NOT SQL",),))
    assert raw(db, "SELECT name FROM sqlite_master") == []


def test_a_recorded_version_newer_than_the_code_is_refused(db: Path):
    store = SqliteRelationalStore(db)
    store.migrate_sync("notes", NOTES)
    with pytest.raises(SchemaTooNewError, match="'notes'"):
        store.migrate_sync("notes", NOTES[:1])
    assert raw(db, "SELECT * FROM schema_versions") == [("notes", 2)]


def test_adoption_takes_the_legacy_user_version_as_the_start(db: Path):
    db.parent.mkdir()
    with closing(sqlite3.connect(db)) as conn:
        conn.execute(
            "CREATE TABLE notes_item (id INTEGER PRIMARY KEY, body TEXT NOT NULL)"
        )
        conn.execute("PRAGMA user_version = 1")
    store = SqliteRelationalStore(db)
    assert store.migrate_sync("notes", NOTES, adopt_user_version=True) == 2
    assert raw(db, "SELECT * FROM schema_versions") == [("notes", 2)]
    assert raw(db, "PRAGMA user_version") == [(1,)]


def test_adoption_of_a_fresh_file_starts_at_zero(db: Path):
    store = SqliteRelationalStore(db)
    assert store.migrate_sync("notes", NOTES, adopt_user_version=True) == 2


def test_adoption_of_a_too_new_user_version_is_refused(db: Path):
    db.parent.mkdir()
    with closing(sqlite3.connect(db)) as conn:
        conn.execute("PRAGMA user_version = 5")
    with pytest.raises(SchemaTooNewError):
        SqliteRelationalStore(db).migrate_sync("notes", NOTES, adopt_user_version=True)
    assert raw(db, "SELECT name FROM sqlite_master") == []


def test_a_recorded_row_wins_over_user_version(db: Path):
    store = SqliteRelationalStore(db)
    store.migrate_sync("notes", NOTES)
    with closing(sqlite3.connect(db)) as conn:
        conn.execute("PRAGMA user_version = 9")
    assert store.migrate_sync("notes", NOTES, adopt_user_version=True) == 2


def test_two_threads_migrating_one_fresh_file_both_succeed(db: Path):
    stores = [SqliteRelationalStore(db), SqliteRelationalStore(db)]
    barrier = threading.Barrier(2)
    results: list[int] = []
    errors: list[BaseException] = []

    def run(store: SqliteRelationalStore) -> None:
        try:
            barrier.wait()
            results.append(store.migrate_sync("notes", NOTES))
        except sqlite3.Error as exc:  # reported by the assert below
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(s,)) for s in stores]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert errors == []
    assert results == [2, 2]
    assert raw(db, "SELECT * FROM schema_versions") == [("notes", 2)]


def test_modes_are_0700_and_0600_even_under_umask_zero(db: Path):
    old = os.umask(0)
    try:
        SqliteRelationalStore(db).migrate_sync("notes", NOTES)
    finally:
        os.umask(old)
    assert stat.S_IMODE(db.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(db.stat().st_mode) == 0o600


async def test_twenty_concurrent_calls_all_succeed(db: Path):
    store = SqliteRelationalStore(db)
    assert await store.migrate("notes", NOTES) == 2
    counts = await asyncio.gather(
        *(
            store.execute("INSERT INTO notes_item (body) VALUES (?)", (f"b{i}",))
            for i in range(20)
        )
    )
    assert counts == [1] * 20
    rows = await asyncio.gather(
        *(store.query("SELECT COUNT(*) FROM notes_item") for _ in range(20))
    )
    assert rows == [[(20,)]] * 20
    bodies = await store.query("SELECT body FROM notes_item ORDER BY id")
    assert sorted(b for (b,) in bodies) == sorted(f"b{i}" for i in range(20))


async def test_concurrent_async_migrations_both_succeed(db: Path):
    store = SqliteRelationalStore(db)
    assert await asyncio.gather(
        store.migrate("notes", NOTES), store.migrate("notes", NOTES)
    ) == [2, 2]
