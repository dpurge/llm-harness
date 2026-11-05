"""Contract tests for the relational stores (`SqliteRelationalStore`, `PostgresRelationalStore`).

The backend-agnostic tests take the `rig` fixture and run once per backend. The
`postgres` parameter is deselected by default (`-m "not live"` in pyproject addopts)
and runs against a real server. Start the container with the command in
`tests/test_live_postgres.py`'s docstring:

    docker --context rancher-desktop run -d --rm --name llm-harness-live-postgres -e POSTGRES_PASSWORD=live-test-password -e POSTGRES_DB=llm_harness_live -p 127.0.0.1:15432:5432 postgres:17

then:

    LLM_HARNESS_LIVE_POSTGRES_URL=postgresql://postgres@127.0.0.1:15432/llm_harness_live LLM_HARNESS_LIVE_POSTGRES_PASSWORD=live-test-password uv run pytest -m live tests/test_storage_relational.py

Each Postgres test uses uniquely named tables and namespaces (uuid suffix) and removes
them, and its `schema_versions` rows, in a `finally` block; the shared
`schema_versions` table itself is never dropped. It skips, with a printed reason,
unless the database name in the url contains `live` or `test`.
"""

import asyncio
import os
import sqlite3
import stat
import threading
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any

import psycopg
import pytest

from llm_harness.redact import Secret
from llm_harness.storage.postgres import PostgresRelationalStore
from llm_harness.storage.relational import (
    SchemaTooNewError,
    SqliteRelationalStore,
)
from tests.live_postgres import scratch_target
from tests.platform_marks import posix_only

# SQLite-shaped migrations, for the SQLite-only tests below; the rig builds the
# per-dialect equivalent for the tests that run on both backends.
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


class _Rig:
    """One test's view of one backend: a store, raw access and unique names.

    Everything that differs between the dialects lives in the subclasses, so the
    test bodies hold no `if` on the backend.
    """

    dialect: str
    database_error: type[Exception]
    id_column: str

    def __init__(self) -> None:
        self.suffix = uuid.uuid4().hex[:12]
        self._tables: list[str] = []
        self._namespaces: list[str] = []
        self.store = self.new_store()

    def new_store(self) -> Any:
        raise NotImplementedError

    def untouched_store(self) -> Any:
        """A store whose first touch of the backend must be visible."""
        raise NotImplementedError

    def assert_untouched(self) -> None:
        raise NotImplementedError

    def raw(self, sql: str) -> list[tuple]:
        raise NotImplementedError

    def versions(self) -> list[tuple]:
        """The `(namespace, version)` rows of this test's namespaces."""
        raise NotImplementedError

    def columns(self, table: str) -> list[str]:
        raise NotImplementedError

    def schema_snapshot(self) -> list[tuple]:
        """The schema objects of this test, for a before and after comparison."""
        raise NotImplementedError

    def assert_legacy_user_version_untouched(self) -> None:
        raise NotImplementedError

    def assert_failed_first_migration_left_nothing(
        self, namespace: str, table: str
    ) -> None:
        raise NotImplementedError

    def cleanup(self) -> None:
        pass

    def namespace(self, base: str) -> str:
        name = f"{base}{self.suffix}"
        self._namespaces.append(name)
        return name

    def table(self, namespace: str, base: str) -> str:
        name = f"{namespace}_{base}"
        self._tables.append(name)
        return name

    def notes(self) -> tuple[str, str, tuple]:
        """The namespace `notes`, its table `item` and its two migration steps."""
        namespace = self.namespace("notes")
        item = self.table(namespace, "item")
        migrations = (
            (f"CREATE TABLE {item} (id {self.id_column}, body TEXT NOT NULL)",),
            (f"ALTER TABLE {item} ADD COLUMN tag TEXT NULL",),
        )
        return namespace, item, migrations

    def other(self) -> tuple[str, str, tuple]:
        """The namespace `other` with one table `t` in one migration step."""
        namespace = self.namespace("other")
        table = self.table(namespace, "t")
        return namespace, table, ((f"CREATE TABLE {table} (a INTEGER)",),)


class _SqliteRig(_Rig):
    dialect = "sqlite"
    database_error = sqlite3.Error
    id_column = "INTEGER PRIMARY KEY"

    def __init__(self, db: Path) -> None:
        self._db = db
        super().__init__()

    def new_store(self) -> SqliteRelationalStore:
        return SqliteRelationalStore(self._db)

    def untouched_store(self) -> SqliteRelationalStore:
        return self.new_store()

    def assert_untouched(self) -> None:
        assert not self._db.parent.exists()

    def raw(self, sql: str) -> list[tuple]:
        return raw(self._db, sql)

    def versions(self) -> list[tuple]:
        return self.raw("SELECT * FROM schema_versions ORDER BY namespace")

    def columns(self, table: str) -> list[str]:
        rows = self.raw(f"SELECT name FROM pragma_table_info('{table}')")
        return [name for (name,) in rows]

    def schema_snapshot(self) -> list[tuple]:
        return self.raw("SELECT type, name, sql FROM sqlite_master ORDER BY name")

    def assert_legacy_user_version_untouched(self) -> None:
        assert self.raw("PRAGMA user_version") == [(0,)]

    def assert_failed_first_migration_left_nothing(
        self, namespace: str, table: str
    ) -> None:
        # SQLite: the file is private to the test, so nothing at all may exist.
        assert self.raw("SELECT name FROM sqlite_master") == []


class _PostgresRig(_Rig):
    dialect = "postgres"
    database_error = psycopg.Error
    # An insert without an id fails on plain INTEGER PRIMARY KEY (see the spec spike).
    id_column = "BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY"

    def __init__(self) -> None:
        self._url, self._password = scratch_target(
            "the relational contract tests create and drop their own uniquely named "
            "tables"
        )
        self.connect_attempts = 0
        super().__init__()

    def _secret(self) -> Secret | None:
        return Secret(self._password) if self._password else None

    def new_store(self) -> PostgresRelationalStore:
        return PostgresRelationalStore(self._url, self._secret())

    def untouched_store(self) -> PostgresRelationalStore:
        def refuse(**kwargs: Any) -> Any:
            self.connect_attempts += 1
            raise AssertionError("the store reached the connection factory")

        return PostgresRelationalStore(
            self._url, self._secret(), connect_factory=refuse
        )

    def assert_untouched(self) -> None:
        assert self.connect_attempts == 0

    def raw(self, sql: str) -> list[tuple]:
        with psycopg.connect(self._url, password=self._password, autocommit=True) as c:
            cursor = c.execute(sql)  # no parameters: a literal `%` stays literal
            return cursor.fetchall() if cursor.description else []

    def _table_count(self, table: str) -> int:
        ((count,),) = self.raw(
            "SELECT count(*) FROM information_schema.tables "
            f"WHERE table_schema = current_schema() AND table_name = '{table}'"
        )
        return count

    def versions(self) -> list[tuple]:
        # `schema_versions` is shared with other tests and other namespaces.
        return self.raw(
            "SELECT namespace, version FROM schema_versions "
            f"WHERE namespace LIKE '%{self.suffix}' ORDER BY namespace"
        )

    def columns(self, table: str) -> list[str]:
        rows = self.raw(
            "SELECT column_name FROM information_schema.columns "
            f"WHERE table_schema = current_schema() AND table_name = '{table}' "
            "ORDER BY ordinal_position"
        )
        return [name for (name,) in rows]

    def schema_snapshot(self) -> list[tuple]:
        return self.raw(
            "SELECT table_name, column_name, data_type "
            "FROM information_schema.columns "
            f"WHERE table_schema = current_schema() AND table_name LIKE '%{self.suffix}%' "
            "ORDER BY table_name, ordinal_position"
        )

    def assert_legacy_user_version_untouched(self) -> None:
        pass  # Postgres has no `PRAGMA user_version`

    def assert_failed_first_migration_left_nothing(
        self, namespace: str, table: str
    ) -> None:
        # Postgres: `schema_versions` is shared and may already exist from other
        # tests, so the SQLite assertion (an empty database) cannot hold. Instead:
        # no row for this test's namespace and no table of this test.
        assert self._table_count(table) == 0
        if self._table_count("schema_versions"):
            assert self.versions() == []

    def cleanup(self) -> None:
        with psycopg.connect(self._url, password=self._password, autocommit=True) as c:
            for table in self._tables:
                c.execute(f"DROP TABLE IF EXISTS {table}")  # our own hex-suffixed names
            if self._table_count("schema_versions"):
                for namespace in self._namespaces:
                    c.execute(
                        "DELETE FROM schema_versions WHERE namespace = %s", (namespace,)
                    )


@pytest.fixture(params=["sqlite", pytest.param("postgres", marks=pytest.mark.live)])
def backend(request: pytest.FixtureRequest) -> str:
    return request.param


@pytest.fixture
def rig(backend: str, db: Path):
    made = _SqliteRig(db) if backend == "sqlite" else _PostgresRig()
    try:
        yield made
    finally:
        made.cleanup()


def test_construction_creates_nothing(db: Path):
    SqliteRelationalStore(db)
    assert not db.parent.exists()


def test_a_fresh_file_migrates_and_records_the_version(rig: _Rig):
    notes, item, migrations = rig.notes()
    assert rig.store.migrate_sync(notes, migrations) == 2
    assert rig.versions() == [(notes, 2)]
    assert rig.columns(item) == ["id", "body", "tag"]
    # Without adoption the legacy user_version is ignored and left alone.
    rig.assert_legacy_user_version_untouched()


def test_a_second_migrate_is_a_no_op(rig: _Rig):
    notes, item, migrations = rig.notes()
    rig.store.migrate_sync(notes, migrations)
    rig.store.execute_sync(f"INSERT INTO {item} (body) VALUES (?)", ("kept",))
    assert rig.store.migrate_sync(notes, migrations) == 2
    assert rig.store.query_sync(f"SELECT body FROM {item}") == [("kept",)]


def test_a_longer_list_applies_only_the_new_steps(rig: _Rig):
    notes, item, migrations = rig.notes()
    rig.store.migrate_sync(notes, migrations[:1])
    rig.store.execute_sync(f"INSERT INTO {item} (body) VALUES (?)", ("kept",))
    assert rig.store.migrate_sync(notes, migrations) == 2
    assert rig.store.query_sync(f"SELECT body, tag FROM {item}") == [("kept", None)]


def test_namespaces_are_versioned_independently(rig: _Rig):
    notes, _, migrations = rig.notes()
    other, _, other_migrations = rig.other()
    rig.store.migrate_sync(notes, migrations)
    rig.store.migrate_sync(other, other_migrations)
    assert rig.versions() == [(notes, 2), (other, 1)]


def test_execute_and_query_round_trip_with_bound_parameters(rig: _Rig):
    notes, item, migrations = rig.notes()
    rig.store.migrate_sync(notes, migrations)
    evil = f"x'); DROP TABLE {item};--"
    assert rig.store.execute_sync(f"INSERT INTO {item} (body) VALUES (?)", (evil,)) == 1
    assert rig.store.query_sync(f"SELECT body FROM {item} WHERE body = ?", (evil,)) == [
        (evil,)
    ]
    assert rig.store.execute_sync(f"UPDATE {item} SET tag = ?", ("t",)) == 1
    assert rig.store.execute_sync(f"DELETE FROM {item} WHERE id = ?", (999,)) == 0


@pytest.mark.parametrize(
    "name", ["", "Notes", "1notes", "no-te", "no te", "a" * 65, "näme", "x;y", "a\n"]
)
def test_an_invalid_namespace_is_rejected_before_any_file_is_made(rig: _Rig, name: str):
    # SQLite: no file or directory is made. Postgres: the connection factory is
    # never reached.
    _, _, migrations = rig.notes()
    store = rig.untouched_store()
    with pytest.raises(ValueError, match="lowercase letters"):
        store.migrate_sync(name, migrations)
    rig.assert_untouched()


def test_a_failing_statement_rolls_everything_back(rig: _Rig):
    other, _, other_migrations = rig.other()
    rig.store.migrate_sync(other, other_migrations)
    before = rig.schema_snapshot()
    notes = rig.namespace("notes")
    one, two = rig.table(notes, "one"), rig.table(notes, "two")
    bad = (
        (f"CREATE TABLE {one} (a INTEGER)", f"INSERT INTO {one} VALUES (1)"),
        (f"CREATE TABLE {two} (a INTEGER)", "THIS IS NOT SQL"),
    )
    with pytest.raises(rig.database_error):
        rig.store.migrate_sync(notes, bad)
    assert rig.versions() == [(other, 1)]
    assert rig.schema_snapshot() == before


def test_a_failing_first_migration_leaves_no_schema_versions_table(rig: _Rig):
    notes = rig.namespace("notes")
    item = rig.table(notes, "item")
    with pytest.raises(rig.database_error):
        rig.store.migrate_sync(notes, (("THIS IS NOT SQL",),))
    rig.assert_failed_first_migration_left_nothing(notes, item)


def test_a_recorded_version_newer_than_the_code_is_refused(rig: _Rig):
    notes, _, migrations = rig.notes()
    rig.store.migrate_sync(notes, migrations)
    with pytest.raises(SchemaTooNewError, match=f"'{notes}'"):
        rig.store.migrate_sync(notes, migrations[:1])
    assert rig.versions() == [(notes, 2)]


# The adoption tests and the user_version test are SQLite-only: they use
# `PRAGMA user_version`, which Postgres does not have. Postgres ignores
# `adopt_user_version`; that is covered in tests/test_storage_postgres.py.
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


def test_two_threads_migrating_one_fresh_file_both_succeed(rig: _Rig):
    notes, _, migrations = rig.notes()
    stores = [rig.new_store(), rig.new_store()]
    barrier = threading.Barrier(2)
    results: list[int] = []
    errors: list[BaseException] = []

    def run(store: Any) -> None:
        try:
            barrier.wait()
            results.append(store.migrate_sync(notes, migrations))
        except rig.database_error as exc:  # reported by the assert below
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(s,)) for s in stores]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert errors == []
    assert results == [2, 2]
    assert rig.versions() == [(notes, 2)]


@posix_only
def test_modes_are_0700_and_0600_even_under_umask_zero(db: Path):
    old = os.umask(0)
    try:
        SqliteRelationalStore(db).migrate_sync("notes", NOTES)
    finally:
        os.umask(old)
    assert stat.S_IMODE(db.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(db.stat().st_mode) == 0o600


async def test_twenty_concurrent_calls_all_succeed(rig: _Rig):
    notes, item, migrations = rig.notes()
    store = rig.store
    assert await store.migrate(notes, migrations) == 2
    counts = await asyncio.gather(
        *(
            store.execute(f"INSERT INTO {item} (body) VALUES (?)", (f"b{i}",))
            for i in range(20)
        )
    )
    assert counts == [1] * 20
    rows = await asyncio.gather(
        *(store.query(f"SELECT COUNT(*) FROM {item}") for _ in range(20))
    )
    assert rows == [[(20,)]] * 20
    bodies = await store.query(f"SELECT body FROM {item} ORDER BY id")
    assert sorted(b for (b,) in bodies) == sorted(f"b{i}" for i in range(20))


async def test_concurrent_async_migrations_both_succeed(rig: _Rig):
    notes, _, migrations = rig.notes()
    store = rig.store
    assert await asyncio.gather(
        store.migrate(notes, migrations), store.migrate(notes, migrations)
    ) == [2, 2]
